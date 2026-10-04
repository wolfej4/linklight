"""Pages that work without signing in: the big-screen display, player pages and public brackets."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, or_, select

from .. import brackets
from .. import gameservers as gs
from ..auth import is_admin
from ..db import active_event, get_session
from ..models import Attendee, Circuit, GameServer, LanTable, Match, Tournament
from ..qr import qr_svg
from ..web import back, base_url, game_host, render
from .tournaments import bracket_context

router = APIRouter()


def _live_matches(s: Session, event_id: int, people: dict, limit: int = 8):
    out = []
    for t in s.exec(select(Tournament).where(Tournament.event_id == event_id, Tournament.status == "live")).all():
        ms = s.exec(select(Match).where(Match.tournament_id == t.id, Match.done == False,  # noqa: E712
                                        Match.p1 != None, Match.p2 != None)  # noqa: E711
                    .order_by(Match.round, Match.slot)).all()
        busy: set[int] = set()
        shown = 0
        for m in ms:
            if shown == 4:
                break
            if m.p1 in busy or m.p2 in busy:
                continue  # a player can only be in one match at a time
            busy.update((m.p1, m.p2))
            shown += 1
            out.append(dict(t=t, m=m, a=people.get(m.p1), b=people.get(m.p2)))
    return out[:limit]


@router.get("/display", response_class=HTMLResponse)
def display(request: Request, s: Session = Depends(get_session)):
    return render(request, "display.html", event=active_event(s), **_display_ctx(request, s))


@router.get("/display/body", response_class=HTMLResponse)
def display_body(request: Request, s: Session = Depends(get_session)):
    return render(request, "_display_body.html", event=active_event(s), **_display_ctx(request, s))


def _display_ctx(request: Request, s: Session) -> dict:
    ev = active_event(s)
    people = {a.id: a for a in s.exec(select(Attendee).where(Attendee.event_id == ev.id)).all()}
    servers = []
    ok, _ = gs.docker_available()
    if ok:
        tpls = gs.templates()
        for g in s.exec(select(GameServer).where(GameServer.event_id == ev.id)).all():
            state, _d = gs.status(g.container_name)
            if state == "running":
                servers.append(dict(g=g, label=tpls.get(g.template, {}).get("label", g.template)))
    done = s.exec(select(Tournament).where(Tournament.event_id == ev.id, Tournament.status == "done")).all()
    winners = []
    for t in done:
        ctx = bracket_context(s, t)
        if ctx["champion"]:
            winners.append(dict(t=t, who=ctx["champion"]))
    return dict(
        here=sum(1 for a in people.values() if a.checked_in_at), total=len(people),
        matches=_live_matches(s, ev.id, people), servers=servers, host=game_host(request),
        winners=winners, now=datetime.now(),
        live=s.exec(select(Tournament).where(Tournament.event_id == ev.id, Tournament.status == "live")).all(),
    )


@router.get("/t/{tid}", response_class=HTMLResponse)
def public_bracket(request: Request, tid: int, s: Session = Depends(get_session)):
    t = s.get(Tournament, tid)
    if not t:
        return HTMLResponse("Tournament not found.", status_code=404)
    return render(request, "bracket_public.html", event=active_event(s), **bracket_context(s, t))


def _player(s: Session, token: str) -> Attendee | None:
    return s.exec(select(Attendee).where(Attendee.token == token)).first()


@router.get("/p/{token}", response_class=HTMLResponse)
def player(request: Request, token: str, s: Session = Depends(get_session)):
    a = _player(s, token)
    if not a:
        return HTMLResponse("This player link isn't valid. Ask an organizer for a new one.", status_code=404)
    ev = active_event(s)
    table = s.get(LanTable, a.table_id) if a.table_id else None
    circuit = s.get(Circuit, table.circuit_id) if table and table.circuit_id else None
    people = {p.id: p for p in s.exec(select(Attendee).where(Attendee.event_id == a.event_id)).all()}
    mine = []
    for m in s.exec(select(Match).where(or_(Match.p1 == a.id, Match.p2 == a.id), Match.is_bye == False)  # noqa: E712
                    .order_by(Match.round)).all():
        t = s.get(Tournament, m.tournament_id)
        if t and t.status != "setup":
            mine.append(dict(t=t, m=m, a=people.get(m.p1), b=people.get(m.p2),
                             ready=m.p1 is not None and m.p2 is not None))
    return render(request, "player.html", event=ev, a=a, table=table, circuit=circuit, matches=mine,
                  qr=qr_svg(f"{base_url(request)}/p/{a.token}"))


@router.post("/p/{token}/checkin")
def player_checkin(request: Request, token: str, s: Session = Depends(get_session)):
    a = _player(s, token)
    if a and is_admin(request) and not a.checked_in_at:
        a.checked_in_at = datetime.now()
        s.add(a)
        s.commit()
    return back(f"/p/{token}")


@router.post("/p/{token}/report/{mid}")
def player_report(token: str, mid: int, mine: int = Form(...), theirs: int = Form(...),
                  s: Session = Depends(get_session)):
    a = _player(s, token)
    m = s.get(Match, mid)
    if not a or not m or a.id not in (m.p1, m.p2):
        return back(f"/p/{token}", "You can only report your own matches.")
    if m.done:
        return back(f"/p/{token}", "This match already has a result. Ask an organizer to correct it.")
    t = s.get(Tournament, m.tournament_id)
    s1, s2 = (mine, theirs) if a.id == m.p1 else (theirs, mine)
    try:
        brackets.report(s, t, m, s1, s2)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/p/{token}", str(e))
    return back(f"/p/{token}")
