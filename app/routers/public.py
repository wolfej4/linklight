"""Pages that work without signing in: the big-screen display, player pages and public brackets."""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, or_, select

from .. import brackets, teams
from .. import gameservers as gs
from ..auth import current_user, is_admin, require_user
from ..db import active_event, get_session
from ..models import Attendee, Circuit, Event, GameServer, LanTable, Match, Team, TimetableItem, Tournament
from ..qr import qr_svg
from ..web import back, base_url, game_host, render
from .tournaments import bracket_context

router = APIRouter()


def _live_matches(s: Session, event_id: int, limit: int = 8):
    out = []
    for t in s.exec(select(Tournament).where(Tournament.event_id == event_id, Tournament.status == "live")).all():
        people = teams.sides(s, t)
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
        matches=_live_matches(s, ev.id), servers=servers, host=game_host(request),
        winners=winners, now=datetime.now(), schedule=_coming_up(s, ev.id),
        live=s.exec(select(Tournament).where(Tournament.event_id == ev.id, Tournament.status == "live")).all(),
    )


def _coming_up(s: Session, event_id: int, limit: int = 3) -> list[TimetableItem]:
    """Timetable items that haven't finished yet, soonest first."""
    now = datetime.now()
    items = s.exec(select(TimetableItem).where(TimetableItem.event_id == event_id)
                   .order_by(TimetableItem.starts_at)).all()
    return [i for i in items if (i.ends_at or i.starts_at) >= now][:limit]


def _my_spot(request: Request, s: Session, event_id: int) -> Attendee | None:
    """The signed-in user's attendee row at this event, if they hold a ticket."""
    user = current_user(request)
    if not user:
        return None
    return s.exec(select(Attendee).where(Attendee.event_id == event_id, Attendee.user_id == user.id)).first()


@router.get("/t/{tid}", response_class=HTMLResponse)
def public_bracket(request: Request, tid: int, s: Session = Depends(get_session)):
    t = s.get(Tournament, tid)
    ev = s.get(Event, t.event_id) if t else None
    if not t or not ev or (not ev.published and not ev.active and not is_admin(request)):
        return HTMLResponse("Tournament not found.", status_code=404)
    ctx = bracket_context(s, t)
    me = _my_spot(request, s, t.event_id)
    my_team = teams.team_of(s, t, me.id) if me and t.teams else None
    entered = bool(my_team) if t.teams else bool(me and any(e.attendee_id == me.id for e in ctx["entrants"]))
    return render(request, "bracket_public.html", event=None, ev=ev, me=me, my_team=my_team, entered=entered, **ctx)


def _signup_target(request: Request, s: Session, tid: int) -> tuple[Tournament, Attendee]:
    require_user(request)
    t = s.get(Tournament, tid)
    if not t or not t.signups_open:
        raise ValueError("Sign-ups for this tournament are closed.")
    me = _my_spot(request, s, t.event_id)
    if not me:
        raise ValueError("You need a ticket for this event to sign up.")
    return t, me


@router.post("/t/{tid}/join")
def solo_join(request: Request, tid: int, s: Session = Depends(get_session)):
    try:
        t, me = _signup_target(request, s, tid)
        teams.enter_solo(s, t, me)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/t/{tid}", str(e))
    return back(f"/t/{tid}", notice="You're signed up.")


@router.post("/t/{tid}/leave")
def solo_leave(request: Request, tid: int, s: Session = Depends(get_session)):
    try:
        t, me = _signup_target(request, s, tid)
        if t.teams:
            team = teams.team_of(s, t, me.id)
            if team:
                teams.leave_team(s, t, team, me.id)
        else:
            teams.withdraw_solo(s, t, me.id)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/t/{tid}", str(e))
    return back(f"/t/{tid}", notice="You've left.")


@router.post("/t/{tid}/teams")
def team_create(request: Request, tid: int, name: str = Form(...), s: Session = Depends(get_session)):
    try:
        t, me = _signup_target(request, s, tid)
        teams.create_team(s, t, name, me)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/t/{tid}", str(e))
    return back(f"/t/{tid}", notice="Team created. Share the join code with your teammates.")


@router.post("/t/{tid}/teams/join")
def team_join(request: Request, tid: int, code: str = Form(...), s: Session = Depends(get_session)):
    try:
        t, me = _signup_target(request, s, tid)
        team = s.exec(select(Team).where(Team.tournament_id == tid, Team.join_code == code.strip().upper())).first()
        if not team:
            raise ValueError("No team here has that code. Check it with your captain.")
        teams.join_team(s, t, team, me)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/t/{tid}", str(e))
    return back(f"/t/{tid}", notice=f"You joined {team.name}.")


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
    mine = []
    for t in s.exec(select(Tournament).where(Tournament.event_id == a.event_id, Tournament.status != "setup")
                    .order_by(Tournament.id)).all():
        me = teams.slot_for(s, t, a.id)
        if me is None:
            continue
        people = teams.sides(s, t)
        for m in s.exec(select(Match).where(Match.tournament_id == t.id, or_(Match.p1 == me, Match.p2 == me),
                                            Match.is_bye == False).order_by(Match.round)).all():  # noqa: E712
            mine.append(dict(t=t, m=m, me=me, a=people.get(m.p1), b=people.get(m.p2),
                             ready=m.p1 is not None and m.p2 is not None))
    return render(request, "player.html", event=s.get(Event, a.event_id) or ev, a=a, table=table, circuit=circuit,
                  matches=mine, qr=qr_svg(f"{base_url(request)}/p/{a.token}"))


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
    t = s.get(Tournament, m.tournament_id) if m else None
    me = teams.slot_for(s, t, a.id) if a and t and t.event_id == a.event_id else None
    if me is None or me not in (m.p1, m.p2):
        return back(f"/p/{token}", "You can only report your own matches.")
    if m.done:
        return back(f"/p/{token}", "This match already has a result. Ask an organizer to correct it.")
    s1, s2 = (mine, theirs) if me == m.p1 else (theirs, mine)
    try:
        brackets.report(s, t, m, s1, s2)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/p/{token}", str(e))
    return back(f"/p/{token}")
