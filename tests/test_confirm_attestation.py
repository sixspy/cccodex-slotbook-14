"""确认与互证同成败（甲路径：同事务双写）。

红测先行，覆盖：
1. 假绿夹具：确认成功、正式册有行、互证页空 —— 对账与互证面必须显形；
2. 正常确认：正式册与已签发互证同事务 1:1 落库，五处同态；
3. 双人确认同一占位：抢档锁，后到只留争用痕，业务零增；
4. 互证写失败：整笔回滚，正式零增、互证零增、占位态不变；
5. 偏航护栏：草稿/作废态互证一律拒写，记「确认互证偏航」。

夹具走本机文件库 fixtures/false_green_no_attestation.json，不替换库文件。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import app  # noqa: E402
from db import FIXTURES, init_db  # noqa: E402
import db as dbmod  # noqa: E402
import confirm_svc  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(dbmod, "DB_PATH", db)
    if db.exists():
        db.unlink()
    init_db()
    yield


def _login(c: TestClient, user: str = "admin", password: str = "123456"):
    r = c.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _counts() -> dict:
    """五处同态快照：正式册、互证册、占位态、档期盘、业务审计。"""
    with dbmod.connect() as conn:
        return {
            "bookings": conn.execute(
                "SELECT COUNT(*) c FROM bookings WHERE status='已确认'"
            ).fetchone()["c"],
            "attestations": conn.execute(
                "SELECT COUNT(*) c FROM attestations WHERE status='已签发'"
            ).fetchone()["c"],
            "holds_open": conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE status='占位中'"
            ).fetchone()["c"],
            "holds_confirmed": conn.execute(
                "SELECT COUNT(*) c FROM holds WHERE status='已确认'"
            ).fetchone()["c"],
            "slot_booked": conn.execute(
                "SELECT COALESCE(SUM(booked),0) s FROM slots"
            ).fetchone()["s"],
            "audit_confirm": conn.execute(
                "SELECT COUNT(*) c FROM audit_trail WHERE action='booking_confirm'"
            ).fetchone()["c"],
            "locks": conn.execute("SELECT COUNT(*) c FROM slot_locks").fetchone()["c"],
        }


def _load_false_green_fixture() -> None:
    """灌入假绿分裂态：有正式、无互证（互证页空的线上事故复刻）。"""
    data = json.loads(
        (FIXTURES / "false_green_no_attestation.json").read_text(encoding="utf-8")
    )
    with dbmod.connect() as conn:
        for s in data["slots"]:
            cur = conn.execute(
                """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
                   VALUES (?,?,?,?,?,0,?,?)""",
                (
                    s["code"], s["title"], s["start_at"], s["end_at"],
                    s["capacity"], s["status"], s.get("generation", 1),
                ),
            )
            slot_id = cur.lastrowid
            for h in data["holds"]:
                if h["slot_code"] != s["code"]:
                    continue
                conn.execute(
                    """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (slot_id, h["party_key"], h["seats"], h["status"],
                     h["expire_at"], h["created_at"]),
                )
            for b in data["bookings"]:
                if b["slot_code"] != s["code"]:
                    continue
                conn.execute(
                    """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (slot_id, None, b["party_key"], b["seats"],
                     b["snapshot_gen"], b["status"], b["created_at"]),
                )
            booked = data["slot_booked_overrides"].get(s["code"], 0)
            if booked:
                conn.execute(
                    "UPDATE slots SET booked=? WHERE id=?", (booked, slot_id)
                )
        conn.commit()


# ---------------------------------------------------------------------------
# 红测一：假绿夹具 —— 有正式无互证必须在互证页与对账面显形
# ---------------------------------------------------------------------------
def test_false_green_booking_without_attestation_is_flagged():
    # 假绿模拟线上存量裂库：在 startup 回填之外灌入，回填不得替它掩盖分裂
    _load_false_green_fixture()
    # 不使用 with：避开 startup 事件（init_db 回填），保持假绿原样
    c = TestClient(app)
    h = _login(c)
    # 前提：确认成功的假绿 —— 正式册确有行
    proof = c.get("/api/proof/P-FG-GREEN", headers=h).json()
    assert proof["bookings"], "夹具前提：正式册有行"
    # 互证页必须带互证册数据，并把「零互证」判为非同态，而不是空页假装没事
    assert proof["attestations"] == []
    assert proof["attestation_ok"] is False

    recon = c.get("/api/reconcile", headers=h).json()
    item = [i for i in recon["items"] if i["slot_code"] == "S-FG-09"][0]
    assert item["booking_seats"] == 2
    assert item["attestation_seats"] == 0
    assert item["ok"] is False
    assert recon["all_ok"] is False


# ---------------------------------------------------------------------------
# 红测二：正常确认 —— 正式与互证同事务双写、1:1、五处同态
# ---------------------------------------------------------------------------
def test_confirm_writes_attestation_in_same_transaction():
    with TestClient(app) as c:
        h = _login(c)
        r = c.post(
            "/api/holds", headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-AT1", "seats": 1},
        )
        assert r.status_code == 200, r.text
        hid = r.json()["hold_id"]

        before = _counts()
        r2 = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r2.status_code == 200, r2.text
        bid = r2.json()["booking_id"]
        assert r2.json()["proof_code"], "成功响应必须带互证号，禁止只回成功文案"
        after = _counts()

        # 正式册与互证册各增 1，占位态翻转，档期盘 booked +1
        assert after["bookings"] == before["bookings"] + 1
        assert after["attestations"] == before["attestations"] + 1
        assert after["holds_open"] == before["holds_open"] - 1
        assert after["holds_confirmed"] == before["holds_confirmed"] + 1
        assert after["slot_booked"] == before["slot_booked"] + 1
        assert after["audit_confirm"] == before["audit_confirm"] + 1
        assert after["locks"] == 0, "确认结束必须释放档锁"

        with dbmod.connect() as conn:
            att = conn.execute(
                "SELECT * FROM attestations WHERE booking_id=?", (bid,)
            ).fetchone()
        assert att is not None
        assert att["status"] == "已签发"
        assert att["hold_id"] == hid
        assert att["party_key"] == "P-AT1"
        # 互证与正式册 1:1，不允许重复签发
        with dbmod.connect() as conn2:
            dup = conn2.execute(
                "SELECT COUNT(*) c FROM attestations WHERE booking_id=?", (bid,)
            ).fetchone()["c"]
        assert dup == 1

        proof = c.get("/api/proof/P-AT1", headers=h).json()
        assert len(proof["attestations"]) == 1
        assert proof["attestation_ok"] is True

        recon = c.get("/api/reconcile/S-AM-01", headers=h).json()
        assert recon["ok"] is True
        assert recon["attestation_seats"] == recon["booking_seats"]


def test_five_views_stay_homomorphic_after_confirm():
    """正式册、互证页、占位态、对账、审计五处同态。"""
    with TestClient(app) as c:
        h = _login(c)
        hid = c.post(
            "/api/holds", headers=h,
            json={"slot_code": "S-PM-02", "party_key": "P-HOMO", "seats": 2},
        ).json()["hold_id"]
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r.status_code == 200, r.text
        bid = r.json()["booking_id"]

        with dbmod.connect() as conn:
            booking = conn.execute(
                "SELECT * FROM bookings WHERE id=?", (bid,)
            ).fetchone()
            att = conn.execute(
                "SELECT * FROM attestations WHERE booking_id=?", (bid,)
            ).fetchone()
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (hid,)).fetchone()
            slot = conn.execute(
                "SELECT * FROM slots WHERE id=?", (booking["slot_id"],)
            ).fetchone()
            audit = conn.execute(
                "SELECT * FROM audit_trail WHERE action='booking_confirm' ORDER BY id DESC LIMIT 1"
            ).fetchone()

        # 1) 正式册
        assert booking["status"] == "已确认"
        # 2) 互证册（互证页数据源）
        assert att is not None and att["status"] == "已签发"
        assert att["seats"] == booking["seats"] == 2
        assert att["snapshot_gen"] == booking["snapshot_gen"] == slot["generation"]
        # 3) 占位态
        assert hold["status"] == "已确认"
        # 4) 档期盘与对账
        assert slot["booked"] == booking["seats"]
        recon = c.get("/api/reconcile/S-PM-02", headers=h).json()
        assert recon["ok"] is True
        # 5) 审计
        assert audit is not None and audit["target"] == "P-HOMO"


# ---------------------------------------------------------------------------
# 红测三：两人同时确认同一占位 —— 抢档锁，后到只留争用痕
# ---------------------------------------------------------------------------
def test_second_confirmer_only_leaves_contention_trace():
    with TestClient(app) as c:
        admin = _login(c, "admin")
        clerk = _login(c, "clerk")
        hid = c.post(
            "/api/holds", headers=admin,
            json={"slot_code": "S-EVE-03", "party_key": "P-RACE", "seats": 1},
        ).json()["hold_id"]

        # 先到者持档锁（模拟两人同时点确认的抢锁瞬间）
        la = c.post(
            "/api/locks/acquire", headers=admin,
            json={"lock_key": "slot:S-EVE-03"},
        )
        assert la.status_code == 200, la.text

        before = _counts()
        r = c.post("/api/bookings/confirm", headers=clerk, json={"hold_id": hid})
        assert r.status_code == 409, r.text
        after = _counts()

        # 后到者：正式册/互证/占位态/档期盘/业务审计零增，只留争用痕
        assert after == before
        contentions = c.get("/api/contention", headers=admin).json()["items"]
        assert any(
            x["lock_key"] == "slot:S-EVE-03" and x["actor"] == "clerk"
            for x in contentions
        ), "后到必须只留争用痕"

        c.post(
            "/api/locks/release", headers=admin,
            json={"lock_key": "slot:S-EVE-03"},
        )

        # 先到者确认成功
        ok = c.post("/api/bookings/confirm", headers=admin, json={"hold_id": hid})
        assert ok.status_code == 200, ok.text

        # 同一占位二次确认：状态护栏拒绝，依旧零增、不重复签发
        before2 = _counts()
        again = c.post("/api/bookings/confirm", headers=clerk, json={"hold_id": hid})
        assert again.status_code == 409
        assert _counts() == before2


# ---------------------------------------------------------------------------
# 红测四：互证写失败 —— 同事务整笔回滚，两边零增
# ---------------------------------------------------------------------------
def test_attestation_failure_rolls_back_both_sides(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("互证写失败注入")

    monkeypatch.setattr(confirm_svc, "write_attestation", boom)

    # 关 500 重抛，拿 HTTP 响应
    with TestClient(app, raise_server_exceptions=False) as c:
        h = _login(c)
        hid = c.post(
            "/api/holds", headers=h,
            json={"slot_code": "S-AM-01", "party_key": "P-ROLL", "seats": 1},
        ).json()["hold_id"]

        before = _counts()
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r.status_code == 500, r.text
        after = _counts()

        # 正式零增、互证零增、占位态未翻转、档期盘未增、锁已释放
        assert after == before
        with dbmod.connect() as conn:
            hold = conn.execute("SELECT * FROM holds WHERE id=?", (hid,)).fetchone()
        assert hold["status"] == "占位中"


# ---------------------------------------------------------------------------
# 红测五：偏航护栏 —— 草稿/作废态互证拒写，记「确认互证偏航」
# ---------------------------------------------------------------------------
def test_draft_attestation_is_drift_rejected(monkeypatch):
    real_writer = confirm_svc.write_attestation

    def as_draft(conn, **kwargs):  # 乙路径残留：试图落草稿态
        kwargs["status"] = "草稿"
        return real_writer(conn, **kwargs)

    monkeypatch.setattr(confirm_svc, "write_attestation", as_draft)

    with TestClient(app, raise_server_exceptions=False) as c:
        h = _login(c)
        hid = c.post(
            "/api/holds", headers=h,
            json={"slot_code": "S-PM-02", "party_key": "P-DRIFT", "seats": 1},
        ).json()["hold_id"]

        before = _counts()
        r = c.post("/api/bookings/confirm", headers=h, json={"hold_id": hid})
        assert r.status_code == 409, r.text
        assert _counts() == before

        with dbmod.connect() as conn:
            drift = conn.execute(
                "SELECT * FROM audit_trail WHERE action='确认互证偏航'"
            ).fetchall()
        assert drift, "偏航必须留审计痕"
        # 互证册里不得出现任何草稿/作废态行
        with dbmod.connect() as conn:
            bad = conn.execute(
                "SELECT COUNT(*) c FROM attestations WHERE status!='已签发'"
            ).fetchone()["c"]
        assert bad == 0
