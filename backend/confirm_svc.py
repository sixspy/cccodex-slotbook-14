"""确认入册：占位 → 正式预订，写快照 generation。

甲路径（2026-11-21 当日锁死）：正式册 bookings 与互证册 attestations 在同一个
数据库事务内双写，任一步失败两边同滚——正式零增、互证零增、占位态不翻转。
禁止只返回成功文案；禁止与乙路径（草稿→原子切换）混用，非「已签发」态互证
一律按「确认互证偏航」拒写并留审计痕。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth import AuthHeader, require_user, require_writer
from db import (
    ATTESTATION_DRIFT_ACTION,
    ATTESTATION_STATUS_FINAL,
    AttestationDriftError,
    connect,
    write_audit,
    write_attestation,
)
from locks import release, try_acquire

router = APIRouter(tags=["confirm"])


class ConfirmIn(BaseModel):
    hold_id: int


@router.get("/api/bookings")
def list_bookings(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.*, s.code AS slot_code, a.proof_code AS proof_code
               FROM bookings b
               JOIN slots s ON s.id=b.slot_id
               LEFT JOIN attestations a ON a.booking_id=b.id
               ORDER BY b.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/bookings/confirm")
def confirm_hold(body: ConfirmIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        hold = conn.execute(
            "SELECT * FROM holds WHERE id=?", (body.hold_id,)
        ).fetchone()
        if not hold:
            raise HTTPException(404, "占位不存在")
        slot = conn.execute(
            "SELECT * FROM slots WHERE id=?", (hold["slot_id"],)
        ).fetchone()
        if not slot:
            raise HTTPException(404, "档期不存在")
        lock_key = f"slot:{slot['code']}"
        # 两人同时确认同一占位：抢档锁，后到只留争用痕，不进业务事务
        if not try_acquire(conn, lock_key, user):
            conn.commit()  # 仅落争用痕（contention_log），不写任何业务
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            # 持锁后复检占位态，杜绝持锁等待期间状态被改
            hold = conn.execute(
                "SELECT * FROM holds WHERE id=?", (body.hold_id,)
            ).fetchone()
            if hold["status"] != "占位中":
                raise HTTPException(409, "占位已非占位中，禁止确认")
            if hold["expire_at"] < datetime.now().isoformat(timespec="seconds"):
                conn.execute(
                    "UPDATE holds SET status='已过期' WHERE id=?", (hold["id"],)
                )
                write_audit(conn, user, "hold_expire", str(hold["id"]), {})
                conn.commit()
                raise HTTPException(409, "占位已过期")
            remain = int(slot["capacity"]) - int(slot["booked"])
            if hold["seats"] > remain:
                raise HTTPException(409, "确认时容量不足，请释放或改候补")
            now = datetime.now().isoformat(timespec="seconds")
            cur = conn.execute(
                """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    slot["id"],
                    hold["id"],
                    hold["party_key"],
                    hold["seats"],
                    slot["generation"],
                    "已确认",
                    now,
                ),
            )
            booking_id = cur.lastrowid
            # —— 甲路径：正式册与互证册同事务双写，任一步失败整笔回滚 ——
            proof_code = f"PF-{now.replace('-', '').replace(':', '').replace('T', '-')}-{booking_id:06d}"
            try:
                write_attestation(
                    conn,
                    proof_code=proof_code,
                    booking_id=booking_id,
                    hold_id=hold["id"],
                    slot_id=slot["id"],
                    party_key=hold["party_key"],
                    seats=hold["seats"],
                    snapshot_gen=slot["generation"],
                    status=ATTESTATION_STATUS_FINAL,
                    created_at=now,
                )
            except AttestationDriftError:
                # 偏航：业务同事务回滚（正式零增、互证零增），另起事务留审计痕
                conn.rollback()
                with connect() as audit_conn:
                    write_audit(
                        audit_conn,
                        user,
                        ATTESTATION_DRIFT_ACTION,
                        hold["party_key"],
                        {"hold_id": hold["id"], "slot": slot["code"]},
                    )
                    audit_conn.commit()
                raise HTTPException(409, "确认互证偏航：禁止草稿态互证，已拒写")
            conn.execute(
                "UPDATE holds SET status='已确认' WHERE id=?", (hold["id"],)
            )
            conn.execute(
                "UPDATE slots SET booked = booked + ? WHERE id=?",
                (hold["seats"], slot["id"]),
            )
            write_audit(
                conn,
                user,
                "booking_confirm",
                hold["party_key"],
                {
                    "booking_id": booking_id,
                    "proof_code": proof_code,
                    "gen": slot["generation"],
                },
            )
            conn.commit()
            return {
                "ok": True,
                "booking_id": booking_id,
                "proof_code": proof_code,
                "snapshot_gen": slot["generation"],
            }
        except HTTPException:
            conn.rollback()  # 业务失败整笔回滚，不允许半笔假绿
            raise
        except Exception:
            conn.rollback()  # 互证/正式任一步异常：两边同滚
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()
