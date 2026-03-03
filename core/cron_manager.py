"""
core/cron_manager.py
─────────────────────
APScheduler-backed cron job system.
All jobs are DB-backed and hot-reloadable from the dashboard — no restart needed.

Job Modes
─────────
  autonomous : Agent runs immediately at the scheduled time.
               Result is sent to Telegram automatically.
               Example: "Every morning, summarize my overnight emails."

  approval   : Sends a Telegram notification that a task is ready.
               User clicks "Run Now" in the dashboard to execute.
               Example: "Every Sunday, prepare the weekly report" — but review first.

Both modes log to cron_runs for full history.

Installation
────────────
Requires: pip install apscheduler
If apscheduler is missing, cron jobs are silently disabled (monitors still work).
"""
import asyncio
import time
from datetime import datetime
from core import monitor_db
from core.logger import get_logger

logger = get_logger("cron_manager")

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _HAS_APSCHEDULER = True
except ImportError:
    _HAS_APSCHEDULER = False
    logger.warning(
        "apscheduler not installed — cron jobs disabled. "
        "Install with: pip install apscheduler"
    )


class CronManager:

    def __init__(self):
        self._scheduler: "AsyncIOScheduler | None" = None
        self._platform  = None
        self._primary_chat_id: str | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def set_platform(self, platform, primary_chat_id: str | None):
        self._platform        = platform
        self._primary_chat_id = primary_chat_id

    async def start(self):
        if not _HAS_APSCHEDULER:
            logger.warning("Cron manager skipped — apscheduler not installed")
            return

        self._scheduler = AsyncIOScheduler(timezone="UTC")
        jobs = monitor_db.get_all_cron_jobs()
        added = 0
        for job in jobs:
            if job.get("enabled", 1):
                if self._add_apjob(job):
                    added += 1

        self._scheduler.start()
        logger.info(f"Cron manager started — {added} active job(s)")

    async def stop(self):
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Cron manager stopped")

    def reload(self):
        """
        Hot-reload all jobs from DB.
        Call after any dashboard create/update/delete/toggle operation.
        """
        if not self._scheduler:
            return

        # Remove all existing scheduled jobs
        for apjob in self._scheduler.get_jobs():
            apjob.remove()

        # Re-add from DB
        jobs  = monitor_db.get_all_cron_jobs()
        added = 0
        for job in jobs:
            if job.get("enabled", 1):
                if self._add_apjob(job):
                    added += 1

        logger.info(f"Cron manager reloaded — {added} active job(s)")

    # ── APScheduler job management ────────────────────────────────────────

    def _add_apjob(self, job: dict) -> bool:
        """Add one job to APScheduler. Returns True on success."""
        try:
            trigger = CronTrigger.from_crontab(job["cron_expr"], timezone="UTC")
        except Exception as e:
            logger.error(
                f"Invalid cron expression '{job['cron_expr']}' for job "
                f"[{job['id']}] '{job['name']}': {e}"
            )
            return False

        self._scheduler.add_job(
            self._fire_job,
            trigger         = trigger,
            args            = [job["id"]],
            id              = f"cron_{job['id']}",
            name            = job["name"],
            replace_existing= True,
            misfire_grace_time = 300,       # 5-min grace window if server was down
        )

        # Persist next_run_at for dashboard display
        apjob = self._scheduler.get_job(f"cron_{job['id']}")
        if apjob and apjob.next_run_time:
            monitor_db.update_cron_job(job["id"], next_run_at=apjob.next_run_time.isoformat())

        return True

    # ── Job execution ─────────────────────────────────────────────────────

    async def _fire_job(self, job_id: int):
        """Called by APScheduler at the scheduled time."""
        job = monitor_db.get_cron_job(job_id)
        if not job or not job.get("enabled", 1):
            return

        logger.info(f"Cron job firing: [{job_id}] '{job['name']}' (mode={job['mode']})")

        if job["mode"] == "autonomous":
            await self._run_autonomous(job)
        else:
            await self._notify_approval_needed(job)

        # Update next_run_at in DB
        apjob = self._scheduler.get_job(f"cron_{job_id}") if self._scheduler else None
        if apjob and apjob.next_run_time:
            monitor_db.update_cron_job(job_id, next_run_at=apjob.next_run_time.isoformat())

    async def _run_autonomous(self, job: dict):
        """
        Run the agent with the job prompt, send result to Telegram.
        This is called both by the scheduler and by "Run Now" from the dashboard.
        """
        import agent as agent_module

        chat_id = job.get("chat_id") or self._primary_chat_id
        t0      = time.time()

        try:
            response = await agent_module.run(
                user_message         = job["prompt"],
                user_id              = 0,   # system user
                conversation_history = [],
            )
            duration_ms  = (time.time() - t0) * 1000
            result_text  = response.text or "(no response)"

            monitor_db.log_cron_run(
                job["id"], "autonomous", response.success,
                result_text, response.error, duration_ms,
            )

            if chat_id and self._platform:
                msg = (
                    f"⏰ *Scheduled Task: {job['name']}*\n"
                    f"_Ran automatically — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_\n\n"
                    f"{result_text[:3500]}"
                )
                await self._platform.send_message(str(chat_id), msg)

        except Exception as e:
            duration_ms = (time.time() - t0) * 1000
            logger.error(f"Cron job [{job['id']}] run failed: {e}", exc_info=True)
            monitor_db.log_cron_run(
                job["id"], "autonomous", False, None, str(e), duration_ms
            )
            if chat_id and self._platform:
                await self._platform.send_message(
                    str(chat_id),
                    f"❌ *Scheduled Task Failed: {job['name']}*\n\nError: {str(e)[:500]}",
                )

    async def _notify_approval_needed(self, job: dict):
        """
        Approval mode: notify the user that a task is ready to run.
        User goes to Dashboard → Cron Jobs → Run Now to execute.
        """
        chat_id = job.get("chat_id") or self._primary_chat_id

        monitor_db.log_cron_run(
            job["id"], "approval_pending", True,
            "Approval notification sent — awaiting manual trigger",
            None, 0,
        )

        if chat_id and self._platform:
            msg = (
                f"⏰ *Scheduled Task Ready: {job['name']}*\n\n"
                f"Prompt preview:\n_{job['prompt'][:250]}_\n\n"
                f"This job requires your approval.\n"
                f"Go to *Dashboard → Cron Jobs* and press *Run Now* to execute."
            )
            await self._platform.send_message(str(chat_id), msg)

    # ── Dashboard: Run Now ─────────────────────────────────────────────────

    async def run_job_now(self, job_id: int):
        """Manual trigger from dashboard — always executes as autonomous."""
        job = monitor_db.get_cron_job(job_id)
        if not job:
            raise ValueError(f"Cron job {job_id} not found")
        logger.info(f"Manual run: [{job_id}] '{job['name']}'")
        await self._run_autonomous(job)

    # ── Status for dashboard ───────────────────────────────────────────────

    def get_all_statuses(self) -> list[dict]:
        """
        Return DB jobs enriched with live next_run_time from APScheduler.
        Used by GET /api/cron endpoint.
        """
        jobs = monitor_db.get_all_cron_jobs()
        if not self._scheduler:
            return jobs

        result = []
        for job in jobs:
            apjob = self._scheduler.get_job(f"cron_{job['id']}")
            if apjob and apjob.next_run_time:
                job["next_run_at"] = apjob.next_run_time.isoformat()
            result.append(job)
        return result


# ── Singleton ──────────────────────────────────────────────────────────────

_manager: CronManager | None = None


def get_manager() -> CronManager:
    global _manager
    if _manager is None:
        _manager = CronManager()
    return _manager
