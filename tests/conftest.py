import os
import tempfile

# Settings are read at import time, so point everything at a scratch folder first.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lanparty-test-")
os.environ["ADMIN_PASSWORD"] = "organizer-pw"
os.environ["ADMIN_EMAILS"] = "boss@example.com"
os.environ["EMAIL_TO_LOG"] = "1"

from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel, select  # noqa: E402

from app import config, mailer  # noqa: E402
from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Event, TicketType, User  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield


@pytest.fixture
def outbox(monkeypatch):
    sent = []

    def fake_send(to, subject, body):
        sent.append(dict(to=to, subject=subject, body=body))
        return True

    monkeypatch.setattr(mailer, "send", fake_send)
    return sent


@pytest.fixture
def db():
    with Session(engine, expire_on_commit=False) as s:
        yield s


def client() -> TestClient:
    return TestClient(app, base_url="http://testserver")


@pytest.fixture
def admin():
    c = client()
    c.__enter__()
    r = c.post("/login", data={"password": "organizer-pw", "next": "/admin"}, follow_redirects=False)
    assert r.status_code == 303
    yield c
    c.__exit__(None, None, None)


def login_as(db: Session, **fields) -> tuple[TestClient, User]:
    """A client signed in as a fresh user, using the email link flow end to end."""
    email = fields.pop("email", None) or f"user{datetime.now().timestamp()}@example.com"
    c = client()
    c.__enter__()
    captured = []
    real = mailer.send
    mailer.send = lambda to, subject, body: captured.append(body) or True
    try:
        r = c.post("/login/email", data={"email": email, "next": "/account"})
        assert r.status_code == 200, r.text
    finally:
        mailer.send = real
    token = captured[-1].split("/login/email/")[1].split()[0]
    r = c.post(f"/login/email/{token}", follow_redirects=False)
    assert r.status_code == 303
    user = db.exec(select(User).where(User.email == email)).one()
    for k, v in fields.items():
        setattr(user, k, v)
    db.add(user)
    db.commit()
    return c, user


@pytest.fixture
def make_user(db):
    made = []

    def _make(**fields):
        c, u = login_as(db, **fields)
        made.append(c)
        return c, u

    yield _make
    for c in made:
        c.__exit__(None, None, None)


@pytest.fixture
def event(db):
    ev = Event(name="Fall LAN", published=True, active=True,
               starts_at=(datetime.now() + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M"),
               ends_at=(datetime.now() + timedelta(days=11)).strftime("%Y-%m-%dT%H:%M"), venue="Garage")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


@pytest.fixture
def ticket_type(db, event):
    tt = TicketType(event_id=event.id, name="BYOC", price_cents=2500, quantity=10, max_per_order=4)
    db.add(tt)
    db.commit()
    db.refresh(tt)
    return tt


@pytest.fixture
def free_type(db, event):
    tt = TicketType(event_id=event.id, name="Free entry", price_cents=0, quantity=0, max_per_order=4)
    db.add(tt)
    db.commit()
    db.refresh(tt)
    return tt


@pytest.fixture
def stripe_on(monkeypatch):
    monkeypatch.setattr(config, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_test")


@pytest.fixture
def paypal_on(monkeypatch):
    monkeypatch.setattr(config, "PAYPAL_CLIENT_ID", "pp-id")
    monkeypatch.setattr(config, "PAYPAL_CLIENT_SECRET", "pp-secret")
