"""usage_meter.py: the Usage Meter add-on for proxy.py.

Keeps the latest `anthropic-ratelimit-unified-*` response headers (how much of the
subscription's 5-hour and 7-day windows is used) and serves them to the dashboard.
Standard library only. proxy.py registers it with two lines:

    import usage_meter
    usage_meter.install(HUB, Tap)

Read-only: it looks at a reply's headers only after the hub has finished the
exchange, which happens after the reply was fully passed back to the agent, so
the traffic is never changed or delayed.

    GET /__usage            -> {"headers": {name: value}, "updated": ISO time or null}
    GET /__usage-meter.js   -> the dashboard component (usage_meter.js)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "anthropic-ratelimit-unified-"
SCRIPT = Path(__file__).resolve().parent / "usage_meter.js"


def unified(headers) -> dict[str, str]:
    """The rate-limit headers in a response's header dict, names lowercased."""
    if not isinstance(headers, dict):
        return {}
    return {name.lower(): str(value) for name, value in headers.items()
            if isinstance(name, str) and name.lower().startswith(PREFIX)}


class UsageMeter:
    """The latest rate-limit headers seen, guarded by its own lock."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.headers: dict[str, str] = {}
        self.updated: str | None = None
        self.seeded = False

    def observe(self, response, when: str | None = None) -> None:
        found = unified((response or {}).get("headers")) if isinstance(response, dict) else {}
        if not found:
            return  # errors, local replies, other upstreams: keep the last known values
        with self.lock:
            self.headers = found
            self.updated = when or datetime.now().isoformat(timespec="seconds")

    def seed(self, exchanges) -> None:
        """After a restart, start from the newest logged reply that had the headers."""
        with self.lock:
            if self.seeded:
                return
            self.seeded = True
            if self.headers:
                return
        for ex in reversed(list(exchanges)):
            if unified((ex.get("response") or {}).get("headers")):
                self.observe(ex["response"], ex.get("time"))
                return

    def snapshot(self) -> dict:
        with self.lock:
            return {"headers": dict(self.headers), "updated": self.updated}


METER = UsageMeter()


def install(hub, tap_cls) -> None:
    """Watch the hub's finished replies and add the two routes to the handler."""
    finish = hub.finish

    def finish_and_observe(xid: int, response: dict) -> None:
        finish(xid, response)
        try:
            METER.observe(response)
        except Exception:  # noqa: BLE001  the meter must never break the relay
            pass

    hub.finish = finish_and_observe

    dashboard = tap_cls.dashboard

    def dashboard_with_usage(self) -> None:
        route = urlsplit(self.path).path
        if route == "/__usage":
            with hub.lock:
                loaded = list(hub.finished)
            METER.seed(loaded)
            self.send_body(200, "application/json", json.dumps(METER.snapshot(), ensure_ascii=False).encode())
        elif route == "/__usage-meter.js":
            try:
                self.send_body(200, "text/javascript; charset=utf-8", SCRIPT.read_bytes())
            except OSError:
                self.send_body(500, "text/plain", b"usage_meter.js is missing next to proxy.py")
        else:
            dashboard(self)

    tap_cls.dashboard = dashboard_with_usage
