import logging
import logging.handlers
import json
import sqlite3
import os
from datetime import datetime
from core.schemas import AgentRunLog, ToolCallLog
from core.config_loader import get_settings

_logger: logging.Logger = None
_db_path: str = None


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log = {
            "ts": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        if hasattr(record, "data"):
            log["data"] = record.data
        return json.dumps(log)


def setup() -> logging.Logger:
    global _logger, _db_path

    # Guard — only initialize once, no matter how many times setup() is called
    if _logger is not None:
        return _logger

    settings = get_settings()
    log_cfg = settings.get("logging", {})

    os.makedirs("logs", exist_ok=True)

    # Use a named top-level logger instead of root so uvicorn/fastapi
    # don't inherit our handlers and double-print everything
    base = logging.getLogger("app")
    base.setLevel(getattr(logging, log_cfg.get("level", "INFO")))
    base.propagate = False  # don't bubble up to root — stops duplication

    # Console — human readable
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    base.addHandler(console)

    # Rotating file — JSON structured
    log_file = log_cfg.get("file", "logs/agent.log")
    max_mb = log_cfg.get("max_log_size_mb", 50)
    backups = log_cfg.get("backup_count", 5)
    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backups
    )
    fh.setFormatter(JSONFormatter())
    base.addHandler(fh)

    # Silence noisy libraries
    for noisy in ["httpx", "httpcore", "telegram", "asyncio", "googleapiclient"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _logger = logging.getLogger("app.agent")

    # Init audit DB
    _db_path = log_cfg.get("audit_db", "logs/audit.db")
    _init_db()

    _logger.info("Logging initialized")
    return _logger


def get_logger(name: str = "agent") -> logging.Logger:
    # All loggers are children of "app" so they inherit its handlers
    # but not the root logger's — preventing duplication
    return logging.getLogger(f"app.{name}")


def _init_db():
    conn = sqlite3.connect(_db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agent_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            user_id     INTEGER,
            user_message TEXT,
            agent_response TEXT,
            model_used  TEXT,
            iterations  INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            duration_ms REAL,
            success     INTEGER,
            error       TEXT
        );

        CREATE TABLE IF NOT EXISTS tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER,
            timestamp   TEXT,
            tool_name   TEXT,
            input_params TEXT,
            result      TEXT,
            success     INTEGER,
            duration_ms REAL,
            error       TEXT,
            FOREIGN KEY (run_id) REFERENCES agent_runs(id)
        );

        CREATE TABLE IF NOT EXISTS model_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            model_name  TEXT,
            event       TEXT,
            detail      TEXT
        );

        CREATE TABLE IF NOT EXISTS router_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            user_id     INTEGER,
            user_message TEXT,
            selected_tools TEXT,
            model_used  TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            duration_ms REAL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_user ON agent_runs(user_id);
        CREATE INDEX IF NOT EXISTS idx_runs_ts   ON agent_runs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tools_run ON tool_calls(run_id);
        CREATE INDEX IF NOT EXISTS idx_router_ts ON router_logs(timestamp);
    """)
    conn.commit()
    conn.close()


def log_router_run(user_id: int, user_message: str, selected_tools: list[str], model: str, in_tokens: int, out_tokens: int, duration_ms: float):
    """Write a tool router call to the audit database."""
    if not _db_path:
        return
    conn = sqlite3.connect(_db_path)
    try:
        conn.execute(
            """INSERT INTO router_logs
               (timestamp, user_id, user_message, selected_tools, model_used,
                input_tokens, output_tokens, duration_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                datetime.utcnow().isoformat(),
                user_id,
                user_message,
                ",".join(selected_tools) if selected_tools else "NONE",
                model,
                in_tokens,
                out_tokens,
                duration_ms
            )
        )
        conn.commit()
    finally:
        conn.close()


def log_agent_run(user_id: int, run_log: AgentRunLog):
    """Write a completed agent run to the audit database."""
    if not _db_path:
        return
    conn = sqlite3.connect(_db_path)
    try:
        r = run_log.response
        cur = conn.execute(
            """INSERT INTO agent_runs
               (timestamp, user_id, user_message, agent_response, model_used,
                iterations, input_tokens, output_tokens, duration_ms, success, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_log.timestamp.isoformat(),
                user_id,
                run_log.user_message,
                r.text[:2000],
                r.model_used,
                r.iterations,
                r.total_input_tokens,
                r.total_output_tokens,
                r.duration_ms,
                int(r.success),
                r.error
            )
        )
        run_id = cur.lastrowid
        for tc in r.tool_calls:
            conn.execute(
                """INSERT INTO tool_calls
                   (run_id, timestamp, tool_name, input_params, result, success, duration_ms, error)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    tc.timestamp.isoformat(),
                    tc.tool_name,
                    json.dumps(tc.input_params),
                    tc.result_text[:1000],
                    int(tc.success),
                    tc.duration_ms,
                    tc.error
                )
            )
        conn.commit()
    finally:
        conn.close()


def log_model_event(model_name: str, event: str, detail: str = ""):
    """Log model status changes — rate limits, errors, recovery."""
    logger = get_logger("model_router")
    logger.warning(f"Model event | {model_name} | {event} | {detail}")
    if not _db_path:
        return
    conn = sqlite3.connect(_db_path)
    conn.execute(
        "INSERT INTO model_events (timestamp, model_name, event, detail) VALUES (?,?,?,?)",
        (datetime.utcnow().isoformat(), model_name, event, detail)
    )
    conn.commit()
    conn.close()


def get_total_stats() -> dict:
    """Sum up runs and tokens across agent and router."""
    if not _db_path or not os.path.exists(_db_path):
        return {"runs": 0, "tokens": 0}
    conn = sqlite3.connect(_db_path)
    try:
        # Agent tokens
        r1 = conn.execute("SELECT COUNT(*), SUM(input_tokens + output_tokens) FROM agent_runs").fetchone()
        agent_runs = r1[0] or 0
        agent_tokens = r1[1] or 0

        # Router tokens
        r2 = conn.execute("SELECT SUM(input_tokens + output_tokens) FROM router_logs").fetchone()
        router_tokens = r2[0] or 0

        return {
            "runs": agent_runs,
            "tokens": agent_tokens + router_tokens
        }
    finally:
        conn.close()


def get_recent_runs(limit: int = 20) -> list[dict]:
    """Fetch recent agent runs for the Web UI."""
    if not _db_path or not os.path.exists(_db_path):
        return []
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, timestamp, user_id, user_message, model_used,
                  iterations, input_tokens, output_tokens, duration_ms, success, error
           FROM agent_runs ORDER BY id DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_run_tools(run_id: int) -> list[dict]:
    """Fetch tool calls for a specific run."""
    if not _db_path:
        return []
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id",
        (run_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_model_events(limit: int = 50) -> list[dict]:
    """Fetch recent model events for the Web UI."""
    if not _db_path or not os.path.exists(_db_path):
        return []
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM model_events ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recent_router_runs(limit: int = 50) -> list[dict]:
    """Fetch recent router (tiny LLM) calls for the Web UI."""
    if not _db_path or not os.path.exists(_db_path):
        return []
    conn = sqlite3.connect(_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM router_logs ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
