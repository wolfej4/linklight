import sqlite3
from datetime import datetime, timedelta

from sqlalchemy import inspect
from sqlmodel import select

from app.models import Attendee, Match, Team, TimetableItem, Tournament

from .conftest import client


def _ticket_holder(make_user, db, event, free_type, handle):
    c, user = make_user(handle=handle)
    c.post(f"/events/{event.id}/checkout", data={f"qty_{free_type.id}": "1", "for_me": "1"})
    a = db.exec(select(Attendee).where(Attendee.user_id == user.id)).one()
    return c, a


def test_team_signup_bracket_and_score_reporting(make_user, admin, db, event, free_type):
    admin.post("/tournaments", data={"name": "2v2 Rocket", "fmt": "single", "best_of": "1", "team_size": "2",
                                     "signups_open": "1"})
    t = db.exec(select(Tournament)).one()
    assert t.teams and t.signups_open
    players = [_ticket_holder(make_user, db, event, free_type, h) for h in ("ann", "bob", "cat", "dan", "eve")]
    (ann, a_ann), (bob, a_bob), (cat, a_cat), (dan, a_dan), (eve, _) = players

    ann.post(f"/t/{t.id}/teams", data={"name": "Orange"})
    cat.post(f"/t/{t.id}/teams", data={"name": "Blue"})
    orange = db.exec(select(Team).where(Team.name == "Orange")).one()
    blue = db.exec(select(Team).where(Team.name == "Blue")).one()
    assert orange.join_code in ann.get(f"/t/{t.id}").text
    r = bob.post(f"/t/{t.id}/teams/join", data={"code": "nope"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    bob.post(f"/t/{t.id}/teams/join", data={"code": orange.join_code.lower()})
    dan.post(f"/t/{t.id}/teams/join", data={"code": blue.join_code})
    r = eve.post(f"/t/{t.id}/teams/join", data={"code": blue.join_code}, follow_redirects=False)
    assert "full" in r.headers["location"]

    admin.post(f"/tournaments/{t.id}/start")
    db.expire_all()
    final = db.exec(select(Match).where(Match.tournament_id == t.id)).one()
    assert {final.p1, final.p2} == {orange.id, blue.id}

    # Bob reports from his player page; the score is recorded for his team.
    page = bob.get(f"/p/{a_bob.token}").text
    assert "Playing for Orange" in page and "vs <b>Blue</b>" in page
    bob.post(f"/p/{a_bob.token}/report/{final.id}", data={"mine": "3", "theirs": "1"})
    db.expire_all()
    final = db.get(Match, final.id)
    assert final.done and final.winner == orange.id
    assert db.get(Tournament, t.id).status == "done"
    assert "Orange" in client().get(f"/t/{t.id}").text
    # Someone not in the match can't report it.
    r = eve.post(f"/p/{players[4][1].token}/report/{final.id}", data={"mine": "9", "theirs": "0"}, follow_redirects=False)
    assert "own+matches" in r.headers["location"] or "own%20matches" in r.headers["location"]


def test_captain_leaving_hands_over_or_disbands(make_user, admin, db, event, free_type):
    admin.post("/tournaments", data={"name": "Duos", "team_size": "2", "signups_open": "1"})
    t = db.exec(select(Tournament)).one()
    (ann, a_ann), (bob, a_bob) = [_ticket_holder(make_user, db, event, free_type, h) for h in ("ann", "bob")]
    ann.post(f"/t/{t.id}/teams", data={"name": "Duo"})
    team = db.exec(select(Team)).one()
    bob.post(f"/t/{t.id}/teams/join", data={"code": team.join_code})
    ann.post(f"/t/{t.id}/leave")
    db.refresh(team)
    assert team.captain_id == a_bob.id
    bob.post(f"/t/{t.id}/leave")
    assert db.exec(select(Team)).first() is None


def test_solo_signup_needs_a_ticket(make_user, admin, db, event, free_type):
    admin.post("/tournaments", data={"name": "1v1", "team_size": "1", "signups_open": "1"})
    t = db.exec(select(Tournament)).one()
    no_ticket, _ = make_user()
    r = no_ticket.post(f"/t/{t.id}/join", follow_redirects=False)
    assert "ticket" in r.headers["location"]
    c, a = _ticket_holder(make_user, db, event, free_type, "solo")
    c.post(f"/t/{t.id}/join")
    assert "signed up as <strong>solo" in c.get(f"/t/{t.id}").text
    admin.post(f"/tournaments/{t.id}/signups")  # organizer closes sign-ups
    r = c.post(f"/t/{t.id}/leave", follow_redirects=False)
    assert "closed" in r.headers["location"]


def test_timetable_shows_on_event_page_and_big_screen(admin, db, event):
    soon = datetime.now() + timedelta(hours=1)
    admin.post("/admin/timetable", data={"title": "Doors open", "starts_at": soon.strftime("%Y-%m-%dT%H:%M")})
    admin.post("/admin/timetable", data={"title": "Old thing", "starts_at": "2020-01-01T10:00"})
    assert len(db.exec(select(TimetableItem)).all()) == 2
    with client() as c:
        assert "Doors open" in c.get(f"/events/{event.id}").text
        tv = c.get("/display").text
    assert "Doors open" in tv and "Old thing" not in tv


def test_news_posts_publish_and_drafts_stay_hidden(admin, db, event):
    admin.post("/admin/news", data={"title": "Bring a power strip", "body": "One per person.\n\n<script>x</script>",
                                    "event_id": str(event.id), "published": "1"})
    admin.post("/admin/news", data={"title": "Not yet", "body": "draft"})
    with client() as c:
        news = c.get("/news").text
        assert "Bring a power strip" in news and "Not yet" not in news
        ev_page = c.get(f"/events/{event.id}").text
        assert "Bring a power strip" in ev_page
        assert "<script>x</script>" not in ev_page and "&lt;script&gt;" in ev_page


def test_old_databases_get_new_columns(tmp_path, monkeypatch):
    """A database from before accounts and tickets upgrades in place."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE event (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, starts_at VARCHAR NOT NULL,
            venue VARCHAR NOT NULL, announcement VARCHAR NOT NULL, active BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
        INSERT INTO event VALUES (1, 'Old LAN', 'Sat Oct 17, 2 PM', 'Garage', '', 1, '2025-01-01 00:00:00');
        CREATE TABLE tournament (id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, name VARCHAR NOT NULL,
            game VARCHAR NOT NULL, fmt VARCHAR NOT NULL, status VARCHAR NOT NULL, best_of INTEGER NOT NULL,
            created_at DATETIME NOT NULL);
        INSERT INTO tournament VALUES (1, 1, 'CS2', '', 'single', 'setup', 1, '2025-01-01 00:00:00');
    """)
    con.commit()
    con.close()
    from sqlmodel import Session, SQLModel, create_engine

    from app import db as dbmod
    old = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(dbmod, "engine", old)
    dbmod.init_db()
    cols = {c["name"] for c in inspect(old).get_columns("event")}
    assert {"published", "ends_at", "description", "address"} <= cols
    with Session(old) as s:
        from app.models import Event
        ev = s.get(Event, 1)
        assert ev.published is False and ev.starts_at == "Sat Oct 17, 2 PM" and ev.start is None
        t = s.get(Tournament, 1)
        assert t.team_size == 1 and t.signups_open is False
    assert "ix_attendee_user_id" in {i["name"] for i in inspect(old).get_indexes("attendee")}
    SQLModel.metadata.drop_all(old)
