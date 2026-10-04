from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import is_admin
from .config import BASE_URL, PUBLIC_HOST

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _dt(value, fmt="%a %-I:%M %p"):
    return value.strftime(fmt) if value else ""


templates.env.filters["dt"] = _dt


def base_url(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def game_host(request: Request) -> str:
    return PUBLIC_HOST or request.url.hostname or "localhost"


def render(request: Request, name: str, **ctx):
    ctx.setdefault("is_admin", is_admin(request))
    ctx.setdefault("error", request.query_params.get("error"))
    ctx.setdefault("base", base_url(request))
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, error: str | None = None):
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    return RedirectResponse(url, status_code=303)
