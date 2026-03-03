"""
core/monitor_engine.py
───────────────────────
The heart of the proactive monitoring system.

Architecture
────────────
Each enabled monitor gets its own asyncio.Task (a long-running coroutine).
The task sleeps for interval_s, wakes up, calls adapter.poll(), dispatches
events, saves the cursor, then goes back to sleep. Zero threads, zero
subprocesses — all sharing the same event loop as FastAPI/Uvicorn.

Memory/CPU impact: ~4KB per monitor task + negligible asyncio overhead.
CPU spikes only during actual network calls (milliseconds, then back to 0).

Event Actions
─────────────
Each monitor has an `actions` JSON array. Supported action types:

  notify_telegram:
    Sends the event title+body to Telegram.
    Optional: {"type": "notify_telegram", "chat_id": "123456"}
              (omit chat_id to use the primary user from settings.yaml)

  run_agent:
    Feeds a prompt (with event details interpolated) to the AI agent,
    then sends the agent's response to Telegram.
    {"type": "run_agent",
     "prompt_template": "New FB message:\n\n{body}\n\nSuggest a reply.",
     "chat_id": null}

    Template variables: {title}, {body}, {severity}, {monitor_name}

Hot Reload
──────────
Call engine.reload() after any monitor CRUD operation in the dashboard.
It cancels stale tasks and starts new ones without restarting the server.
"""
import asyncio
import json
import sys
import os
import time

# ── Ensure project root is on sys.path so 'monitors' package resolves ─────
# core/ is a subdirectory; Python may not find top-level monitors/ without this.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# ──────────────────────────────────────────────────────────────────────────

from core import monitor_db
from core.logger import get_logger

# Lazy helpers — resolved at first call, not at module-load time.
# This avoids ImportError if monitors/ isn't on sys.path during the very
# first import of this module (e.g. in circular-import edge cases).
def _get_adapter(monitor_type: str):
    from monitors import get_adapter  # noqa
    return get_adapter(monitor_type)

logger = get_logger("monitor_engine")

# Startup jitter cap — spreads initial polls so they don't all fire at once
_JITTER_CAP_S = 20


