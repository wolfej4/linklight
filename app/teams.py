"""Team sign-ups. In team tournaments, bracket slots (Entrant/Match p1, p2, winner) hold team ids."""
from sqlmodel import Session, select

from .models import Attendee, Entrant, Team, TeamMember, Tournament


def sides(s: Session, t: Tournament) -> dict:
    """Whatever fills a bracket slot, keyed by id: attendees for solo tournaments, teams otherwise."""
    if t.teams:
        return {x.id: x for x in s.exec(select(Team).where(Team.tournament_id == t.id)).all()}
    return {a.id: a for a in s.exec(select(Attendee).where(Attendee.event_id == t.event_id)).all()}


def slot_for(s: Session, t: Tournament, attendee_id: int) -> int | None:
    """The bracket id this attendee plays under in t: their own id, or their team's."""
    if not t.teams:
        return attendee_id
    team = team_of(s, t, attendee_id)
    return team.id if team else None


def team_of(s: Session, t: Tournament, attendee_id: int) -> Team | None:
    return s.exec(select(Team).join(TeamMember, TeamMember.team_id == Team.id)
                  .where(Team.tournament_id == t.id, TeamMember.attendee_id == attendee_id)).first()


def members(s: Session, team_id: int) -> list[Attendee]:
    return list(s.exec(select(Attendee).join(TeamMember, TeamMember.attendee_id == Attendee.id)
                       .where(TeamMember.team_id == team_id).order_by(TeamMember.id)).all())


def _entered(s: Session, t: Tournament, attendee_id: int) -> bool:
    if t.teams:
        return team_of(s, t, attendee_id) is not None
    return s.exec(select(Entrant).where(Entrant.tournament_id == t.id, Entrant.attendee_id == attendee_id)).first() is not None


def _next_seed(s: Session, t: Tournament) -> int:
    return len(s.exec(select(Entrant).where(Entrant.tournament_id == t.id)).all()) + 1


def enter_solo(s: Session, t: Tournament, a: Attendee):
    if t.teams:
        raise ValueError("This is a team tournament. Create or join a team instead.")
    if t.status != "setup":
        raise ValueError("Sign-ups are closed. The bracket has started.")
    if _entered(s, t, a.id):
        raise ValueError("You're already signed up.")
    s.add(Entrant(tournament_id=t.id, attendee_id=a.id, seed=_next_seed(s, t)))


def withdraw_solo(s: Session, t: Tournament, attendee_id: int):
    if t.status != "setup":
        raise ValueError("The bracket has started. Ask an organizer to remove you.")
    for e in s.exec(select(Entrant).where(Entrant.tournament_id == t.id, Entrant.attendee_id == attendee_id)).all():
        s.delete(e)


def create_team(s: Session, t: Tournament, name: str, captain: Attendee) -> Team:
    name = name.strip()[:40]
    if not t.teams:
        raise ValueError("This tournament is for solo players.")
    if t.status != "setup":
        raise ValueError("Sign-ups are closed. The bracket has started.")
    if not name:
        raise ValueError("Give your team a name.")
    if s.exec(select(Team).where(Team.tournament_id == t.id, Team.name == name)).first():
        raise ValueError("Another team already has that name.")
    if _entered(s, t, captain.id):
        raise ValueError(f"{captain.display} is already on a team in this tournament.")
    team = Team(tournament_id=t.id, name=name, captain_id=captain.id)
    s.add(team)
    s.flush()
    s.add(TeamMember(team_id=team.id, attendee_id=captain.id))
    s.add(Entrant(tournament_id=t.id, attendee_id=captain.id, team_id=team.id, seed=_next_seed(s, t)))
    return team


def join_team(s: Session, t: Tournament, team: Team, a: Attendee):
    if t.status != "setup":
        raise ValueError("Sign-ups are closed. The bracket has started.")
    if _entered(s, t, a.id):
        raise ValueError(f"{a.display} is already on a team in this tournament.")
    if len(members(s, team.id)) >= t.team_size:
        raise ValueError(f"{team.name} is full.")
    s.add(TeamMember(team_id=team.id, attendee_id=a.id))


def leave_team(s: Session, t: Tournament, team: Team, attendee_id: int):
    """Remove a member. The next member becomes captain, and an empty team is disbanded."""
    if t.status != "setup":
        raise ValueError("The bracket has started. Ask an organizer to change the roster.")
    for m in s.exec(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.attendee_id == attendee_id)).all():
        s.delete(m)
    s.flush()
    rest = members(s, team.id)
    if not rest:
        disband(s, team)
        return
    if team.captain_id == attendee_id:
        team.captain_id = rest[0].id
        s.add(team)
        for e in s.exec(select(Entrant).where(Entrant.team_id == team.id)).all():
            e.attendee_id = team.captain_id
            s.add(e)


def disband(s: Session, team: Team):
    for model in (TeamMember, Entrant):
        for row in s.exec(select(model).where(model.team_id == team.id)).all():
            s.delete(row)
    s.delete(team)


def drop_from_signups(s: Session, attendee_id: int):
    """Take someone out of every tournament that's still taking sign-ups."""
    for t in s.exec(select(Tournament).where(Tournament.status == "setup")).all():
        if t.teams:
            team = team_of(s, t, attendee_id)
            if team:
                leave_team(s, t, team, attendee_id)
        else:
            withdraw_solo(s, t, attendee_id)


def in_started_tournament(s: Session, attendee_id: int) -> bool:
    for t in s.exec(select(Tournament).where(Tournament.status != "setup")).all():
        if t.teams:
            if team_of(s, t, attendee_id):
                return True
        elif s.exec(select(Entrant).where(Entrant.tournament_id == t.id, Entrant.attendee_id == attendee_id)).first():
            return True
    return False
