"""
core/scheduler.py
──────────────────
Startup wiring for the entire proactive agent system.
Called once from main.py's lifespan context manager.

Usage in main.py lifespan:
──────────────────────────
    from core import scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        platform = get_platform()
        ...
        await platform.start(app)

        # ── ADD THESE TWO LINES ──
        _engine, _cron = await scheduler.start(platform)
        app.state.monitor_engine = _engine
        app.state.cron_manager   = _cron
        # ────────────────────────

        yield

        await scheduler.stop(_engine, _cron)
        await platform.stop()
"""
import asyncio
import sys
import os

# ── Path anchor ────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
# ──────────────────────────────────────────────────────────────────────────

from core import monitor_db
from core.monitor_engine import get_engine
from core.cron_manager import get_manager
from core.logger import get_logger
from core.config_loader import get_settings

logger = get_logger("scheduler")


async def start(platform) -> tuple:
    """
    Initialise and start all background systems.

    Args:
        platform: the active MessagingPlatform instance (for sending notifications)

    Returns:
        (MonitorEngine, CronManager) — store on app.state for API route access
    """
    # 1. Ensure DB tables exist (safe to call every startup)
    monitor_db.init_tables()

    # 2. Resolve the primary notification target
    settings         = get_settings()
    primary_user_id  = str(
        settings.get("messaging", {}).get("primary_user_id", "") or ""
    )

    # 3. Start monitor engine
    engine = get_engine()
    engine.set_platform(platform, primary_user_id or None)
    await engine.start()

    # 4. Start cron manager
    cron = get_manager()
    cron.set_platform(platform, primary_user_id or None)
    await cron.start()

    logger.info("✅ Proactive agent scheduler fully started")
    return engine, cron


async def stop(engine, cron):
    """Graceful shutdown — call during lifespan teardown."""
    await asyncio.gather(
        engine.stop(),
        cron.stop(),
        return_exceptions=True,
    )
    logger.info("Scheduler stopped")
