"""Pre-built answers for the AI usage page's fixed questions.

Keeper, Discord #gateway unified usage ledger thread, msg 1552976803495280683:
"option A is the better option. lets do it please". Option A, as proposed:
the page asks the same questions every time, so a background renderer
computes those exact answers after each change to the Redis copies and once a
minute, stores them in Redis, and the dashboard serves the stored answer.
Countdowns and "updated x ago" fields can be up to a minute old; the ledger
data itself only moves every 15 minutes. Any other question (a different
window, a filter) is computed live, exactly as before.

Why this is needed after the Redis copies (measured 2026-09-25): each panel
still worked through ~47k rows per request (1.6 s decoding and 1 s building
the chart for the 168-hour view), and the dashboard is one Python process, so
the page's panels ran one at a time: 10-17 s for the whole page with every
panel under 3 s on its own.

The renderer calls the dashboard's own routes (same code, same Redis copies,
same fallbacks) and stores the response body verbatim, so a pre-built answer
is byte-for-byte what the route returned at ``rendered_at``. The renderer's
app never serves pre-built answers itself; only an app created with
``SERVE_ENV`` set to ``serve`` does. A stored answer older than
``DEFAULT_MAX_AGE_SECONDS`` is ignored and the route runs live, so a dead
renderer degrades to the old behaviour, never to a frozen page.

Keys: ``ai-usage:v1:view:<path?query>``, a hash of ``status``, ``body``,
``content_type``, ``rendered_at``, ``rendered_at_epoch``, ``render_seconds``
and the copy generations the render started from.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Callable

from .usage_cache import KEY_PREFIX, META_KEY, QUOTA_META_KEY, SOCKET_ENV, _client

logger = logging.getLogger(__name__)

SERVE_ENV = "AI_USAGE_PREBUILT_VIEWS"
MAX_AGE_ENV = "AI_USAGE_PREBUILT_MAX_AGE_SECONDS"
VIEW_PREFIX = f"{KEY_PREFIX}:view:"
DEFAULT_INTERVAL_SECONDS = 60
# Two and a half render cycles: one slow or missed render keeps serving,
# a stopped renderer hands back to the live routes within minutes.
DEFAULT_MAX_AGE_SECONDS = 150
POLL_SECONDS = 2

# The page's own requests (frontend/src/hooks/useDashboardData.ts) that do
# real work per request. The rulings panels, provider status and billing
# already answer in milliseconds and stay live.
VIEWS = (
    "/api/unified-usage?hours=5",
    "/api/unified-usage?hours=24",
    "/api/unified-usage?hours=168",
    "/api/subscriptions?hours=168&history=0",
    "/api/quota-burn?hours=6",
    "/api/weekly-utilization?weeks=4",
    "/api/opencode-go-usage",
)


def view_key(path: str, query_string: str) -> str:
    return f"{VIEW_PREFIX}{path}?{query_string}" if query_string else f"{VIEW_PREFIX}{path}"


def _split(view: str) -> tuple[str, str]:
    path, _, query = view.partition("?")
    return path, query


def _generations(client) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for name, key in (("usage", META_KEY), ("quota", QUOTA_META_KEY)):
        raw = client.get(key)
        found[name] = json.loads(raw).get("generation") if raw else None
    return found


def render_once(app, socket_path: str, *, views: tuple[str, ...] = VIEWS, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Call each view's route on ``app`` and store the response verbatim."""
    if app.config.get("SERVE_PREBUILT_VIEWS"):
        raise RuntimeError("the renderer's app must not serve pre-built views (it would copy its own answers)")
    client = _client(socket_path)
    generations = _generations(client)
    http = app.test_client()
    rendered: dict[str, Any] = {}
    started_all = time.monotonic()
    for view in views:
        started = time.monotonic()
        try:
            response = http.get(view)
        except Exception as exc:  # noqa: BLE001 - one view failing leaves the rest current
            logger.warning("render of %s failed: %s: %s", view, type(exc).__name__, exc)
            rendered[view] = {"status": "failed"}
            continue
        seconds = time.monotonic() - started
        if response.status_code != 200:
            # Errors are not cached: the live route answers them honestly.
            client.delete(view_key(*_split(view)))
            rendered[view] = {"status": response.status_code, "seconds": round(seconds, 2)}
            continue
        now = clock()
        client.hset(
            view_key(*_split(view)),
            mapping={
                "status": str(response.status_code),
                "content_type": response.content_type,
                "body": response.get_data(as_text=True),
                "rendered_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "rendered_at_epoch": repr(now),
                "render_seconds": f"{seconds:.3f}",
                "generations": json.dumps(generations, separators=(",", ":")),
            },
        )
        rendered[view] = {"status": 200, "seconds": round(seconds, 2)}
    return {
        "views": rendered,
        "total_seconds": round(time.monotonic() - started_all, 2),
        "generations": generations,
    }


def lookup(
    path: str,
    query_string: str,
    *,
    socket_path: str | None = None,
    max_age_seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any] | None:
    """The stored answer for exactly this request, or ``None`` to run the route."""
    view = f"{path}?{query_string}" if query_string else path
    if view not in VIEWS:
        return None
    socket_path = socket_path or os.environ.get(SOCKET_ENV)
    if not socket_path:
        return None
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_SECONDS))
    try:
        stored = _client(socket_path).hgetall(view_key(path, query_string))
    except Exception as exc:  # noqa: BLE001 - any cache fault means run the route
        logger.warning("pre-built view unavailable, running the route: %s: %s", type(exc).__name__, exc)
        return None
    if not stored or "body" not in stored:
        return None
    age = clock() - float(stored["rendered_at_epoch"])
    if age > max_age_seconds or age < -5:
        return None
    return {
        "status": int(stored["status"]),
        "content_type": stored["content_type"],
        "body": stored["body"],
        "rendered_at": stored["rendered_at"],
        "age_seconds": age,
    }


def run_forever(
    app,
    socket_path: str,
    *,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    log: Callable[[str], None] = print,
) -> None:
    """Render every ``interval_seconds``, and at once when a copy is refreshed."""
    client = _client(socket_path)
    while True:
        cycle_started = time.monotonic()
        try:
            summary = render_once(app, socket_path)
            log(json.dumps({"rendered": summary}, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - Redis down, etc.: log, keep the loop
            log(json.dumps({"render_failed": f"{type(exc).__name__}: {exc}"}))
            summary = {"generations": None}
        seen = summary.get("generations")
        while time.monotonic() - cycle_started < interval_seconds:
            time.sleep(poll_seconds)
            try:
                if seen is not None and _generations(client) != seen:
                    break  # a copy was refreshed: render the new data now
            except Exception:  # noqa: BLE001
                pass
