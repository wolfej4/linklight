import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta

from sqlmodel import select

from app import payments
from app.models import Attendee, Order, Ticket
from app.payments import Confirmation


def _checkout(c, event, tt, qty=1, provider="stripe", for_me=True):
    data = {f"qty_{tt.id}": str(qty), "provider": provider}
    if for_me:
        data["for_me"] = "1"
    return c.post(f"/events/{event.id}/checkout", data=data, follow_redirects=False)


def _order(db, **where) -> Order:
    db.expire_all()
    return db.exec(select(Order).order_by(Order.id.desc())).first()


def test_free_tickets_are_issued_right_away(make_user, db, event, free_type):
    c, user = make_user(name="Riley", handle="rly", watts=600)
    r = _checkout(c, event, free_type, qty=1, provider="")
    assert r.headers["location"].startswith("/orders/")
    order = _order(db)
    assert order.status == "paid" and order.provider == "free"
    a = db.exec(select(Attendee).where(Attendee.user_id == user.id)).one()
    assert (a.event_id, a.handle, a.watts, a.paid) == (event.id, "rly", 600, True)
    page = c.get("/account").text
    assert "Fall LAN" in page and f"/p/{a.token}" in page


def test_signed_out_visitors_are_sent_to_sign_in(event, free_type):
    from .conftest import client
    with client() as c:
        r = c.post(f"/events/{event.id}/checkout", data={f"qty_{free_type.id}": "1"},
                   headers={"referer": f"http://testserver/events/{event.id}"}, follow_redirects=False)
    assert r.headers["location"] == f"/login?next=/events/{event.id}"


def test_stripe_checkout_holds_stock_until_it_expires(make_user, db, event, ticket_type, stripe_on, monkeypatch):
    ticket_type.quantity = 1
    db.add(ticket_type)
    db.commit()
    seen = {}

    def fake_checkout(order_id, lines, email, success_url, cancel_url, expires_at):
        seen.update(order_id=order_id, lines=lines, success_url=success_url, expires_at=expires_at)
        return "cs_test_1", "https://checkout.stripe.com/c/pay/cs_test_1"

    monkeypatch.setattr(payments, "stripe_checkout", fake_checkout)
    a, _ = make_user()
    b, _ = make_user()
    r = _checkout(a, event, ticket_type)
    assert r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    assert seen["lines"] == [("Fall LAN: BYOC", 2500, 1)]
    assert "{CHECKOUT_SESSION_ID}" in seen["success_url"]
    assert seen["expires_at"] - time.time() >= 30 * 60  # Stripe's minimum
    r = _checkout(b, event, ticket_type)
    assert "sold+out" in r.headers["location"] or "sold%20out" in r.headers["location"]
    # Once the hold runs out, the ticket is back on sale.
    order = _order(db)
    order.created_at = datetime.now() - timedelta(minutes=45)
    db.add(order)
    db.commit()
    assert _checkout(b, event, ticket_type).headers["location"].startswith("https://checkout.stripe.com")


def _signed(payload: bytes, secret="whsec_test", ts=None) -> str:
    ts = str(int(ts or time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _paid_session_event(order: Order, amount=None, currency="usd") -> bytes:
    return json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "id": order.provider_ref, "client_reference_id": str(order.id), "payment_status": "paid",
        "amount_total": order.total_cents if amount is None else amount, "currency": currency,
        "payment_intent": "pi_123"}}}).encode()


def test_stripe_webhook_issues_tickets(make_user, db, event, ticket_type, stripe_on, monkeypatch):
    monkeypatch.setattr(payments, "stripe_checkout", lambda *a, **k: ("cs_1", "https://checkout.stripe.com/x"))
    c, user = make_user()
    _checkout(c, event, ticket_type, qty=2)
    order = _order(db)
    body = _paid_session_event(order)
    from .conftest import client
    with client() as hook:
        assert hook.post("/webhooks/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
        forged = _signed(body, secret="whsec_attacker")
        assert hook.post("/webhooks/stripe", content=body, headers={"stripe-signature": forged}).status_code == 400
        old = _signed(body, ts=time.time() - 3600)
        assert hook.post("/webhooks/stripe", content=body, headers={"stripe-signature": old}).status_code == 400
        assert _order(db).status == "pending"
        r = hook.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)})
        assert r.status_code == 200
        hook.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)})  # retries are harmless
    order = _order(db)
    assert order.status == "paid" and order.provider_payment == "pi_123"
    tix = db.exec(select(Ticket).where(Ticket.order_id == order.id)).all()
    assert [t.status for t in tix] == ["active", "active"]
    assert [t.holder_id for t in tix] == [user.id, None]
    assert len(db.exec(select(Attendee)).all()) == 1


