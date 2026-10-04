from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .auth import LoginRequired, NotOrganizer
from .config import secret_key
from .db import init_db
from .routers import accounts, admin, attendees, public, seating, servers, site, tournaments
from .web import render


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="LAN Party Manager", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=secret_key(), max_age=60 * 60 * 24 * 14, same_site="lax")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


def _return_path(request: Request) -> str:
    """Where to send someone after they sign in: this page, or for a form post, the page the form was on."""
    if request.method == "GET":
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return path
    ref = urlparse(request.headers.get("referer", ""))
    if ref.netloc == request.url.netloc and ref.path.startswith("/"):
        return ref.path + (f"?{ref.query}" if ref.query else "")
    return "/"


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, _exc):
    target = "/login?next=" + quote(_return_path(request), safe="/")
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


@app.exception_handler(NotOrganizer)
async def _not_organizer(request: Request, _exc):
    resp = render(request, "forbidden.html", event=None)
    resp.status_code = 403
    return resp


for r in (site.router, accounts.router, admin.router, attendees.router, seating.router, tournaments.router,
          servers.router, public.router):
    app.include_router(r)


@app.get("/healthz")
def healthz():
    return {"ok": True}
