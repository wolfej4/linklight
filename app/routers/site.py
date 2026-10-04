"""The public website: homepage, event pages, news, checkout and tickets."""
import json
import logging
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, col, select
from starlette.concurrency import run_in_threadpool

from .. import config, mailer, payments, tickets
from ..auth import current_user, is_admin, require_user
from ..db import get_session
from ..models import Attendee, Event, Order, Post, Ticket, TicketType, TimetableItem, Tournament, User
from ..web import back, base_url, render

router = APIRouter()
log = logging.getLogger("lanparty.site")


def _visible_events(request: Request, s: Session) -> list[Event]:
    stmt = select(Event)
    if not is_admin(request):
        stmt = stmt.where(Event.published == True)  # noqa: E712
    return list(s.exec(stmt).all())


def _split(events: list[Event]) -> tuple[list[Event], list[Event]]:
    upcoming = sorted([e for e in events if not e.is_past], key=lambda e: (e.start is None, e.start or e.created_at))
    past = sorted([e for e in events if e.is_past], key=lambda e: e.end or e.start, reverse=True)
    return upcoming, past


def _posts(s: Session, limit: int | None = None, event_id: int | None = None) -> list[Post]:
    stmt = select(Post).where(Post.published == True)  # noqa: E712
    if event_id:
        stmt = stmt.where(Post.event_id == event_id)
    stmt = stmt.order_by(col(Post.published_at).desc())
    if limit:
        stmt = stmt.limit(limit)
    return list(s.exec(stmt).all())


def _not_found(request: Request, what: str, hint: str | None = None):
    resp = render(request, "not_found.html", event=None, what=what, hint=hint)
    resp.status_code = 404
    return resp


@router.get("/", response_class=HTMLResponse)
def home(request: Request, s: Session = Depends(get_session)):
    upcoming, past = _split(_visible_events(request, s))
    return render(request, "home.html", event=None, upcoming=upcoming, past=past[:12], posts=_posts(s, 3),
                  featured=upcoming[0] if upcoming else None, page="home")


@router.get("/events", response_class=HTMLResponse)
def events(request: Request, s: Session = Depends(get_session)):
    upcoming, past = _split(_visible_events(request, s))
    return render(request, "events.html", event=None, upcoming=upcoming, past=past, page="events")


def _public_event(request: Request, s: Session, eid: int) -> Event | None:
    ev = s.get(Event, eid)
    if not ev or (not ev.published and not is_admin(request)):
        return None
    return ev


@router.get("/events/{eid}", response_class=HTMLResponse)
def event_page(request: Request, eid: int, s: Session = Depends(get_session)):
    ev = _public_event(request, s, eid)
    if not ev:
        return _not_found(request, "event")
    user = current_user(request)
    types = tickets.on_sale_types(s, ev)
    people = s.exec(select(Attendee).where(Attendee.event_id == ev.id)).all()
    return render(
        request, "event.html", event=None, ev=ev, page="events", types=types,
        left={t.id: tickets.remaining(s, t) for t in types},
        timetable=s.exec(select(TimetableItem).where(TimetableItem.event_id == ev.id)
                         .order_by(TimetableItem.starts_at)).all(),
        tournaments=s.exec(select(Tournament).where(Tournament.event_id == ev.id).order_by(Tournament.id)).all(),
        going=len(people), handles=sorted({a.handle for a in people if a.handle}, key=str.lower),
        mine=tickets.ticket_of(s, ev.id, user.id) if user else None,
        posts=_posts(s, event_id=ev.id), providers=payments.enabled(), provider_names=payments.NAMES,
    )


# ---- checkout -----------------------------------------------------------------

@router.post("/events/{eid}/checkout")
async def checkout(request: Request, eid: int, s: Session = Depends(get_session)):
    form = await request.form()
    # The rest talks to the database and the payment provider, so keep it off the event loop.
    return await run_in_threadpool(_checkout, request, eid, {k: str(v) for k, v in form.items()}, s)


