"""
core/monitor_db.py
──────────────────
SQLite persistence layer for monitors and cron jobs.
Extends the existing audit.db — no new DB file, no new process.

Tables added:
  monitors       — monitor definitions (type, config JSON, interval, actions)
  monitor_state  — cursor / last-seen per monitor (change detection)
  monitor_runs   — execution log per poll cycle
  cron_jobs      — scheduled task definitions
  cron_runs      — execution history per job
"""
import sqlite3
import json
import os
from datetime import datetime
from core.logger import get_logger

logger = get_logger("monitor_db")

DB_PATH = "logs/audit.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    """
    Create all proactive-agent tables if they don't exist.
    Safe to call on every startup — fully idempotent.
    """
    os.makedirs("logs", exist_ok=True)
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS monitors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            type        TEXT    NOT NULL,
            config      TEXT    NOT NULL DEFAULT '{}',
            actions     TEXT    NOT NULL DEFAULT '[]',
            interval_s  INTEGER NOT NULL DEFAULT 60,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monitor_state (
            monitor_id  INTEGER PRIMARY KEY,
            cursor      TEXT,
            last_run_at TEXT,
            last_ok_at  TEXT,
            error_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS monitor_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id      INTEGER NOT NULL,
            ran_at          TEXT    NOT NULL,
            events_found    INTEGER NOT NULL DEFAULT 0,
            actions_taken   INTEGER NOT NULL DEFAULT 0,
            success         INTEGER NOT NULL DEFAULT 1,
            error           TEXT,
            duration_ms     REAL,
            FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cron_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            cron_expr   TEXT    NOT NULL,
            prompt      TEXT    NOT NULL,
            mode        TEXT    NOT NULL DEFAULT 'autonomous',
            chat_id     TEXT,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            last_run_at TEXT,
            next_run_at TEXT
        );

        CREATE TABLE IF NOT EXISTS cron_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL,
            ran_at      TEXT    NOT NULL,
            mode        TEXT,
            success     INTEGER NOT NULL DEFAULT 1,
            result_text TEXT,
            error       TEXT,
            duration_ms REAL,
            FOREIGN KEY (job_id) REFERENCES cron_jobs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_mon_runs_mid  ON monitor_runs(monitor_id);
        CREATE INDEX IF NOT EXISTS idx_mon_runs_at   ON monitor_runs(ran_at);
        CREATE INDEX IF NOT EXISTS idx_cron_runs_jid ON cron_runs(job_id);
    """)
    conn.commit()
    conn.close()
    logger.info("Monitor DB tables initialized")


# ═══════════════════════════════════════════════════════════════════════════
# Monitor CRUD
# ═══════════════════════════════════════════════════════════════════════════

def get_all_monitors() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT
                m.*,
                s.cursor, s.last_run_at, s.last_ok_at, s.error_count,
                (SELECT COUNT(*) FROM monitor_runs r
                 WHERE r.monitor_id = m.id
                   AND r.ran_at > datetime('now', '-1 hour')
                   AND r.events_found > 0) AS events_last_hour,
                (SELECT COUNT(*) FROM monitor_runs r
                 WHERE r.monitor_id = m.id
                   AND r.ran_at > datetime('now', '-24 hours')
                   AND r.events_found > 0) AS events_last_day
            FROM monitors m
            LEFT JOIN monitor_state s ON s.monitor_id = m.id
            ORDER BY m.id
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_monitor(monitor_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT m.*, s.cursor, s.last_run_at, s.last_ok_at, s.error_count
            FROM monitors m
            LEFT JOIN monitor_state s ON s.monitor_id = m.id
            WHERE m.id = ?
        """, (monitor_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_monitor(
    name: str,
    type_: str,
    config: dict,
    actions: list,
    interval_s: int,
) -> int:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO monitors
               (name, type, config, actions, interval_s, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (name, type_, json.dumps(config), json.dumps(actions), interval_s, now, now),
        )
        monitor_id = cur.lastrowid
        conn.execute(
            """INSERT INTO monitor_state
               (monitor_id, cursor, last_run_at, last_ok_at, error_count)
               VALUES (?,NULL,NULL,NULL,0)""",
            (monitor_id,),
        )
        conn.commit()
        logger.info(f"Monitor created: [{monitor_id}] {name} ({type_})")
        return monitor_id
    finally:
        conn.close()


def update_monitor(monitor_id: int, **kwargs) -> bool:
    allowed = {"name", "type", "config", "actions", "interval_s", "enabled"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False

    # Serialize complex fields
    for field in ("config", "actions"):
        if field in updates and not isinstance(updates[field], str):
            updates[field] = json.dumps(updates[field])

    now = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [now, monitor_id]

    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE monitors SET {set_clause}, updated_at=? WHERE id=?", values
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_monitor(monitor_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM monitors WHERE id=?", (monitor_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_monitor_state(monitor_id: int) -> dict:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM monitor_state WHERE monitor_id=?", (monitor_id,)
        ).fetchone()
        return dict(row) if row else {"monitor_id": monitor_id, "cursor": None, "error_count": 0}
    finally:
        conn.close()


def save_monitor_state(monitor_id: int, cursor: str | None, success: bool):
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        if success:
            conn.execute(
                """UPDATE monitor_state
                   SET cursor=?, last_run_at=?, last_ok_at=?, error_count=0
                   WHERE monitor_id=?""",
                (cursor, now, now, monitor_id),
            )
        else:
            conn.execute(
                """UPDATE monitor_state
                   SET last_run_at=?, error_count=error_count+1
                   WHERE monitor_id=?""",
                (now, monitor_id),
            )
        conn.commit()
    finally:
        conn.close()


def reset_monitor_state(monitor_id: int):
    """Clear cursor — next poll treats everything as new."""
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE monitor_state SET cursor=NULL, error_count=0 WHERE monitor_id=?",
            (monitor_id,),
        )
        conn.commit()
    finally:
        conn.close()


def log_monitor_run(
    monitor_id: int,
    events_found: int,
    actions_taken: int,
    success: bool,
    error: str | None,
    duration_ms: float,
):
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO monitor_runs
               (monitor_id, ran_at, events_found, actions_taken, success, error, duration_ms)
               VALUES (?,?,?,?,?,?,?)""",
            (monitor_id, now, events_found, actions_taken, int(success), error, duration_ms),
        )
        conn.commit()
    finally:
        conn.close()


