from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import config
from .auth import current_user, is_admin
from .config import BASE_URL, PUBLIC_HOST

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _dt(value, fmt="%a %-I:%M %p"):
    return value.strftime(fmt) if value else ""


def _when(ev) -> str:
    """Human date range for an event, falling back to whatever free text was typed."""
    start, end = ev.start, ev.end
    if not start:
        return ev.starts_at
    out = start.strftime("%a %b %-d, %Y, %-I:%M %p")
    if end:
        same_day = end.date() == start.date()
        out += " to " + end.strftime("%-I:%M %p" if same_day else "%a %b %-d, %-I:%M %p")
    return out


def _money(cents: int, currency: str | None = None, zero_is_free: bool = True) -> str:
    if not cents and zero_is_free:
        return "Free"
    cur = (currency or config.CURRENCY).upper()
    sym = {"USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "EUR": "€", "GBP": "£"}.get(cur)
    amount = f"{cents / 100:,.2f}"
    return f"{sym}{amount}" if sym else f"{amount} {cur}"


def _paras(text: str):
    """Plain text to paragraphs. Everything is escaped; blank lines split paragraphs."""
    blocks = [b.strip() for b in (text or "").replace("\r\n", "\n").split("\n\n") if b.strip()]
    return Markup("".join(f"<p>{Markup('<br>').join(escape(line) for line in b.split(chr(10)))}</p>" for b in blocks))


templates.env.filters["dt"] = _dt
templates.env.filters["when"] = _when
templates.env.filters["money"] = _money
templates.env.filters["paras"] = _paras
templates.env.globals["site_name"] = config.SITE_NAME


def base_url(request: Request) -> str:
    return BASE_URL or str(request.base_url).rstrip("/")


def game_host(request: Request) -> str:
    return PUBLIC_HOST or request.url.hostname or "localhost"


def render(request: Request, name: str, **ctx):
    ctx.setdefault("is_admin", is_admin(request))
    ctx.setdefault("user", current_user(request))
    ctx.setdefault("error", request.query_params.get("error"))
    ctx.setdefault("notice", request.query_params.get("notice"))
    ctx.setdefault("base", base_url(request))
    ctx.setdefault("now", datetime.now())
    return templates.TemplateResponse(request, name, ctx)


def back(url: str, error: str | None = None, notice: str | None = None):
    if error:
        url += ("&" if "?" in url else "?") + "error=" + quote(error)
    if notice:
        url += ("&" if "?" in url else "?") + "notice=" + quote(notice)
    return RedirectResponse(url, status_code=303)
