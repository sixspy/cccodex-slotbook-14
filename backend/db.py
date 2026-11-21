"""SQLite：档期预订母仓公共库层。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FIXTURES = ROOT / "fixtures"
FRONTEND = ROOT / "frontend"

PRODUCT_META = {
    "modules": [
        "slots",
        "holds",
        "bookings",
        "waitlist",
        "reconcile",
        "locks",
        "audit",
        "release",
        "transfer",
    ],
    "stubs": ["release_slot", "transfer_booking"],
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def sha_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  username TEXT PRIMARY KEY,
  password_sha TEXT NOT NULL,
  role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  start_at TEXT NOT NULL,
  end_at TEXT NOT NULL,
  capacity INTEGER NOT NULL,
  booked INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS holds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id INTEGER NOT NULL,
  party_key TEXT NOT NULL,
  seats INTEGER NOT NULL,
  status TEXT NOT NULL,
  expire_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(slot_id) REFERENCES slots(id)
);
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id INTEGER NOT NULL,
  hold_id INTEGER,
  party_key TEXT NOT NULL,
  seats INTEGER NOT NULL,
  snapshot_gen INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waitlist(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slot_id INTEGER NOT NULL,
  party_key TEXT NOT NULL,
  seats INTEGER NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_locks(
  lock_key TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contention_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lock_key TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_trail(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER,
  status TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  booking_id INTEGER,
  to_slot_code TEXT,
  status TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
-- 互证册：确认同事务双写（甲路径），与正式册 bookings 1:1
CREATE TABLE IF NOT EXISTS attestations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proof_code TEXT UNIQUE NOT NULL,
  booking_id INTEGER NOT NULL UNIQUE,
  hold_id INTEGER,
  slot_id INTEGER NOT NULL,
  party_key TEXT NOT NULL,
  seats INTEGER NOT NULL,
  snapshot_gen INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

# 甲路径钉死（2026-11-21 当日锁死）：确认与互证同事务双写。
# 互证册只允许「已签发」一种态；草稿/作废属于乙路径残留，出现即偏航拒写。
ATTESTATION_STATUS_FINAL = "已签发"
ATTESTATION_DRIFT_ACTION = "确认互证偏航"
ATTESTATION_ALLOWED_STATUSES = (ATTESTATION_STATUS_FINAL,)
ATTESTATION_DRIFT_STATUSES = ("草稿", "已作废")


class AttestationDriftError(RuntimeError):
    """偏航：试图以草稿/作废等非终态写互证册（与乙路径混用）。"""


def write_attestation(
    conn,
    *,
    proof_code: str,
    booking_id: int,
    hold_id: int | None,
    slot_id: int,
    party_key: str,
    seats: int,
    snapshot_gen: int,
    status: str,
    created_at: str,
) -> int:
    """同事务写互证册。状态非「已签发」即判偏航，由调用方拒写回滚。"""
    if status not in ATTESTATION_ALLOWED_STATUSES:
        raise AttestationDriftError(
            f"互证状态 {status!r} 非甲路径终态 {ATTESTATION_STATUS_FINAL!r}，"
            "禁止草稿/作废原子切换（乙路径）与同事务双写（甲路径）混用"
        )
    cur = conn.execute(
        """INSERT INTO attestations(
               proof_code,booking_id,hold_id,slot_id,party_key,seats,
               snapshot_gen,status,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            proof_code,
            booking_id,
            hold_id,
            slot_id,
            party_key,
            seats,
            snapshot_gen,
            status,
            created_at,
        ),
    )
    return cur.lastrowid


def backfill_attestations(conn) -> None:
    """老库回填：升级前已确认的正式册补签互证，避免换库与假绿分裂。"""
    rows = conn.execute(
        """SELECT b.id,b.hold_id,b.slot_id,b.party_key,b.seats,b.snapshot_gen,b.created_at
           FROM bookings b
           LEFT JOIN attestations a ON a.booking_id=b.id
           WHERE b.status='已确认' AND a.id IS NULL
           ORDER BY b.id"""
    ).fetchall()
    for b in rows:
        conn.execute(
            """INSERT INTO attestations(
                   proof_code,booking_id,hold_id,slot_id,party_key,seats,
                   snapshot_gen,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                f"PF-BACKFILL-{b['id']:06d}",
                b["id"],
                b["hold_id"],
                b["slot_id"],
                b["party_key"],
                b["seats"],
                b["snapshot_gen"],
                ATTESTATION_STATUS_FINAL,
                b["created_at"],
            ),
        )
    if rows:
        write_audit(
            conn,
            "system",
            "attestation_backfill",
            "attestations",
            {"count": len(rows)},
        )


def write_audit(conn, actor: str, action: str, target: str, payload: dict | None = None) -> None:
    from datetime import datetime

    conn.execute(
        "INSERT INTO audit_trail(actor,action,target,payload,created_at) VALUES (?,?,?,?,?)",
        (
            actor,
            action,
            target,
            json.dumps(payload or {}, ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def ensure_users(conn) -> None:
    for u, p, role in (
        ("admin", "123456", "管理员"),
        ("clerk", "123456", "文员"),
        ("viewer", "123456", "只读"),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO users(username,password_sha,role) VALUES (?,?,?)",
            (u, sha_password(p), role),
        )


def seed_if_empty(conn) -> None:
    n = conn.execute("SELECT COUNT(*) c FROM slots").fetchone()["c"]
    if n:
        return
    slots_path = FIXTURES / "slots.json"
    holds_path = FIXTURES / "holds.json"
    slots = json.loads(slots_path.read_text(encoding="utf-8")) if slots_path.is_file() else []
    holds = json.loads(holds_path.read_text(encoding="utf-8")) if holds_path.is_file() else []
    for s in slots:
        conn.execute(
            """INSERT INTO slots(code,title,start_at,end_at,capacity,booked,status,generation)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                s["code"],
                s["title"],
                s["start_at"],
                s["end_at"],
                s["capacity"],
                s.get("booked", 0),
                s["status"],
                s.get("generation", 1),
            ),
        )
    for h in holds:
        slot = conn.execute("SELECT id FROM slots WHERE code=?", (h["slot_code"],)).fetchone()
        if not slot:
            continue
        conn.execute(
            """INSERT INTO holds(slot_id,party_key,seats,status,expire_at,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                slot["id"],
                h["party_key"],
                h["seats"],
                h["status"],
                h["expire_at"],
                h["created_at"],
            ),
        )
    # 一笔已确认正式册，便于对账种子
    s1 = conn.execute("SELECT id,generation FROM slots WHERE code='S-AM-01'").fetchone()
    if s1:
        conn.execute(
            """INSERT INTO bookings(slot_id,hold_id,party_key,seats,snapshot_gen,status,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (s1["id"], None, "P-SEED", 2, s1["generation"], "已确认", "2026-11-20T09:00:00"),
        )
        conn.execute(
            "UPDATE slots SET booked = booked + 2 WHERE id=?", (s1["id"],)
        )
    conn.execute(
        "INSERT INTO waitlist(slot_id,party_key,seats,reason,status,created_at) VALUES (?,?,?,?,?,?)",
        (s1["id"] if s1 else 1, "P-WAIT", 1, "容量不足", "候补中", "2026-11-20T09:05:00"),
    )
    write_audit(conn, "system", "seed", "slots", {"count": len(slots)})


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        ensure_users(conn)
        seed_if_empty(conn)
        backfill_attestations(conn)
        conn.commit()
