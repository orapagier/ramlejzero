"""
monitors/__init__.py
─────────────────────
Central registry of all monitor adapters.

Uses a sys.path anchor so Python always resolves imports to THIS local
package — not any installed 'monitors' package elsewhere on the system.

To add a new adapter:
  1. Create monitors/my_monitor.py with a class MyMonitor(MonitorAdapter)
  2. Import and register it here with _register(MyMonitor)
  3. No other code changes needed — dashboard picks it up automatically.
"""
import sys
import os

# Ensure the project root is always first on sys.path.
# Prevents any installed 'monitors' package shadowing our local one.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from monitors.base import MonitorAdapter, MonitorEvent
from monitors.facebook_page import FacebookPageMonitor
from monitors.gmail_monitor import GmailMonitor
from monitors.error_watchdog import ErrorWatchdogMonitor
from monitors.tool_health import ToolHealthMonitor

_REGISTRY: dict[str, MonitorAdapter] = {}


def _register(cls: type[MonitorAdapter]):
    instance = cls()
    _REGISTRY[instance.monitor_type] = instance


_register(FacebookPageMonitor)
_register(GmailMonitor)
_register(ErrorWatchdogMonitor)
_register(ToolHealthMonitor)


def get_adapter(monitor_type: str) -> MonitorAdapter | None:
    """Return adapter instance for the given type slug, or None."""
    return _REGISTRY.get(monitor_type)


def get_all_types() -> list[dict]:
    """Return metadata for all registered adapters — used by dashboard."""
    return [
        {
            "type":         a.monitor_type,
            "display_name": a.display_name,
            "description":  a.description,
            "schema":       a.schema(),
        }
        for a in _REGISTRY.values()
    ]
