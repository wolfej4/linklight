from datetime import datetime, timedelta

from sqlmodel import select

from app import oauth
from app.models import EmailCode, Event, Identity, Post, User
from app.routers.accounts import resolve_user

from .conftest import client


def _when(days: int) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def test_homepage_splits_upcoming_and_past_and_hides_drafts(db):
    db.add_all([
        Event(name="Next Month LAN", published=True, starts_at=_when(30)),
        Event(name="Last Year LAN", published=True, starts_at=_when(-365), ends_at=_when(-364)),
        Event(name="Secret Draft", published=False, starts_at=_when(5)),
        Post(title="Pizza vote", body="Pepperoni won.", published=True, published_at=datetime.now()),
        Post(title="Unfinished", body="...", published=False),
    ])
    db.commit()
    with client() as c:
        html = c.get("/").text
    upcoming, past = html.split("Past events")
    assert "Next Month LAN" in upcoming and "Last Year LAN" in past
    assert "Secret Draft" not in html
    assert "Pizza vote" in html and "Unfinished" not in html
    assert "Sign in" in html


def test_draft_event_page_is_404_for_public_but_visible_to_organizers(db, admin):
    ev = Event(name="Hidden", published=False)
    db.add(ev)
    db.commit()
    with client() as c:
        assert c.get(f"/events/{ev.id}").status_code == 404
    r = admin.get(f"/events/{ev.id}")
    assert r.status_code == 200 and "This event is a draft" in r.text


def test_email_link_sign_in_works_once(db, outbox):
    with client() as c:
        r = c.post("/login/email", data={"email": "Player@Example.com", "next": "/events"})
        assert "Check your email" in r.text
        assert outbox[0]["to"] == "player@example.com"
        token = outbox[0]["body"].split("/login/email/")[1].split()[0]
        # Opening the link only shows a button, so mail scanners don't burn it.
        assert "Sign in" in c.get(f"/login/email/{token}").text
        r = c.post(f"/login/email/{token}", follow_redirects=False)
        assert r.headers["location"] == "/events"
        assert "player@example.com" in c.get("/account").text
    with client() as other:
        r = other.post(f"/login/email/{token}", follow_redirects=False)
        assert "expired" in r.headers["location"]
    code = db.exec(select(EmailCode)).one()
    assert code.token_hash != token  # only the hash is stored


def test_email_links_are_rate_limited(outbox):
    with client() as c:
        c.post("/login/email", data={"email": "a@example.com"})
        r = c.post("/login/email", data={"email": "a@example.com"}, follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert len(outbox) == 1


def test_next_parameter_cannot_leave_the_site(outbox):
    with client() as c:
        c.post("/login/email", data={"email": "x@example.com", "next": "//evil.example/steal"})
        token = outbox[0]["body"].split("/login/email/")[1].split()[0]
        r = c.post(f"/login/email/{token}", follow_redirects=False)
        assert r.headers["location"] == "/"
        r = c.post("/login", data={"password": "organizer-pw", "next": "https://evil.example"}, follow_redirects=False)
        assert r.headers["location"] == "/admin"


def test_regular_users_cannot_reach_organizer_pages(make_user):
    c, _ = make_user()
    r = c.get("/admin")
    assert r.status_code == 403 and "Organizers only" in r.text
    assert c.post("/admin/events", data={"name": "nope"}).status_code == 403


def test_admin_emails_become_organizers(make_user):
    c, user = make_user(email="boss@example.com")
    assert user.is_admin
    assert c.get("/admin").status_code == 200


def test_organizer_can_promote_a_user(admin, make_user, db):
    c, user = make_user()
    admin.post(f"/admin/users/{user.id}/admin")
    db.refresh(user)
    assert user.is_admin
    assert c.get("/attendees").status_code == 200


def test_resolve_user_links_by_verified_email_only(db):
    existing = User(email="sam@example.com", name="Sam")
    db.add(existing)
    db.commit()
    unverified = resolve_user(db, oauth.Profile("discord", "111", email="sam@example.com", email_verified=False), None)
    assert unverified.id != existing.id and unverified.email is None
    verified = resolve_user(db, oauth.Profile("google", "222", email="SAM@example.com", email_verified=True), None)
    assert verified.id == existing.id
    again = resolve_user(db, oauth.Profile("google", "222"), None)
    assert again.id == existing.id


def test_linking_an_identity_owned_by_someone_else_fails(db):
    a = resolve_user(db, oauth.Profile("steam", "76561198000000001", handle="alpha"), None)
    b = resolve_user(db, oauth.Profile("discord", "999", handle="bravo"), None)
    try:
        resolve_user(db, oauth.Profile("steam", "76561198000000001"), b)
        raise AssertionError("expected a refusal")
    except ValueError as e:
        assert "already linked" in str(e)
    assert {i.user_id for i in db.exec(select(Identity)).all()} == {a.id, b.id}


def test_cannot_unlink_last_sign_in_method(db, make_user):
    c, user = make_user()
    ident = Identity(user_id=user.id, provider="steam", subject="76561198000000002")
    db.add(ident)
    db.commit()
    c.post("/account/email/remove")  # still has Steam, so this is fine
    r = c.post(f"/account/unlink/{ident.id}", follow_redirects=False)
    assert "Link+another" in r.headers["location"] or "Link%20another" in r.headers["location"]
    assert db.get(Identity, ident.id) is not None


def test_oauth_callback_rejects_a_mismatched_state(monkeypatch):
    monkeypatch.setattr(oauth, "enabled", lambda: ["steam"])
    with client() as c:
        r = c.get("/auth/steam/start", follow_redirects=False)
        assert r.headers["location"].startswith("https://steamcommunity.com/openid/login")
        r = c.get("/auth/steam/callback?state=forged", follow_redirects=False)
    assert r.headers["location"].startswith("/login?error=")


def test_steam_callback_checks_the_assertion_with_steam(monkeypatch):
    calls = []

    class FakeResp:
        status_code = 200
        text = "ns:http://specs.openid.net/auth/2.0\nis_valid:true\n"

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data):
            calls.append(data)
            return FakeResp()

    monkeypatch.setattr(oauth.httpx, "Client", FakeClient)
    monkeypatch.setattr(oauth, "enabled", lambda: ["steam"])
    with client() as c:
        loc = c.get("/auth/steam/start?next=/events", follow_redirects=False).headers["location"]
        state = loc.split("state%3D")[1].split("&")[0]
        steamid = "76561198000000042"
        params = {
            "state": state,
            "openid.ns": "http://specs.openid.net/auth/2.0", "openid.mode": "id_res",
            "openid.op_endpoint": "https://steamcommunity.com/openid/login",
            "openid.claimed_id": f"https://steamcommunity.com/openid/id/{steamid}",
            "openid.identity": f"https://steamcommunity.com/openid/id/{steamid}",
            "openid.return_to": f"http://testserver/auth/steam/callback?state={state}",
            "openid.response_nonce": "2026-01-01T00:00:00Zabc", "openid.assoc_handle": "1234567890",
            "openid.signed": "signed,op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
            "openid.sig": "abc=",
        }
        r = c.get("/auth/steam/callback", params=params, follow_redirects=False)
        assert r.headers["location"] == "/events"
        assert calls[0]["openid.mode"] == "check_authentication"
        assert "Steam" in c.get("/account").text