def _checkout(request: Request, eid: int, form: dict, s: Session):
    user = require_user(request)
    ev = _public_event(request, s, eid)
    if not ev:
        return back("/events", "That event isn't available.")
    quantities = {}
    for key, value in form.items():
        if key.startswith("qty_"):
            try:
                quantities[int(key[4:])] = max(0, int(value or 0))
            except ValueError:
                return back(f"/events/{eid}#tickets", "Ticket quantities should be numbers.")
    provider = form.get("provider", "")
    try:
        order = tickets.create_order(s, user, ev, quantities, for_me=form.get("for_me") == "1")
    except ValueError as e:
        return back(f"/events/{eid}#tickets", str(e))

    if order.total_cents == 0:
        order.provider = "free"
        tickets.fulfill(s, order)
        return back(f"/orders/{order.id}", notice="You're in!")
    if provider not in payments.enabled():
        tickets.cancel(s, order, "expired")
        s.commit()
        return back(f"/events/{eid}#tickets", "Choose how you'd like to pay.")

    base = base_url(request)
    order.provider = provider
    try:
        if provider == "stripe":
            lines = [(f"{ev.name}: {tt.name}", price, n) for tt, price, n in tickets.order_lines(s, order)]
            ref, url = payments.stripe_checkout(
                order.id, lines, user.email or "",
                success_url=f"{base}/orders/{order.id}/stripe?session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{base}/orders/{order.id}/cancel",
                expires_at=int(time.time()) + config.HOLD_MINUTES * 60 - 30,
            )
        else:
            ref, url = payments.paypal_create(
                order.id, order.total_cents, f"{ev.name} tickets",
                return_url=f"{base}/orders/{order.id}/paypal", cancel_url=f"{base}/orders/{order.id}/cancel",
            )
    except payments.PaymentError as e:
        tickets.cancel(s, order, "expired")
        s.commit()
        return back(f"/events/{eid}#tickets", str(e))
    order.provider_ref = ref
    s.add(order)
    s.commit()
    return RedirectResponse(url, status_code=303)


def _my_order(request: Request, s: Session, oid: int) -> Order | None:
    user = require_user(request)
    order = s.get(Order, oid)
    if not order or (order.user_id != user.id and not is_admin(request)):
        return None
    return order


@router.get("/orders/{oid}/stripe")
def stripe_return(request: Request, oid: int, session_id: str = "", s: Session = Depends(get_session)):
    order = _my_order(request, s, oid)
    if not order:
        return back("/account", "We couldn't find that order.")
    if order.status != "paid" and order.provider == "stripe" and session_id == order.provider_ref:
        try:
            tickets.confirm(s, order, payments.stripe_session(session_id), "Stripe")
        except payments.PaymentError as e:
            log.warning("Stripe lookup for order %s failed: %s", oid, e)
    return back(f"/orders/{oid}")


@router.get("/orders/{oid}/paypal")
def paypal_return(request: Request, oid: int, token: str = "", s: Session = Depends(get_session)):
    order = _my_order(request, s, oid)
    if not order:
        return back("/account", "We couldn't find that order.")
    if order.status == "paid":
        return back(f"/orders/{oid}")
    if order.provider != "paypal" or token != order.provider_ref:
        return back(f"/orders/{oid}", "That PayPal payment doesn't match this order.")
    if order.status in ("cancelled", "refunded"):
        return back(f"/orders/{oid}", "This order was cancelled, so we didn't take the payment.")
    tickets.check_expiry(s, order)
    if order.status == "expired":
        # Nothing has been charged yet. Only capture if the tickets are still there to sell.
        for tt, _price, n in tickets.order_lines(s, order):
            left = tickets.remaining(s, tt)
            if left is not None and left < n:
                return back(f"/orders/{oid}", "Your checkout timed out and those tickets sold in the meantime. "
                                              "You haven't been charged.")
    try:
        ok = tickets.confirm(s, order, payments.paypal_capture(token, order.id), "PayPal")
    except payments.PaymentError as e:
        return back(f"/orders/{oid}", str(e))
    if not ok:
        return back(f"/orders/{oid}", "PayPal didn't confirm the payment. If you were charged, contact an organizer.")
    return back(f"/orders/{oid}", notice="Payment received. You're in!")


@router.get("/orders/{oid}/cancel")
def checkout_cancelled(request: Request, oid: int, s: Session = Depends(get_session)):
    order = _my_order(request, s, oid)
    if order and order.status == "pending":
        if order.provider == "stripe" and order.provider_ref:
            payments.stripe_expire(order.provider_ref)
        tickets.cancel(s, order, "expired")
        s.commit()
        return back(f"/events/{order.event_id}#tickets", notice="Checkout cancelled. You haven't been charged.")
    return back(f"/orders/{oid}" if order else "/account")


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, s: Session = Depends(get_session)):
    payload = await request.body()
    return await run_in_threadpool(_stripe_event, request, payload, s)


def _stripe_event(request: Request, payload: bytes, s: Session):
    if not payments.stripe_verify_webhook(payload, request.headers.get("stripe-signature", "")):
        return JSONResponse({"error": "bad signature"}, status_code=400)
    try:
        conf = payments.stripe_event_confirmation(json.loads(payload))
    except ValueError:
        return JSONResponse({"error": "bad payload"}, status_code=400)
    if conf and conf.reference.isdigit():
        order = s.get(Order, int(conf.reference))
        if order and order.provider == "stripe":
            tickets.confirm(s, order, conf, "Stripe")
    return {"received": True}


