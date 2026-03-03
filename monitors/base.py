"""
monitors/base.py
────────────────
Abstract base class for all monitor adapters.

INTERFACE CONTRACT
──────────────────
Every adapter must implement:
  poll(config, cursor) → (events, new_cursor)
  schema()             → dict   ← used by dashboard to render config form

Optionally override:
  test(config) → {"ok": bool, "message": str}

CURSOR PATTERN
──────────────
The cursor is an opaque string persisted in monitor_state.cursor.
It represents "everything up to here has been seen."
Each poll receives the previous cursor and returns a new one.

Examples:
  Facebook messages  → ISO timestamp of last updated_time
  Gmail             → comma-separated message IDs (last 200)
  Error watchdog    → JSON {"last_run_id": N, "last_tool_id": M, "last_model_id": K}
  Tool health       → JSON {"https://url": "up"|"down", ...}
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class MonitorEvent:
    """A single detected change / new item."""
    title:     str                          # headline for Telegram notification
    body:      str                          # full detail text
    source_id: str = ""                     # platform-side ID (message_id, run_id, url)
    metadata:  dict = field(default_factory=dict)
    severity:  str = "info"                 # info | warn | error


class MonitorAdapter(ABC):
    """
    Base class for all monitor adapters.
    Subclass and register in monitors/__init__.py.
    """
    monitor_type: str = "base"              # unique slug — override in subclass
    display_name: str = "Monitor"           # shown in dashboard type selector
    description:  str = ""                  # one-liner for dashboard

    # ── Required ──────────────────────────────────────────────────────────

    @abstractmethod
    async def poll(
        self,
        config: dict,
        cursor: str | None,
    ) -> tuple[list[MonitorEvent], str | None]:
        """
        Check for new events since cursor.

        Args:
            config : user config dict stored in DB
            cursor : last-persisted cursor, None on first run

        Returns:
            (events, new_cursor)
              events      — list of MonitorEvent (empty = nothing new)
              new_cursor  — updated cursor to persist, None = no change
        """
        ...

    @classmethod
    @abstractmethod
    def schema(cls) -> dict:
        """
        JSON-serializable schema for dashboard form generation.

        Format:
        {
          "fields": [
            {
              "key":      "field_key",
              "label":    "Display Label",
              "type":     "text|number|checkbox|select|multiselect|textarea|targets_list",
              "default":  <value>,
              "required": bool,
              "help":     "Optional hint text",
              "options":  ["a","b"]     # only for select/multiselect
            },
            ...
          ]
        }
        """
        ...

    # ── Optional ──────────────────────────────────────────────────────────

    async def test(self, config: dict) -> dict:
        """Quick connectivity test from dashboard. Override for richer feedback."""
        try:
            events, _ = await self.poll(config, None)
            return {
                "ok": True,
                "message": f"Connected. Found {len(events)} item(s) on first check.",
            }
        except Exception as e:
            return {"ok": False, "message": str(e)}
