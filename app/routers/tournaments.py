import random

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, col, select

from .. import brackets, teams
from ..auth import require_admin
from ..db import active_event, get_session
from ..models import Attendee, Entrant, Match, Team, TeamMember, Tournament
from ..web import back, render

router = APIRouter(dependencies=[Depends(require_admin)])

FORMATS = {"single": "Single elimination", "roundrobin": "Round robin"}


def bracket_context(s: Session, t: Tournament) -> dict:
    entrants = s.exec(select(Entrant).where(Entrant.tournament_id == t.id).order_by(Entrant.seed, Entrant.id)).all()
    matches = s.exec(select(Match).where(Match.tournament_id == t.id).order_by(Match.round, Match.slot)).all()
    ids = [e.team_id if t.teams else e.attendee_id for e in entrants]
    people = teams.sides(s, t)  # what fills each bracket slot: players, or teams
    rounds: dict[int, list] = {}
    for m in matches:
        rounds.setdefault(m.round, []).append(m)
    total = max(rounds) if rounds else 0
    names = {r: (brackets.round_name(r, total) if t.fmt == "single" else f"Round {r}") for r in rounds}
    champ = brackets.champion(t, matches, ids)
    rosters = {x: teams.members(s, x) for x in people} if t.teams else {}
    return dict(
        t=t, entrants=entrants, rounds=rounds, round_names=names, people=people, rosters=rosters,
        standings=brackets.standings(matches, ids) if t.fmt == "roundrobin" else None,
        champion=people.get(champ) if champ else None, formats=FORMATS,
    )


def _get(s: Session, tid: int) -> Tournament:
    t = s.get(Tournament, tid)
    if not t:
        raise ValueError("Tournament not found.")
    return t


