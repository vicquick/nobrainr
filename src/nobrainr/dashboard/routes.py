"""Page route handlers — serve Jinja2 templates."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)


async def dashboard_page(request: Request) -> HTMLResponse:
    """Knowledge graph visualization — main dashboard page."""
    template = jinja_env.get_template("graph.html")
    return HTMLResponse(template.render())


async def timeline_page(request: Request) -> HTMLResponse:
    """Timeline view of memories."""
    template = jinja_env.get_template("timeline.html")
    return HTMLResponse(template.render())


async def memories_page(request: Request) -> HTMLResponse:
    """Memory browser with search and filters."""
    template = jinja_env.get_template("memories.html")
    return HTMLResponse(template.render())


async def index_redirect(request: Request) -> RedirectResponse:
    """Redirect root /dashboard requests."""
    return RedirectResponse(url="/dashboard")


page_routes = [
    Route("/dashboard", dashboard_page),
    Route("/timeline", timeline_page),
    Route("/memories", memories_page),
]
