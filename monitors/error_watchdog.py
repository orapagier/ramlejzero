"""
monitors/error_watchdog.py
───────────────────────────
Watches the local audit DB for failures — zero network calls.
Detects:
  - Agent run failures  (agent_runs.success = 0)
  - Tool call errors    (tool_calls.success = 0)
  - Model status events (model_events table)

Cursor: JSON object storing last-seen row IDs per table.
Only rows with id > last_seen are inspected on each poll,
so this is O(new rows) and uses no CPU when the system is healthy.

Config fields:
  watch_agent_errors  — bool (default: true)
  watch_tool_errors   — bool (default: true)
  watch_model_events  — bool (default: true)
"""
import sqlite3
import json
import os
from monitors.base import MonitorAdapter, MonitorEvent

_AUDIT_DB = "logs/audit.db"


class ErrorWatchdogMonitor(MonitorAdapter):
    monitor_type = "error_watchdog"
    display_name = "Error Watchdog"
    description  = "Watches your own audit DB for agent failures, tool errors, and model events"

    # ── Main poll ─────────────────────────────────────────────────────────

    async def poll(
        self, config: dict, cursor: str | None
    ) -> tuple[list[MonitorEvent], str | None]:
        if not os.path.exists(_AUDIT_DB):
            return [], cursor               # DB not yet created — nothing to watch

        watch_agent = config.get("watch_agent_errors", True)
        watch_tools = config.get("watch_tool_errors", True)
        watch_model = config.get("watch_model_events", True)

        # Decode cursor
        try:
            state = json.loads(cursor) if cursor else {}
        except Exception:
            state = {}

        last_run_id   = state.get("last_run_id",   0)
        last_tool_id  = state.get("last_tool_id",  0)
        last_model_id = state.get("last_model_id", 0)

        events = []
        conn   = self._conn()

        try:
            # ── Agent run failures ─────────────────────────────────────────
            if watch_agent:
                rows = conn.execute(
                    "SELECT * FROM agent_runs WHERE id > ? AND success = 0 ORDER BY id",
                    (last_run_id,),
                ).fetchall()
                for row in rows:
                    last_run_id = max(last_run_id, row["id"])
                    events.append(MonitorEvent(
                        title     = "❌ Agent Run Failed",
                        body      = (
                            f"User message: {str(row['user_message'])[:120]}\n"
                            f"Model: {row['model_used']}\n"
                            f"Error: {row['error'] or 'unknown'}\n"
                            f"Duration: {row['duration_ms']:.0f}ms"
                        ),
                        source_id = str(row["id"]),
                        metadata  = dict(row),
                        severity  = "error",
                    ))

            # ── Tool call failures ─────────────────────────────────────────
            if watch_tools:
                rows = conn.execute(
                    """
                    SELECT tc.*, ar.user_message AS _user_message
                    FROM tool_calls tc
                    JOIN agent_runs ar ON ar.id = tc.run_id
                    WHERE tc.id > ? AND tc.success = 0
                    ORDER BY tc.id
                    """,
                    (last_tool_id,),
                ).fetchall()
                for row in rows:
                    last_tool_id = max(last_tool_id, row["id"])
                    events.append(MonitorEvent(
                        title     = f"🔧 Tool Error: {row['tool_name']}",
                        body      = (
                            f"Tool: {row['tool_name']}\n"
                            f"Error: {row['error'] or 'unknown'}\n"
                            f"Duration: {row['duration_ms']:.0f}ms\n"
                            f"Triggered by: {str(row['_user_message'])[:80]}"
                        ),
                        source_id = str(row["id"]),
                        metadata  = {k: v for k, v in dict(row).items() if not k.startswith("_")},
                        severity  = "warn",
                    ))

            # ── Model status events ────────────────────────────────────────
            if watch_model:
                rows = conn.execute(
                    "SELECT * FROM model_events WHERE id > ? ORDER BY id",
                    (last_model_id,),
                ).fetchall()
                for row in rows:
                    last_model_id = max(last_model_id, row["id"])
                    severity = "error" if "rate_limit" in row["event"].lower() else "warn"
                    events.append(MonitorEvent(
                        title     = f"🤖 Model Event: {row['event']}",
                        body      = (
                            f"Model: {row['model_name']}\n"
                            f"Event: {row['event']}\n"
                            f"{row['detail'] or ''}"
                        ),
                        source_id = str(row["id"]),
                        metadata  = dict(row),
                        severity  = severity,
                    ))

        finally:
            conn.close()

        new_cursor = json.dumps({
            "last_run_id":   last_run_id,
            "last_tool_id":  last_tool_id,
            "last_model_id": last_model_id,
        })
        return events, new_cursor

    # ── Dashboard schema ──────────────────────────────────────────────────

    @classmethod
    def schema(cls) -> dict:
        return {
            "fields": [
                {
                    "key": "watch_agent_errors", "label": "Watch Agent Run Failures",
                    "type": "checkbox", "default": True, "required": False,
                },
                {
                    "key": "watch_tool_errors", "label": "Watch Tool Call Errors",
                    "type": "checkbox", "default": True, "required": False,
                },
                {
                    "key": "watch_model_events", "label": "Watch Model Status Events",
                    "type": "checkbox", "default": True, "required": False,
                    "help": "Rate limits, unavailability, recovery",
                },
            ]
        }

    # ── Helpers ───────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(_AUDIT_DB)
        conn.row_factory = sqlite3.Row
        return conn

    async def test(self, config: dict) -> dict:
        if not os.path.exists(_AUDIT_DB):
            return {"ok": False, "message": f"Audit DB not found at {_AUDIT_DB}"}
        try:
            conn = self._conn()
            conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()
            conn.close()
            return {"ok": True, "message": "Audit DB accessible. Watchdog ready."}
        except Exception as e:
            return {"ok": False, "message": str(e)}
