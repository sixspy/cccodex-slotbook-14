"""对账：档期 booked、正式册 seats、互证册 seats 三方同源校验。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _confirmed_seats(conn, slot_id: int) -> int:
    return conn.execute(
        """SELECT COALESCE(SUM(seats),0) s FROM bookings
           WHERE slot_id=? AND status='已确认'""",
        (slot_id,),
    ).fetchone()["s"]


def _attested_seats(conn, slot_id: int) -> int:
    return conn.execute(
        """SELECT COALESCE(SUM(seats),0) s FROM attestations
           WHERE slot_id=? AND status='已签发'""",
        (slot_id,),
    ).fetchone()["s"]


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        slots = conn.execute("SELECT * FROM slots ORDER BY code").fetchall()
        items = []
        for s in slots:
            booked_sum = _confirmed_seats(conn, s["id"])
            attested_sum = _attested_seats(conn, s["id"])
            # 三方同源：盘面 booked = 正式册 = 互证册，缺互证即显形
            ok = (
                int(booked_sum) == int(s["booked"])
                and int(attested_sum) == int(booked_sum)
            )
            items.append(
                {
                    "slot_code": s["code"],
                    "slot_booked": s["booked"],
                    "booking_seats": booked_sum,
                    "attestation_seats": attested_sum,
                    "generation": s["generation"],
                    "ok": ok,
                    "status": s["status"],
                }
            )
        return {"items": items, "all_ok": all(i["ok"] for i in items)}


@router.get("/api/reconcile/{code}")
def reconcile_one(code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        s = conn.execute("SELECT * FROM slots WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(404, "档期不存在")
        booked_sum = _confirmed_seats(conn, s["id"])
        attested_sum = _attested_seats(conn, s["id"])
        holds = conn.execute(
            "SELECT COUNT(*) c FROM holds WHERE slot_id=? AND status='占位中'",
            (s["id"],),
        ).fetchone()["c"]
        return {
            "slot_code": code,
            "slot_booked": s["booked"],
            "booking_seats": booked_sum,
            "attestation_seats": attested_sum,
            "open_holds": holds,
            "ok": int(booked_sum) == int(s["booked"])
            and int(attested_sum) == int(booked_sum),
            "generation": s["generation"],
        }