def test_stripe_webhook_ignores_the_wrong_amount(make_user, db, event, ticket_type, stripe_on, monkeypatch):
    monkeypatch.setattr(payments, "stripe_checkout", lambda *a, **k: ("cs_1", "https://checkout.stripe.com/x"))
    c, _ = make_user()
    _checkout(c, event, ticket_type)
    order = _order(db)
    for body in (_paid_session_event(order, amount=1), _paid_session_event(order, currency="eur")):
        c.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)})
    assert _order(db).status == "pending"


def test_stripe_return_page_confirms_without_waiting_for_the_webhook(make_user, db, event, ticket_type, stripe_on, monkeypatch):
    monkeypatch.setattr(payments, "stripe_checkout", lambda *a, **k: ("cs_9", "https://checkout.stripe.com/x"))
    c, _ = make_user()
    _checkout(c, event, ticket_type)
    order = _order(db)
    monkeypatch.setattr(payments, "stripe_session", lambda sid: Confirmation(
        paid=True, amount_cents=2500, currency="USD", payment_ref="pi_9", reference=str(order.id)))
    c.get(f"/orders/{order.id}/stripe?session_id=wrong")
    assert _order(db).status == "pending"
    r = c.get(f"/orders/{order.id}/stripe?session_id=cs_9")
    assert r.status_code == 200 and _order(db).status == "paid"


def test_other_people_cannot_see_your_order(make_user, db, event, free_type):
    a, _ = make_user()
    b, _ = make_user()
    _checkout(a, event, free_type, provider="")
    order = _order(db)
    assert a.get(f"/orders/{order.id}").status_code == 200
    assert b.get(f"/orders/{order.id}").status_code == 404


def test_paypal_capture_on_return(make_user, db, event, ticket_type, paypal_on, monkeypatch):
    monkeypatch.setattr(payments, "paypal_create", lambda *a, **k: ("PP-ORDER-1", "https://www.sandbox.paypal.com/pay?token=PP-ORDER-1"))
    captured = []

    def fake_capture(pp_id, order_id):
        captured.append(pp_id)
        return Confirmation(paid=True, amount_cents=5000, currency="USD", payment_ref="CAP-1", reference=str(order_id))

    monkeypatch.setattr(payments, "paypal_capture", fake_capture)
    c, user = make_user()
    r = _checkout(c, event, ticket_type, qty=2, provider="paypal")
    assert r.headers["location"].startswith("https://www.sandbox.paypal.com/")
    order = _order(db)
    c.get(f"/orders/{order.id}/paypal?token=SOMEONE-ELSES")
    assert not captured
    r = c.get(f"/orders/{order.id}/paypal?token=PP-ORDER-1", follow_redirects=False)
    assert "notice=" in r.headers["location"]
    order = _order(db)
    assert order.status == "paid" and order.provider_payment == "CAP-1"


def test_paypal_does_not_capture_a_timed_out_order_when_stock_is_gone(make_user, db, event, ticket_type, paypal_on, monkeypatch):
    ticket_type.quantity = 1
    db.add(ticket_type)
    db.commit()
    monkeypatch.setattr(payments, "paypal_create", lambda *a, **k: ("PP-A", "https://paypal.test/a"))
    monkeypatch.setattr(payments, "paypal_capture", lambda *a: (_ for _ in ()).throw(AssertionError("captured")))
    a, _ = make_user()
    b, _ = make_user()
    _checkout(a, event, ticket_type, provider="paypal")
    slow = _order(db)
    slow.created_at = datetime.now() - timedelta(hours=1)
    db.add(slow)
    db.commit()
    monkeypatch.setattr(payments, "paypal_create", lambda *a, **k: ("PP-B", "https://paypal.test/b"))
    _checkout(b, event, ticket_type, provider="paypal")  # takes the freed-up ticket
    r = a.get(f"/orders/{slow.id}/paypal?token=PP-A", follow_redirects=False)
    assert "haven" in r.headers["location"]  # "You haven't been charged"


def test_gifting_and_claiming_a_spare_ticket(make_user, db, event, free_type, outbox):
    buyer, b_user = make_user(handle="buyer")
    friend, f_user = make_user(handle="friend")
    _checkout(buyer, event, free_type, qty=2, provider="")
    order = _order(db)
    spare = db.exec(select(Ticket).where(Ticket.order_id == order.id, Ticket.holder_id == None)).one()  # noqa: E711
    assert spare.claim_token in buyer.get(f"/orders/{order.id}").text
    buyer.post(f"/tickets/{spare.id}/send", data={"email": "friend@example.com"})
    assert spare.claim_token in outbox[-1]["body"]
    # The friend can't use someone else's buyer controls...
    assert "isn" in friend.post(f"/tickets/{spare.id}/take", follow_redirects=False).headers["location"]
    # ...but can claim with the link, once.
    assert "Claim ticket" in friend.get(f"/claim/{spare.claim_token}").text
    friend.post(f"/claim/{spare.claim_token}")
    db.refresh(spare)
    assert spare.holder_id == f_user.id
    assert friend.get(f"/claim/{spare.claim_token}").status_code == 404
    handles = sorted(a.handle for a in db.exec(select(Attendee).where(Attendee.event_id == event.id)).all())
    assert handles == ["buyer", "friend"]


