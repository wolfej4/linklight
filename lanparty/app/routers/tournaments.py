import random

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, col, select

from .. import brackets
from ..auth import require_admin
from ..db import active_event, get_session
from ..models import Attendee, Entrant, Match, Tournament
from ..web import back, render

router = APIRouter(dependencies=[Depends(require_admin)])

FORMATS = {"single": "Single elimination", "roundrobin": "Round robin"}


def bracket_context(s: Session, t: Tournament) -> dict:
    entrants = s.exec(select(Entrant).where(Entrant.tournament_id == t.id).order_by(Entrant.seed, Entrant.id)).all()
    matches = s.exec(select(Match).where(Match.tournament_id == t.id).order_by(Match.round, Match.slot)).all()
    ids = [e.attendee_id for e in entrants]
    people = {a.id: a for a in s.exec(select(Attendee).where(Attendee.event_id == t.event_id)).all()}
    rounds: dict[int, list] = {}
    for m in matches:
        rounds.setdefault(m.round, []).append(m)
    total = max(rounds) if rounds else 0
    names = {r: (brackets.round_name(r, total) if t.fmt == "single" else f"Round {r}") for r in rounds}
    champ = brackets.champion(t, matches, ids)
    return dict(
        t=t, entrants=entrants, rounds=rounds, round_names=names, people=people,
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
           s: Session = Depends(get_session)):
    ev = active_event(s)
    t = Tournament(event_id=ev.id, name=name.strip() or "Tournament", game=game.strip(),
                   fmt=fmt if fmt in FORMATS else "single", best_of=max(1, best_of))
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
    entered = {e.attendee_id for e in ctx["entrants"]}
    pool = s.exec(select(Attendee).where(Attendee.event_id == ev.id).order_by(col(Attendee.name))).all()
    return render(request, "tournament.html", event=ev, section="tournaments",
                  pool=[a for a in pool if a.id not in entered], **ctx)


@router.post("/tournaments/{tid}/entrants")
def add_entrant(tid: int, attendee_id: int = Form(...), s: Session = Depends(get_session)):
    t = _get(s, tid)
    if t.status != "setup":
        return back(f"/tournaments/{tid}", "Players can't be added after the bracket starts.")
    n = len(s.exec(select(Entrant).where(Entrant.tournament_id == tid)).all())
    s.add(Entrant(tournament_id=tid, attendee_id=attendee_id, seed=n + 1))
    s.commit()
    return back(f"/tournaments/{tid}")


@router.post("/tournaments/{tid}/entrants/checked-in")
def add_checked_in(tid: int, s: Session = Depends(get_session)):
    t = _get(s, tid)
    if t.status != "setup":
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
    if e and t.status == "setup":
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
    for model in (Match, Entrant):
        for row in s.exec(select(model).where(model.tournament_id == tid)).all():
            s.delete(row)
    t = s.get(Tournament, tid)
    if t:
        s.delete(t)
    s.commit()
    return back("/tournaments")
