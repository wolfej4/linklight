import hmac
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select
from starlette.middleware.sessions import SessionMiddleware

from . import gameservers as gs
from .auth import LoginRequired, require_admin
from .config import ADMIN_PASSWORD, secret_key
from .db import active_event, get_session, init_db
from .models import Attendee, Circuit, Entrant, Event, GameServer, LanTable, Match, Tournament
from .routers import attendees, public, seating, servers, tournaments
from .web import back, render


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="LAN Party Manager", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=secret_key(), max_age=60 * 60 * 24 * 14, same_site="lax")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.exception_handler(LoginRequired)
async def _login_redirect(request: Request, _exc):
    target = "/login?next=" + request.url.path
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


for r in (attendees.router, seating.router, tournaments.router, servers.router, public.router):
    app.include_router(r)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/login")
def login_page(request: Request, next: str = "/"):
    return render(request, "login.html", next=next, event=None)


@app.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/")):
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        request.session["admin"] = True
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    return render(request, "login.html", next=next, event=None, error="That password didn't match. Try again.")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", dependencies=[Depends(require_admin)])
def dashboard(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    people = s.exec(select(Attendee).where(Attendee.event_id == ev.id)).all()
    tables = s.exec(select(LanTable).where(LanTable.event_id == ev.id)).all()
    circuits = s.exec(select(Circuit).where(Circuit.event_id == ev.id)).all()
    tourneys = s.exec(select(Tournament).where(Tournament.event_id == ev.id)).all()
    servers_ = s.exec(select(GameServer).where(GameServer.event_id == ev.id)).all()
    seats = sum(t.seats for t in tables)
    seated = sum(1 for a in people if a.table_id)
    load = seating.circuit_loads(circuits, tables, people)
    return render(
        request, "dashboard.html", event=ev, section="home",
        stats=dict(
            total=len(people), checked=sum(1 for a in people if a.checked_in_at),
            paid=sum(1 for a in people if a.paid), seats=seats, open=seats - seated,
            live=sum(1 for t in tourneys if t.status == "live"),
            running=sum(1 for g in servers_ if gs.status(g.container_name)[0] == "running") if servers_ else 0,
            servers=len(servers_),
        ),
        hot=[c for c in load if c["pct"] >= 90],
        events=s.exec(select(Event).order_by(Event.id.desc())).all(),
    )


@app.post("/event", dependencies=[Depends(require_admin)])
def update_event(
    request: Request, name: str = Form(...), starts_at: str = Form(""), venue: str = Form(""),
    announcement: str = Form(""), s: Session = Depends(get_session),
):
    ev = active_event(s)
    ev.name, ev.starts_at, ev.venue, ev.announcement = name.strip() or ev.name, starts_at, venue, announcement.strip()
    s.add(ev)
    s.commit()
    return back("/")


@app.post("/events", dependencies=[Depends(require_admin)])
def create_event(name: str = Form(...), s: Session = Depends(get_session)):
    for e in s.exec(select(Event)).all():
        e.active = False
        s.add(e)
    s.add(Event(name=name.strip() or "LAN Party", active=True))
    s.commit()
    return back("/")


@app.post("/events/{event_id}/activate", dependencies=[Depends(require_admin)])
def activate_event(event_id: int, s: Session = Depends(get_session)):
    for e in s.exec(select(Event)).all():
        e.active = e.id == event_id
        s.add(e)
    s.commit()
    return back("/")


@app.post("/events/{event_id}/delete", dependencies=[Depends(require_admin)])
def delete_event(event_id: int, s: Session = Depends(get_session)):
    ev = s.get(Event, event_id)
    if not ev:
        return back("/")
    if ev.active:
        return back("/", "Switch to another event before deleting this one.")
    for g in s.exec(select(GameServer).where(GameServer.event_id == event_id)).all():
        gs.remove(g.container_name)
        s.delete(g)
    for t in s.exec(select(Tournament).where(Tournament.event_id == event_id)).all():
        for row in s.exec(select(Match).where(Match.tournament_id == t.id)).all():
            s.delete(row)
        for row in s.exec(select(Entrant).where(Entrant.tournament_id == t.id)).all():
            s.delete(row)
        s.delete(t)
    for model in (Attendee, LanTable, Circuit):
        for row in s.exec(select(model).where(model.event_id == event_id)).all():
            s.delete(row)
    s.delete(ev)
    s.commit()
    return back("/")
