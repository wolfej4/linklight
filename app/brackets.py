"""Bracket generation and result handling for single elimination and round robin."""
from sqlmodel import Session, delete, select

from .models import Entrant, Match, Tournament


def seed_order(size: int) -> list[int]:
    """Standard bracket seeding: 1v8, 4v5, 2v7, 3v6 for size 8."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2 + 1
        order = [x for s in order for x in (s, n - s)]
    return order


def round_name(r: int, total: int) -> str:
    left = total - r
    return {0: "Final", 1: "Semifinals", 2: "Quarterfinals"}.get(left, f"Round {r}")


def generate(s: Session, t: Tournament):
    entrants = s.exec(
        select(Entrant).where(Entrant.tournament_id == t.id).order_by(Entrant.seed, Entrant.id)
    ).all()
    ids = [e.team_id if t.teams else e.attendee_id for e in entrants]
    if len(ids) < 2:
        raise ValueError("Add at least two teams before starting." if t.teams else "Add at least two players before starting.")
    s.exec(delete(Match).where(Match.tournament_id == t.id))
    if t.fmt == "roundrobin":
        _round_robin(s, t, ids)
    else:
        _single(s, t, ids)
    t.status = "live"
    s.add(t)


def _single(s: Session, t: Tournament, ids: list[int]):
    size = 1 << (len(ids) - 1).bit_length()
    rounds = size.bit_length() - 1
    grid = []
    for r in range(1, rounds + 1):
        row = [Match(tournament_id=t.id, round=r, slot=i) for i in range(size >> r)]
        s.add_all(row)
        grid.append(row)
    s.flush()
    for r in range(rounds - 1):
        for i, m in enumerate(grid[r]):
            m.next_match_id = grid[r + 1][i // 2].id
            m.next_side = 1 if i % 2 == 0 else 2
    order = seed_order(size)
    for i, m in enumerate(grid[0]):
        a, b = order[2 * i], order[2 * i + 1]
        m.p1 = ids[a - 1] if a <= len(ids) else None
        m.p2 = ids[b - 1] if b <= len(ids) else None
        if (m.p1 is None) != (m.p2 is None):
            m.is_bye, m.done, m.winner = True, True, m.p1 or m.p2
            _advance(s, m)


def _round_robin(s: Session, t: Tournament, ids: list[int]):
    players: list = list(ids)
    if len(players) % 2:
        players.append(None)
    n = len(players)
    for r in range(n - 1):
        for i in range(n // 2):
            a, b = players[i], players[n - 1 - i]
            if a is not None and b is not None:
                s.add(Match(tournament_id=t.id, round=r + 1, slot=i, p1=a, p2=b))
        players = [players[0], players[-1], *players[1:-1]]


def _advance(s: Session, m: Match):
    if not m.next_match_id:
        return
    nxt = s.get(Match, m.next_match_id)
    if m.next_side == 1:
        nxt.p1 = m.winner
    else:
        nxt.p2 = m.winner
    s.add(nxt)


def _clear_next(s: Session, m: Match):
    if not m.next_match_id:
        return
    nxt = s.get(Match, m.next_match_id)
    if nxt.done:
        raise ValueError("The next-round match already has a result. Reset that one first.")
    if m.next_side == 1:
        nxt.p1 = None
    else:
        nxt.p2 = None
    s.add(nxt)


def report(s: Session, t: Tournament, m: Match, s1: int, s2: int):
    if t.status == "setup":
        raise ValueError("This tournament hasn't started yet.")
    if m.p1 is None or m.p2 is None:
        raise ValueError("Both sides need to be decided before reporting a score.")
    if s1 < 0 or s2 < 0:
        raise ValueError("Scores can't be negative.")
    if t.fmt == "single" and s1 == s2:
        raise ValueError("Elimination matches need a winner. Scores can't be tied.")
    if m.done and t.fmt == "single":
        _clear_next(s, m)
    m.s1, m.s2, m.done = s1, s2, True
    m.winner = m.p1 if s1 > s2 else m.p2 if s2 > s1 else None
    s.add(m)
    if t.fmt == "single":
        _advance(s, m)
    _check_complete(s, t)


def reset(s: Session, t: Tournament, m: Match):
    if m.is_bye:
        raise ValueError("Byes can't be reset.")
    if t.fmt == "single" and m.done:
        _clear_next(s, m)
    m.s1 = m.s2 = m.winner = None
    m.done = False
    s.add(m)
    if t.status == "done":
        t.status = "live"
        s.add(t)


def _check_complete(s: Session, t: Tournament):
    s.flush()
    ms = s.exec(select(Match).where(Match.tournament_id == t.id)).all()
    if ms and all(m.done for m in ms):
        t.status = "done"
        s.add(t)


def standings(matches: list[Match], ids: list[int]) -> list[dict]:
    table = {i: dict(id=i, w=0, l=0, d=0, pf=0, pa=0, pts=0, played=0) for i in ids}
    for m in matches:
        if not m.done or m.is_bye or m.p1 not in table or m.p2 not in table:
            continue
        a, b = table[m.p1], table[m.p2]
        a["pf"] += m.s1; a["pa"] += m.s2; b["pf"] += m.s2; b["pa"] += m.s1
        a["played"] += 1; b["played"] += 1
        if m.winner == m.p1:
            a["w"] += 1; b["l"] += 1; a["pts"] += 3
        elif m.winner == m.p2:
            b["w"] += 1; a["l"] += 1; b["pts"] += 3
        else:
            a["d"] += 1; b["d"] += 1; a["pts"] += 1; b["pts"] += 1
    return sorted(table.values(), key=lambda r: (-r["pts"], -(r["pf"] - r["pa"]), -r["pf"]))


def champion(t: Tournament, matches: list[Match], ids: list[int]):
    if t.status != "done":
        return None
    if t.fmt == "single":
        final = max(matches, key=lambda m: m.round, default=None)
        return final.winner if final else None
    rows = standings(matches, ids)
    return rows[0]["id"] if rows else None
