"""Server-side deep-link resolution for the dependency-free fallback UI (T-18 DoD 5)."""

from __future__ import annotations

import html
import json

ROUTE_PLACEHOLDER = "__ORCH_ROUTE__"


def fallback_route(pathname: str) -> dict[str, str | None]:
    """`/tasks/7` → `{"view": "task", "arg": "7"}` (the React router's `TaskDetail` view)."""
    view, _, arg = (pathname or "").strip("/").partition("/")
    if not view:
        view = "dashboard"
    elif view == "tasks" and arg:
        view = "task"
    return {"view": view, "arg": arg or None}


def render_shell(html_text: str, pathname: str) -> str:
    """Put the resolved route into the page's `data-route`; fail loudly if the hook is gone."""
    if ROUTE_PLACEHOLDER not in html_text:
        raise RuntimeError(f"fallback UI lost its {ROUTE_PLACEHOLDER} route placeholder")
    route = html.escape(json.dumps(fallback_route(pathname)), quote=True)
    return html_text.replace(ROUTE_PLACEHOLDER, route, 1)
