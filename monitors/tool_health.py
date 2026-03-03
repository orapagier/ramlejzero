"""
monitors/tool_health.py
────────────────────────
Pings configured URLs and fires events only on STATE CHANGES:
  up → down : 🔴 alert
  down → up  : 🟢 recovery notice

No events when state is stable — zero noise when everything is fine.
Uses aiohttp for concurrent async HTTP checks.

Config fields:
  targets — list of {name, url, expected_status (default 200), timeout_s (default 5)}

Cursor: JSON {"url": "up"|"down"}
First run just records initial state — no alert on first poll.
"""
import json
import asyncio
from monitors.base import MonitorAdapter, MonitorEvent

try:
    import aiohttp
    _HAS_AIOHTTP = True
except ImportError:
    _HAS_AIOHTTP = False


class ToolHealthMonitor(MonitorAdapter):
    monitor_type = "tool_health"
    display_name = "Tool Health / Uptime"
    description  = "Pings URLs and alerts on state changes (up↔down)"

    # ── Main poll ─────────────────────────────────────────────────────────

    async def poll(
        self, config: dict, cursor: str | None
    ) -> tuple[list[MonitorEvent], str | None]:
        if not _HAS_AIOHTTP:
            raise RuntimeError("aiohttp not installed — run: pip install aiohttp")

        targets = config.get("targets", [])
        if not targets:
            return [], cursor

        try:
            prev_states: dict = json.loads(cursor) if cursor else {}
        except Exception:
            prev_states = {}

        events     = []
        new_states = dict(prev_states)

        # Check all targets concurrently
        async with aiohttp.ClientSession() as session:
            tasks   = [self._check_target(session, t) for t in targets]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for target, result in zip(targets, results):
            name = target.get("name") or target.get("url", "?")
            url  = target.get("url", "")

            if isinstance(result, Exception):
                current = "down"
                detail  = str(result)
            else:
                current, detail = result

            prev = prev_states.get(url)
            new_states[url] = current

            if prev is None:
                # First run — record baseline, no alert
                continue

            if prev == "up" and current == "down":
                events.append(MonitorEvent(
                    title     = f"🔴 {name} is DOWN",
                    body      = f"URL: {url}\nReason: {detail}",
                    source_id = url,
                    metadata  = {"url": url, "name": name, "state": "down", "detail": detail},
                    severity  = "error",
                ))
            elif prev == "down" and current == "up":
                events.append(MonitorEvent(
                    title     = f"🟢 {name} is back UP",
                    body      = f"URL: {url}\nStatus: {detail}",
                    source_id = url,
                    metadata  = {"url": url, "name": name, "state": "up", "detail": detail},
                    severity  = "info",
                ))

        new_cursor = json.dumps(new_states)
        return events, new_cursor

    # ── Individual target check ───────────────────────────────────────────

    async def _check_target(
        self, session: "aiohttp.ClientSession", target: dict
    ) -> tuple[str, str]:
        url      = target.get("url", "")
        expected = int(target.get("expected_status", 200))
        timeout  = float(target.get("timeout_s", 5))

        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=True,
                ssl=False,                  # don't fail on self-signed certs
            ) as resp:
                if resp.status == expected:
                    return "up", f"HTTP {resp.status}"
                return "down", f"HTTP {resp.status} (expected {expected})"
        except asyncio.TimeoutError:
            return "down", f"Timeout after {timeout}s"
        except Exception as e:
            return "down", str(e)

    # ── Test override ─────────────────────────────────────────────────────

    async def test(self, config: dict) -> dict:
        targets = config.get("targets", [])
        if not targets:
            return {"ok": False, "message": "No targets configured."}
        try:
            async with aiohttp.ClientSession() as session:
                tasks   = [self._check_target(session, t) for t in targets]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            lines = []
            for t, r in zip(targets, results):
                name = t.get("name") or t.get("url", "?")
                if isinstance(r, Exception):
                    lines.append(f"❌ {name}: {r}")
                else:
                    icon = "✅" if r[0] == "up" else "❌"
                    lines.append(f"{icon} {name}: {r[1]}")
            return {"ok": True, "message": "\n".join(lines)}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    # ── Dashboard schema ──────────────────────────────────────────────────

    @classmethod
    def schema(cls) -> dict:
        return {
            "fields": [
                {
                    "key":      "targets",
                    "label":    "URL Targets",
                    "type":     "targets_list",       # rendered as dynamic table in dashboard
                    "default":  [],
                    "required": True,
                    "help":     "Each row: Name, URL, Expected HTTP Status, Timeout (s)",
                },
            ]
        }