def test_one_ticket_per_person_per_event(make_user, db, event, free_type):
    buyer, _ = make_user()
    _checkout(buyer, event, free_type, qty=2, provider="")
    spare = db.exec(select(Ticket).where(Ticket.holder_id == None)).one()  # noqa: E711
    r = buyer.post(f"/claim/{spare.claim_token}", follow_redirects=False)
    assert "already+have" in r.headers["location"] or "already%20have" in r.headers["location"]


def test_buyer_can_take_a_ticket_back_and_reissue_it(make_user, db, event, free_type):
    buyer, _ = make_user()
    friend, f_user = make_user()
    _checkout(buyer, event, free_type, qty=2, provider="")
    spare = db.exec(select(Ticket).where(Ticket.holder_id == None)).one()  # noqa: E711
    old_link = spare.claim_token
    friend.post(f"/claim/{old_link}")
    db.refresh(spare)
    buyer.post(f"/tickets/{spare.id}/release")
    db.refresh(spare)
    assert spare.holder_id is None and spare.claim_token != old_link
    assert db.exec(select(Attendee).where(Attendee.user_id == f_user.id)).first() is None


def test_checked_in_tickets_cannot_be_released(make_user, db, event, free_type):
    c, user = make_user()
    _checkout(c, event, free_type, provider="")
    a = db.exec(select(Attendee).where(Attendee.user_id == user.id)).one()
    a.checked_in_at = datetime.now()
    db.add(a)
    db.commit()
    t = db.exec(select(Ticket).where(Ticket.holder_id == user.id)).one()
    r = c.post(f"/tickets/{t.id}/release", follow_redirects=False)
    assert "door" in r.headers["location"]


def test_refund_voids_tickets_and_frees_seats(make_user, admin, db, event, ticket_type, stripe_on, monkeypatch):
    monkeypatch.setattr(payments, "stripe_checkout", lambda *a, **k: ("cs_r", "https://checkout.stripe.com/x"))
    refunds = []
    monkeypatch.setattr(payments, "stripe_refund", lambda pi, oid: refunds.append(pi))
    c, user = make_user()
    _checkout(c, event, ticket_type)
    order = _order(db)
    body = _paid_session_event(order)
    c.post("/webhooks/stripe", content=body, headers={"stripe-signature": _signed(body)})
    assert db.exec(select(Attendee).where(Attendee.user_id == user.id)).first()
    # Organizers can't delete a ticket holder from the attendee list directly.
    a = db.exec(select(Attendee).where(Attendee.user_id == user.id)).one()
    assert "has+a+ticket" in admin.post(f"/attendees/{a.id}/delete", follow_redirects=False).headers["location"] \
        or "has%20a%20ticket" in admin.post(f"/attendees/{a.id}/delete", follow_redirects=False).headers["location"]
    admin.post(f"/admin/orders/{order.id}/refund")
    assert refunds == ["pi_123"]
    assert _order(db).status == "refunded"
    assert db.exec(select(Attendee).where(Attendee.user_id == user.id)).first() is None
    assert {t.status for t in db.exec(select(Ticket)).all()} == {"void"}


def test_cannot_order_more_than_the_limit(make_user, event, ticket_type, stripe_on):
    c, _ = make_user()
    r = _checkout(c, event, ticket_type, qty=5)
    assert "up+to+4" in r.headers["location"] or "up%20to%204" in r.headers["location"]


def test_past_events_do_not_sell(make_user, db, event, free_type):
    event.starts_at = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M")
    event.ends_at = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    db.add(event)
    db.commit()
    c, _ = make_user()
    r = _checkout(c, event, free_type, provider="")
    assert "error=" in r.headers["location"]
    assert db.exec(select(Order)).first() is None


def test_admin_prices_parse_strictly(admin, db, event):
    admin.post("/admin/tickets", data={"name": "VIP", "price": "$1,234.5", "quantity": "5", "max_per_order": "2"})
    from app.models import TicketType
    tt = db.exec(select(TicketType).where(TicketType.name == "VIP")).one()
    assert tt.price_cents == 123450
    r = admin.post("/admin/tickets", data={"name": "Bad", "price": "12.345"}, follow_redirects=False)
    assert "error=" in r.headers["location"]


def test_webhook_signature_helper_matches_stripe_format(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "STRIPE_WEBHOOK_SECRET", "whsec_abc")
    payload = b'{"id":"evt_1"}'
    assert payments.stripe_verify_webhook(payload, _signed(payload, "whsec_abc"))
    assert not payments.stripe_verify_webhook(payload, _signed(payload, "whsec_other"))
    assert not payments.stripe_verify_webhook(payload + b" ", _signed(payload, "whsec_abc"))