@router.get("/tournaments")
def page(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    ts = s.exec(select(Tournament).where(Tournament.event_id == ev.id).order_by(col(Tournament.id).desc())).all()
    counts = {t.id: len(s.exec(select(Entrant).where(Entrant.tournament_id == t.id)).all()) for t in ts}
    return render(request, "tournaments.html", event=ev, section="tournaments", tournaments=ts, counts=counts, formats=FORMATS)


@router.post("/tournaments")
def create(name: str = Form(...), game: str = Form(""), fmt: str = Form("single"), best_of: int = Form(1),
           team_size: int = Form(1), signups_open: str = Form(""), s: Session = Depends(get_session)):
    ev = active_event(s)
    t = Tournament(event_id=ev.id, name=name.strip() or "Tournament", game=game.strip(),
                   fmt=fmt if fmt in FORMATS else "single", best_of=max(1, best_of),
                   team_size=max(1, min(team_size, 16)), signups_open=bool(signups_open))
    s.add(t)
    s.commit()
    return back(f"/tournaments/{t.id}")


@router.get("/tournaments/{tid}")
def detail(request: Request, tid: int, s: Session = Depends(get_session)):
    ev = active_event(s)
    t = s.get(Tournament, tid)
    if not t:
        return back("/tournaments", "That tournament no longer exists.")
    ctx = bracket_context(s, t)
    if t.teams:
        entered = {a.id for roster in ctx["rosters"].values() for a in roster}
    else:
        entered = {e.attendee_id for e in ctx["entrants"]}
    pool = s.exec(select(Attendee).where(Attendee.event_id == t.event_id).order_by(col(Attendee.name))).all()
    return render(request, "tournament.html", event=ev, section="tournaments",
                  pool=[a for a in pool if a.id not in entered], **ctx)


@router.post("/tournaments/{tid}/signups")
def toggle_signups(tid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    t.signups_open = not t.signups_open
    s.add(t)
    s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/entrants")
def add_entrant(tid: int, attendee_id: int = Form(...), s: Session = Depends(get_session)):
    t = _get(s, tid)
    a = s.get(Attendee, attendee_id)
    if not a or a.event_id != t.event_id:
        return back(f"/tournaments/{tid}", "That player isn't at this event.")
    try:
        teams.enter_solo(s, t, a)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/teams")
def add_team(tid: int, name: str = Form(...), captain_id: int = Form(...), s: Session = Depends(get_session)):
    t = _get(s, tid)
    a = s.get(Attendee, captain_id)
    if not a or a.event_id != t.event_id:
        return back(f"/tournaments/{tid}", "That player isn't at this event.")
    try:
        teams.create_team(s, t, name, a)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/teams/{team_id}/members")
def add_member(tid: int, team_id: int, attendee_id: int = Form(...), s: Session = Depends(get_session)):
    t = _get(s, tid)
    team = s.get(Team, team_id)
    a = s.get(Attendee, attendee_id)
    if not team or team.tournament_id != tid or not a or a.event_id != t.event_id:
        return back(f"/tournaments/{tid}")
    try:
        teams.join_team(s, t, team, a)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/teams/{team_id}/members/{attendee_id}/delete")
def remove_member(tid: int, team_id: int, attendee_id: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    team = s.get(Team, team_id)
    if team and team.tournament_id == tid:
        try:
            teams.leave_team(s, t, team, attendee_id)
            s.commit()
        except ValueError as e:
            s.rollback()
            return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/teams/{team_id}/delete")
def remove_team(tid: int, team_id: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    team = s.get(Team, team_id)
    if team and team.tournament_id == tid and t.status == "setup":
        teams.disband(s, team)
        s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/entrants/checked-in")
def add_checked_in(tid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    if t.status != "setup" or t.teams:
        return back(f"/tournaments/{tid}")
    have = {e.attendee_id for e in s.exec(select(Entrant).where(Entrant.tournament_id == tid)).all()}
    here = s.exec(select(Attendee).where(Attendee.event_id == t.event_id, Attendee.checked_in_at != None)  # noqa: E711
                  .order_by(Attendee.checked_in_at)).all()
    n = len(have)
    for a in here:
        if a.id not in have:
            n += 1
            s.add(Entrant(tournament_id=tid, attendee_id=a.id, seed=n))
    s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/entrants/{eid}/delete")
def remove_entrant(tid: int, eid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    e = s.get(Entrant, eid)
    if e and e.tournament_id == tid and t.status == "setup":
        team = s.get(Team, e.team_id) if e.team_id else None
        if team:
            teams.disband(s, team)
        else:
            s.delete(e)
        s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/shuffle")
def shuffle(tid: int, s: Session = Depends(get_session)):
    es = s.exec(select(Entrant).where(Entrant.tournament_id == tid)).all()
    order = list(range(1, len(es) + 1))
    random.shuffle(order)
    for e, seed in zip(es, order):
        e.seed = seed
        s.add(e)
    s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/start")
def start(tid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    try:
        brackets.generate(s, t)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/unstart")
def unstart(tid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    for m in s.exec(select(Match).where(Match.tournament_id == tid)).all():
        s.delete(m)
    t.status = "setup"
    s.add(t)
    s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/matches/{mid}/report")
def report(tid: int, mid: int, s1: int = Form(...), s2: int = Form(...), s: Session = Depends(get_session)):
    t = _get(s, tid)
    m = s.get(Match, mid)
    try:
        brackets.report(s, t, m, s1, s2)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}#m{mid}")


@router.post("/tournaments/{tid}/matches/{mid}/reset")
def reset(tid: int, mid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    m = s.get(Match, mid)
    try:
        brackets.reset(s, t, m)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back(f"/tournaments/{tid}", str(e))
    return back(f"/tournaments/{tid}#m{mid}")


@router.post("/tournaments/{tid}/delete")
def delete(tid: int, s: Session = Depends(get_session)):
    for team in s.exec(select(Team).where(Team.tournament_id == tid)).all():
        for row in s.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all():
            s.delete(row)
        s.delete(team)
    for model in (Match, Entrant):
        for row in s.exec(select(model).where(model.tournament_id == tid)).all():
            s.delete(row)
    t = s.get(Tournament, tid)
    if t:
        s.delete(t)
    s.commit()
    return back("/tournaments")