def test_apple_client_secret_is_an_es256_jwt(monkeypatch):
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app import config
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    monkeypatch.setattr(config, "APPLE_PRIVATE_KEY", pem)
    monkeypatch.setattr(config, "APPLE_TEAM_ID", "TEAM123")
    monkeypatch.setattr(config, "APPLE_KEY_ID", "KEY456")
    monkeypatch.setattr(config, "APPLE_CLIENT_ID", "com.example.lan")
    token = oauth.apple_client_secret()
    assert jwt.get_unverified_header(token)["kid"] == "KEY456"
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], audience="https://appleid.apple.com")
    assert claims["iss"] == "TEAM123" and claims["sub"] == "com.example.lan"


def test_id_token_checks_audience_and_nonce():
    import time

    import jwt
    good = {"iss": "https://appleid.apple.com", "aud": "svc", "sub": "u1", "nonce": "n1", "exp": time.time() + 60}
    tok = jwt.encode(good, "k", algorithm="HS256")
    assert oauth._id_claims(tok, ("https://appleid.apple.com",), "svc", "n1")["sub"] == "u1"
    for bad in ({"aud": "other"}, {"nonce": "n2"}, {"exp": time.time() - 5}):
        try:
            oauth._id_claims(jwt.encode({**good, **bad}, "k", algorithm="HS256"), ("https://appleid.apple.com",), "svc", "n1")
            raise AssertionError(f"accepted {bad}")
        except oauth.OAuthError:
            pass


def test_names_with_quotes_cannot_break_out_of_confirm_dialogs(admin, db, event):
    from app.models import Attendee
    a = Attendee(event_id=event.id, name="x');alert(1);//")
    db.add(a)
    db.commit()
    html = admin.get(f"/attendees/{a.id}").text
    assert "onsubmit" not in html
    assert 'data-confirm="Remove x&#39;);alert(1);//?"' in html


def test_login_page_lists_every_option(monkeypatch):
    from app import mailer
    monkeypatch.setattr(oauth, "enabled", lambda: ["steam"])
    monkeypatch.setattr(mailer, "enabled", lambda: False)
    with client() as c:
        html = c.get("/login").text
    for name in ("Steam", "Discord", "Google", "Apple", "email"):
        assert f"Login with {name}" in html
    assert 'href="/auth/steam/start' in html
    assert 'href="/auth/discord/start' not in html  # not configured, so shown but disabled
    assert "Email login isn't set up yet" in html
    monkeypatch.setattr(oauth, "enabled", lambda: list(oauth.PROVIDERS))
    monkeypatch.setattr(mailer, "enabled", lambda: True)
    with client() as c:
        html = c.get("/login").text
    assert all(f'href="/auth/{p}/start' in html for p in oauth.PROVIDERS)
    assert "Not set up yet" not in html
