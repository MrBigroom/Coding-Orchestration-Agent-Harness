"""Path → view resolution for the dependency-free fallback UI (T-18 DoD 5).

The fallback page is plain HTML/CSS/JS served when `web/dist` is missing, and a notification
deep link is the React path form (`/tasks/<id>`). Resolving that path in JavaScript alone meant
the only way to test it was grepping the page source; the API now resolves the request path
here and hands the result to the page, so a test can call the URL and assert the route the page
will actually render. The hash form (`#task/<id>`) stays in-page navigation.
"""

from __future__ import annotations

import html
import json

ROUTE_PLACEHOLDER = "__ORCH_ROUTE__"
DEFAULT_VIEW = "dashboard"


def fallback_route(pathname: str, fragment: str = "") -> dict[str, str | None]:
    """Resolve a URL to the fallback UI's (view, arg).

    `#task/7` wins over the path when both are present, exactly like the page's own
    `location.hash` handling. `/tasks/7` maps onto the singular `task` view, which is the view
    the React router calls `TaskDetail`.
    """
    raw_fragment = fragment[1:] if fragment.startswith("#") else fragment
    raw = (raw_fragment or pathname or "").strip("/")
    view, _, arg = raw.partition("/")
    view = view or DEFAULT_VIEW
    if view == "tasks" and arg:
        view = "task"
    return {"view": view, "arg": arg or None}


def render_shell(html_text: str, pathname: str) -> str:
    """Put the resolved route into the page's `data-route` attribute.

    Fails loudly if the placeholder disappears: a page that silently lost its route would show
    the dashboard for every deep link, which is the defect this mechanism exists to prevent.
    """
    if ROUTE_PLACEHOLDER not in html_text:
        raise RuntimeError(f"fallback UI lost its {ROUTE_PLACEHOLDER} route placeholder")
    route = json.dumps(fallback_route(pathname))
    return html_text.replace(ROUTE_PLACEHOLDER, html.escape(route, quote=True), 1)