def get_monitor_runs(monitor_id: int, limit: int = 30) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM monitor_runs WHERE monitor_id=? ORDER BY id DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_monitor_stats() -> dict:
    """Aggregate stats for the dashboard overview."""
    conn = _get_conn()
    try:
        total     = conn.execute("SELECT COUNT(*) FROM monitors").fetchone()[0] or 0
        active    = conn.execute("SELECT COUNT(*) FROM monitors WHERE enabled=1").fetchone()[0] or 0
        in_error  = conn.execute(
            "SELECT COUNT(*) FROM monitor_state WHERE error_count >= 3"
        ).fetchone()[0] or 0
        events_24h = conn.execute(
            "SELECT COALESCE(SUM(events_found),0) FROM monitor_runs "
            "WHERE ran_at > datetime('now','-24 hours')"
        ).fetchone()[0] or 0
        return {
            "total": total,
            "active": active,
            "in_error": in_error,
            "events_24h": events_24h,
        }
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Cron Job CRUD
# ═══════════════════════════════════════════════════════════════════════════

def get_all_cron_jobs() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute("SELECT * FROM cron_jobs ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_cron_job(job_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM cron_jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_cron_job(
    name: str,
    cron_expr: str,
    prompt: str,
    mode: str = "autonomous",
    chat_id: str | None = None,
) -> int:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO cron_jobs
               (name, cron_expr, prompt, mode, chat_id, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,1,?,?)""",
            (name, cron_expr, prompt, mode, chat_id, now, now),
        )
        conn.commit()
        logger.info(f"Cron job created: [{cur.lastrowid}] {name} ({cron_expr})")
        return cur.lastrowid
    finally:
        conn.close()


def update_cron_job(job_id: int, **kwargs) -> bool:
    allowed = {"name", "cron_expr", "prompt", "mode", "chat_id", "enabled", "next_run_at", "last_run_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False

    now = datetime.utcnow().isoformat()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [now, job_id]

    conn = _get_conn()
    try:
        conn.execute(
            f"UPDATE cron_jobs SET {set_clause}, updated_at=? WHERE id=?", values
        )
        conn.commit()
        return True
    finally:
        conn.close()


def delete_cron_job(job_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM cron_jobs WHERE id=?", (job_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def log_cron_run(
    job_id: int,
    mode: str,
    success: bool,
    result_text: str | None,
    error: str | None,
    duration_ms: float,
):
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO cron_runs
               (job_id, ran_at, mode, success, result_text, error, duration_ms)
               VALUES (?,?,?,?,?,?,?)""",
            (
                job_id, now, mode, int(success),
                result_text[:3000] if result_text else None,
                error, duration_ms,
            ),
        )
        conn.execute("UPDATE cron_jobs SET last_run_at=? WHERE id=?", (now, job_id))
        conn.commit()
    finally:
        conn.close()


def get_cron_runs(job_id: int, limit: int = 20) -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cron_runs WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_cron_stats() -> dict:
    conn = _get_conn()
    try:
        total   = conn.execute("SELECT COUNT(*) FROM cron_jobs").fetchone()[0] or 0
        active  = conn.execute("SELECT COUNT(*) FROM cron_jobs WHERE enabled=1").fetchone()[0] or 0
        ran_24h = conn.execute(
            "SELECT COUNT(*) FROM cron_runs WHERE ran_at > datetime('now','-24 hours')"
        ).fetchone()[0] or 0
        return {"total": total, "active": active, "ran_24h": ran_24h}
    finally:
        conn.close()
