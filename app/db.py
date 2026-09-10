"""SQLite persistence layer (aiosqlite). Single-user app: one active session, job history, index rows."""
import json
import time
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    phase       TEXT NOT NULL DEFAULT 'index',          -- index | migrate
    status      TEXT NOT NULL DEFAULT 'pending',        -- pending|indexing|awaiting_mapping|migrating|paused|done|failed|canceled
    source_json TEXT,
    dest_json   TEXT,
    filters_json TEXT NOT NULL DEFAULT '[]',
    mapping_json TEXT,
    delay       REAL NOT NULL DEFAULT 1.0,
    total       INTEGER NOT NULL DEFAULT 0,
    processed   INTEGER NOT NULL DEFAULT 0,
    copied      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    last_msg_id INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS copied (
    job_id TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    PRIMARY KEY (job_id, msg_id)
);
CREATE TABLE IF NOT EXISTS index_rows (
    job_id  TEXT NOT NULL,
    msg_id  INTEGER NOT NULL,
    ts      REAL,
    type    TEXT,
    sender  TEXT,
    size    INTEGER DEFAULT 0,
    urls    TEXT DEFAULT '[]',
    preview TEXT,
    PRIMARY KEY (job_id, msg_id)
);
CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    ts      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_index_rows_job ON index_rows(job_id, type);
CREATE INDEX IF NOT EXISTS idx_logs_job ON logs(job_id, id);
"""


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    return conn


async def init_db() -> None:
    conn = await connect()
    try:
        await conn.executescript(SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


# ---------------------------------------------------------------- kv store
async def kv_get(key: str) -> Optional[str]:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None
    finally:
        await conn.close()


async def kv_set(key: str, value: str) -> None:
    conn = await connect()
    try:
        await conn.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await conn.commit()
    finally:
        await conn.close()


async def kv_delete(key: str) -> None:
    conn = await connect()
    try:
        await conn.execute("DELETE FROM kv WHERE key=?", (key,))
        await conn.commit()
    finally:
        await conn.close()


# ---------------------------------------------------------------- jobs
def _row_to_job(row) -> dict:
    j = dict(row)
    for k in ("source_json", "dest_json", "filters_json", "mapping_json"):
        if j.get(k):
            try:
                j[k.replace("_json", "")] = json.loads(j[k])
            except Exception:
                j[k.replace("_json", "")] = None
        else:
            j[k.replace("_json", "")] = None
    return j


async def create_job(job: dict) -> None:
    now = time.time()
    conn = await connect()
    try:
        await conn.execute(
            """INSERT INTO jobs(id,kind,phase,status,source_json,dest_json,filters_json,mapping_json,delay,
                                total,processed,copied,skipped,failed,last_msg_id,error,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job["id"], job["kind"], job.get("phase", "index"), job.get("status", "pending"),
                json.dumps(job.get("source")), json.dumps(job.get("dest")),
                json.dumps(job.get("filters", [])), json.dumps(job.get("mapping")),
                job.get("delay", 1.0), 0, 0, 0, 0, 0, 0, None, now, now,
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    for k in ("source", "dest", "filters", "mapping"):
        if k in fields:
            fields[k + "_json"] = json.dumps(fields.pop(k))
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    conn = await connect()
    try:
        await conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), job_id))
        await conn.commit()
    finally:
        await conn.close()


async def get_job(job_id: str) -> Optional[dict]:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = await cur.fetchone()
        return _row_to_job(row) if row else None
    finally:
        await conn.close()


async def list_jobs(limit: int = 100) -> list:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = await cur.fetchall()
        return [_row_to_job(r) for r in rows]
    finally:
        await conn.close()


# ---------------------------------------------------------------- copied tracking
async def mark_copied(job_id: str, msg_ids: list) -> None:
    if not msg_ids:
        return
    conn = await connect()
    try:
        await conn.executemany(
            "INSERT OR IGNORE INTO copied(job_id,msg_id) VALUES(?,?)",
            [(job_id, m) for m in msg_ids],
        )
        await conn.commit()
    finally:
        await conn.close()


async def copied_set(job_id: str) -> set:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT msg_id FROM copied WHERE job_id=?", (job_id,))
        return {r["msg_id"] for r in await cur.fetchall()}
    finally:
        await conn.close()


# ---------------------------------------------------------------- index rows
async def insert_index_rows(job_id: str, rows: list) -> None:
    if not rows:
        return
    conn = await connect()
    try:
        await conn.executemany(
            """INSERT OR IGNORE INTO index_rows(job_id,msg_id,ts,type,sender,size,urls,preview)
               VALUES(?,?,?,?,?,?,?,?)""",
            [(job_id, r["msg_id"], r.get("ts"), r.get("type"), r.get("sender"),
              r.get("size", 0), json.dumps(r.get("urls", [])), r.get("preview", "")) for r in rows],
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_index_rows(job_id: str) -> list:
    conn = await connect()
    try:
        cur = await conn.execute("SELECT * FROM index_rows WHERE job_id=? ORDER BY ts DESC", (job_id,))
        rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["urls"] = json.loads(d.get("urls") or "[]")
            except Exception:
                d["urls"] = []
            out.append(d)
        return out
    finally:
        await conn.close()


async def index_type_counts(job_id: str) -> dict:
    conn = await connect()
    try:
        cur = await conn.execute(
            "SELECT type, COUNT(*) AS n FROM index_rows WHERE job_id=? GROUP BY type", (job_id,)
        )
        return {r["type"]: r["n"] for r in await cur.fetchall()}
    finally:
        await conn.close()


async def delete_job_data(job_id: str) -> None:
    conn = await connect()
    try:
        await conn.execute("DELETE FROM copied WHERE job_id=?", (job_id,))
        await conn.execute("DELETE FROM index_rows WHERE job_id=?", (job_id,))
        await conn.execute("DELETE FROM logs WHERE job_id=?", (job_id,))
        await conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        await conn.commit()
    finally:
        await conn.close()


# ---------------------------------------------------------------- logs
async def add_log(job_id: str, message: str, level: str = "info") -> None:
    conn = await connect()
    try:
        await conn.execute(
            "INSERT INTO logs(job_id,level,message,ts) VALUES(?,?,?,?)",
            (job_id, level, message, time.time()),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_logs(job_id: str, limit: int = 200) -> list:
    conn = await connect()
    try:
        cur = await conn.execute(
            "SELECT level,message,ts FROM logs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]
    finally:
        await conn.close()