class MonitorEngine:

    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}   # monitor_id → Task
        self._platform  = None
        self._primary_chat_id: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def set_platform(self, platform, primary_chat_id: str | None):
        self._platform       = platform
        self._primary_chat_id = primary_chat_id

    async def start(self):
        monitors = monitor_db.get_all_monitors()
        for m in monitors:
            if m.get("enabled", 1):
                self._start_task(m)
        active = len(self._tasks)
        logger.info(f"Monitor engine started — {active} active monitor(s)")

    async def stop(self):
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info("Monitor engine stopped")

    def reload(self):
        """Hot-reload after dashboard changes. Schedules async work."""
        asyncio.create_task(self._async_reload(), name="monitor-hot-reload")

    async def _async_reload(self):
        monitors = monitor_db.get_all_monitors()
        db_ids   = {m["id"]: m for m in monitors}

        # Stop tasks for deleted monitors
        for mid in list(self._tasks):
            if mid not in db_ids:
                self._cancel_task(mid)

        # Restart changed/enabled, stop disabled
        for mid, m in db_ids.items():
            if m.get("enabled", 1):
                # Always restart — picks up any config change
                self._cancel_task(mid)
                self._start_task(m)
            else:
                self._cancel_task(mid)

        logger.info(f"Monitor engine reloaded — {len(self._tasks)} active monitor(s)")

    # ── Task management ───────────────────────────────────────────────────

    def _start_task(self, m: dict):
        mid  = m["id"]
        task = asyncio.create_task(
            self._poll_loop(m),
            name=f"mon-{mid}-{m['name'][:20]}",
        )
        task.add_done_callback(lambda t: self._on_task_done(mid, t))
        self._tasks[mid] = task

    def _cancel_task(self, monitor_id: int):
        task = self._tasks.pop(monitor_id, None)
        if task and not task.done():
            task.cancel()

    def _on_task_done(self, monitor_id: int, task: asyncio.Task):
        """Called when a poll loop exits (normally = monitor disabled; error = bug)."""
        self._tasks.pop(monitor_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Monitor task [{monitor_id}] died unexpectedly: {exc}", exc_info=exc)

    # ── Poll loop ─────────────────────────────────────────────────────────

    async def _poll_loop(self, m: dict):
        """
        Long-running coroutine for a single monitor.
        Pattern: jitter → poll → sleep(interval) → poll → ...
        """
        monitor_id = m["id"]
        interval   = m.get("interval_s", 60)

        # Stagger startup so 10 monitors don't all hit APIs simultaneously
        jitter = (monitor_id % _JITTER_CAP_S) + 2
        await asyncio.sleep(jitter)

        while True:
            try:
                await self._run_once(monitor_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Unhandled error in poll loop [{monitor_id}]: {e}", exc_info=True)

            # Re-read interval from DB so dashboard changes take effect immediately
            fresh = monitor_db.get_monitor(monitor_id)
            if not fresh or not fresh.get("enabled", 1):
                logger.info(f"Monitor [{monitor_id}] disabled — loop exiting")
                break

            await asyncio.sleep(fresh.get("interval_s", interval))

    # ── Single poll ───────────────────────────────────────────────────────

    async def _run_once(self, monitor_id: int):
        m = monitor_db.get_monitor(monitor_id)
        if not m:
            return

        adapter = _get_adapter(m["type"])
        if not adapter:
            logger.warning(f"No adapter registered for type '{m['type']}' — skipping [{monitor_id}]")
            return

        config     = json.loads(m.get("config",  "{}"))
        actions    = json.loads(m.get("actions", "[]"))
        state      = monitor_db.get_monitor_state(monitor_id)
        cursor     = state.get("cursor")

        t0         = time.time()
        events     = []
        new_cursor = cursor
        success    = True
        error_msg  = None

        try:
            events, new_cursor = await adapter.poll(config, cursor)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            success   = False
            error_msg = str(e)
            logger.warning(f"Poll error [{monitor_id}] {m['name']}: {e}")

        duration_ms   = (time.time() - t0) * 1000
        actions_taken = 0

        if events:
            logger.info(
                f"Monitor [{monitor_id}] {m['name']}: {len(events)} new event(s)"
            )
            for event in events:
                taken = await self._dispatch(event, actions, m)
                actions_taken += taken

        monitor_db.save_monitor_state(monitor_id, new_cursor, success)
        monitor_db.log_monitor_run(
            monitor_id, len(events), actions_taken,
            success, error_msg, duration_ms,
        )

    # ── Action dispatch ───────────────────────────────────────────────────

    async def _dispatch(
        self,
        event,          # MonitorEvent
        actions: list,
        monitor: dict,
    ) -> int:
        """Dispatch one event to all configured actions. Returns actions-taken count."""
        taken = 0
        for action in actions:
            atype = action.get("type")
            try:
                if atype == "notify_telegram":
                    await self._action_notify(event, action, monitor)
                    taken += 1
                elif atype == "run_agent":
                    await self._action_run_agent(event, action, monitor)
                    taken += 1
                else:
                    logger.warning(f"Unknown action type: '{atype}'")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Action '{atype}' dispatch error: {e}", exc_info=True)
        return taken

    async def _action_notify(
        self,
        event,          # MonitorEvent
        action:  dict,
        monitor: dict,
    ):
        """Send event as a Telegram notification."""
        if not self._platform:
            logger.warning("No messaging platform — cannot send notification")
            return

        chat_id = action.get("chat_id") or self._primary_chat_id
        if not chat_id:
            logger.warning("notify_telegram action has no chat_id and no primary_user_id is set")
            return

        severity_prefix = {
            "error": "🔴",
            "warn":  "🟡",
            "info":  "🔵",
        }.get(event.severity, "🔵")

        msg = (
            f"{severity_prefix} *{event.title}*\n"
            f"_Monitor: {monitor['name']}_\n\n"
            f"{event.body[:3000]}"
        )
        await self._platform.send_message(str(chat_id), msg)

    async def _action_run_agent(
        self,
        event,          # MonitorEvent
        action:  dict,
        monitor: dict,
    ):
        """Run the AI agent with the event details and send the response to Telegram."""
        import agent as agent_module

        chat_id = action.get("chat_id") or self._primary_chat_id
        template = action.get(
            "prompt_template",
            "New event from monitor '{monitor_name}':\n\n"
            "Title: {title}\n\n"
            "{body}\n\n"
            "Please analyse this and suggest any action needed.",
        )

        prompt = template.format(
            monitor_name = monitor["name"],
            title        = event.title,
            body         = event.body,
            severity     = event.severity,
        )

        try:
            response = await agent_module.run(
                user_message         = prompt,
                user_id              = 0,   # system-initiated, no conversation memory
                conversation_history = [],
            )
            if chat_id and response.text and self._platform:
                reply = (
                    f"🤖 *Agent Response*\n"
                    f"_Triggered by: {monitor['name']}_\n\n"
                    f"{response.text[:3500]}"
                )
                await self._platform.send_message(str(chat_id), reply)
        except Exception as e:
            logger.error(f"run_agent action failed: {e}", exc_info=True)

    # ── Manual trigger (dashboard "Run Now") ─────────────────────────────

    async def run_monitor_now(self, monitor_id: int):
        """Force an immediate poll cycle — called from dashboard."""
        await self._run_once(monitor_id)


# ── Singleton ──────────────────────────────────────────────────────────────

_engine: MonitorEngine | None = None


def get_engine() -> MonitorEngine:
    global _engine
    if _engine is None:
        _engine = MonitorEngine()
    return _engine