@router.get("/orders/{oid}", response_class=HTMLResponse)
def order_page(request: Request, oid: int, s: Session = Depends(get_session)):
    order = _my_order(request, s, oid)
    if not order:
        return _not_found(request, "order")
    tickets.check_expiry(s, order)
    tix = tickets.order_tickets(s, order)
    holders = {t.id: s.get(User, t.holder_id) for t in tix if t.holder_id}
    return render(request, "order.html", event=None, order=order, ev=s.get(Event, order.event_id), tix=tix,
                  types={t.ticket_type_id: s.get(TicketType, t.ticket_type_id) for t in tix}, holders=holders,
                  lines=tickets.order_lines(s, order), can_email=mailer.enabled())


# ---- gifting ------------------------------------------------------------------

def _buyer_ticket(request: Request, s: Session, tid: int) -> tuple[Ticket | None, Order | None]:
    user = require_user(request)
    t = s.get(Ticket, tid)
    order = s.get(Order, t.order_id) if t else None
    if not t or not order or order.user_id != user.id or t.status != "active":
        return None, None
    return t, order


@router.post("/tickets/{tid}/take")
def take_ticket(request: Request, tid: int, s: Session = Depends(get_session)):
    """The buyer keeps a spare ticket for themselves."""
    t, order = _buyer_ticket(request, s, tid)
    if not t:
        return back("/account", "That ticket isn't yours to assign.")
    try:
        tickets.claim(s, t, require_user(request))
    except ValueError as e:
        return back(f"/orders/{order.id}", str(e))
    return back(f"/orders/{order.id}", notice="That ticket is yours.")


@router.post("/tickets/{tid}/send")
def send_ticket(request: Request, tid: int, email: str = Form(...), s: Session = Depends(get_session)):
    t, order = _buyer_ticket(request, s, tid)
    if not t or t.holder_id:
        return back("/account", "That ticket can't be sent.")
    email = email.strip()
    if "@" not in email:
        return back(f"/orders/{order.id}", "That doesn't look like an email address.")
    ev = s.get(Event, t.event_id)
    who = require_user(request).display
    sent = mailer.send(email, f"{who} sent you a ticket to {ev.name}",
                       f"{who} bought you a ticket to {ev.name}.\n\nClaim it here:\n{base_url(request)}/claim/{t.claim_token}\n\n"
                       f"You'll sign in or make an account, and then you're on the list.")
    if not sent:
        return back(f"/orders/{order.id}", "We couldn't send that email. Copy the link and send it yourself.")
    return back(f"/orders/{order.id}", notice=f"Sent to {email}.")


@router.post("/tickets/{tid}/release")
def release_ticket(request: Request, tid: int, s: Session = Depends(get_session)):
    """Take a ticket back from whoever holds it (buyer), or give up your own ticket (holder)."""
    user = require_user(request)
    t = s.get(Ticket, tid)
    order = s.get(Order, t.order_id) if t else None
    if not t or not order or t.status != "active" or user.id not in (order.user_id, t.holder_id):
        return back("/account", "That ticket isn't yours.")
    try:
        tickets.release(s, t)
        s.commit()
    except ValueError as e:
        s.rollback()
        return back("/account", str(e))
    if order.user_id == user.id:
        return back(f"/orders/{order.id}", notice="Ticket freed up. Send the new link to whoever should have it.")
    return back("/account", notice="You gave up that ticket. The person who bought it can pass it to someone else.")


@router.get("/claim/{token}", response_class=HTMLResponse)
def claim_page(request: Request, token: str, s: Session = Depends(get_session)):
    t = s.exec(select(Ticket).where(Ticket.claim_token == token)).first()
    if not t or t.status != "active" or t.holder_id:
        return _not_found(request, "ticket link",
                          "This link was already used or has been replaced. Ask whoever sent it for a new one.")
    return render(request, "claim.html", event=None, t=t, ev=s.get(Event, t.event_id),
                  tt=s.get(TicketType, t.ticket_type_id), token=token)


@router.post("/claim/{token}")
def claim_ticket(request: Request, token: str, s: Session = Depends(get_session)):
    user = require_user(request)
    t = s.exec(select(Ticket).where(Ticket.claim_token == token)).first()
    if not t:
        return back("/account", "This link was already used or has been replaced.")
    try:
        tickets.claim(s, t, user)
    except ValueError as e:
        return back(f"/claim/{token}", str(e))
    return back("/account", notice="Ticket claimed. See you there!")


# ---- news ---------------------------------------------------------------------

@router.get("/news", response_class=HTMLResponse)
def news(request: Request, s: Session = Depends(get_session)):
    posts = _posts(s)
    return render(request, "news.html", event=None, posts=posts, page="news",
                  events={e.id: e for e in s.exec(select(Event)).all()})


@router.get("/news/{pid}", response_class=HTMLResponse)
def news_post(request: Request, pid: int, s: Session = Depends(get_session)):
    p = s.get(Post, pid)
    if not p or (not p.published and not is_admin(request)):
        return _not_found(request, "post")
    return render(request, "post.html", event=None, post=p, page="news",
                  ev=s.get(Event, p.event_id) if p.event_id else None)
