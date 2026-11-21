"""审计与互证只读面。"""
from __future__ import annotations

from fastapi import APIRouter

from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["audit"])


@router.get("/api/audit")
def list_audit(authorization: AuthHeader = None, limit: int = 100):
    require_user(authorization)
    lim = max(1, min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_trail ORDER BY id DESC LIMIT ?", (lim,)
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.get("/api/proof/{party_key}")
def proof(party_key: str, authorization: AuthHeader = None):
    """按当事人互证：占位/正式/候补/互证册是否同源可读。

    互证页空不再被当作成功：已确认正式册必须有 1:1 的已签发互证，
    否则 attestation_ok=False 显形，前台不再误判没订上。
    """
    require_user(authorization)
    with connect() as conn:
        holds = conn.execute(
            "SELECT * FROM holds WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        books = conn.execute(
            "SELECT * FROM bookings WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        waits = conn.execute(
            "SELECT * FROM waitlist WHERE party_key=? ORDER BY id DESC", (party_key,)
        ).fetchall()
        attestations = conn.execute(
            "SELECT * FROM attestations WHERE party_key=? ORDER BY id DESC",
            (party_key,),
        ).fetchall()
        confirmed_ids = {b["id"] for b in books if b["status"] == "已确认"}
        attested_ids = {a["booking_id"] for a in attestations if a["status"] == "已签发"}
        attestation_ok = confirmed_ids == attested_ids
        return {
            "party_key": party_key,
            "holds": [dict(r) for r in holds],
            "bookings": [dict(r) for r in books],
            "waitlist": [dict(r) for r in waits],
            "attestations": [dict(r) for r in attestations],
            "attestation_ok": attestation_ok,
            "confirmed_count": len(confirmed_ids),
            "attested_count": len(attested_ids),
        }
