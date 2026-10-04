"""Ticket sales: stock holds, orders, payment confirmation, and giving tickets to people.

A paid ticket held by someone becomes an Attendee row for that event, so the door, seating,
tournaments and player pages all work the same for walk-ins and online buyers.
"""
import logging
import threading
from datetime import datetime, timedelta

from sqlmodel import Session, col, func, select

from . import config, mailer, teams
from .models import Attendee, Event, Order, Ticket, TicketType, User
from .payments import Confirmation

log = logging.getLogger("lanparty.tickets")

# Checking stock and reserving it has to happen as one step, or two buyers can get the last seat.
_sale_lock = threading.Lock()
MAX_TICKETS_PER_ORDER = 20


def hold_cutoff() -> datetime:
    return datetime.now() - timedelta(minutes=config.HOLD_MINUTES)


def taken(s: Session, tt_id: int) -> int:
    """Tickets sold plus tickets held by checkouts that haven't expired."""
    sold = s.exec(select(func.count()).select_from(Ticket)
                  .where(Ticket.ticket_type_id == tt_id, Ticket.status == "active")).one()
    held = s.exec(select(func.count()).select_from(Ticket).join(Order, Order.id == Ticket.order_id)
                  .where(Ticket.ticket_type_id == tt_id, Ticket.status == "pending", Order.status == "pending",
                         Order.created_at > hold_cutoff())).one()
    return sold + held


def remaining(s: Session, tt: TicketType) -> int | None:
    return None if not tt.quantity else max(0, tt.quantity - taken(s, tt.id))


def ticket_of(s: Session, event_id: int, user_id: int) -> Ticket | None:
    return s.exec(select(Ticket).where(Ticket.event_id == event_id, Ticket.holder_id == user_id,
                                       Ticket.status == "active")).first()


def on_sale_types(s: Session, ev: Event) -> list[TicketType]:
    return list(s.exec(select(TicketType).where(TicketType.event_id == ev.id, TicketType.on_sale == True)  # noqa: E712
                       .order_by(TicketType.sort, TicketType.id)).all())


def create_order(s: Session, user: User, ev: Event, quantities: dict[int, int], for_me: bool) -> Order:
    if not ev.published or ev.is_past:
        raise ValueError("Tickets for this event aren't on sale.")
    wanted = {k: v for k, v in quantities.items() if v > 0}
    if not wanted:
        raise ValueError("Pick at least one ticket.")
    if sum(wanted.values()) > MAX_TICKETS_PER_ORDER:
        raise ValueError(f"You can buy up to {MAX_TICKETS_PER_ORDER} tickets at once.")
    with _sale_lock:
        lines = []
        for tt_id, qty in wanted.items():
            tt = s.get(TicketType, tt_id)
            if not tt or tt.event_id != ev.id or not tt.on_sale:
                raise ValueError("One of those tickets isn't on sale anymore.")
            if qty > tt.max_per_order:
                raise ValueError(f"You can buy up to {tt.max_per_order} {tt.name} tickets per order.")
            left = remaining(s, tt)
            if left is not None and qty > left:
                raise ValueError(f"Only {left} {tt.name} tickets are left." if left else f"{tt.name} is sold out.")
            lines.append((tt, qty))
        order = Order(event_id=ev.id, user_id=user.id, currency=config.CURRENCY,
                      for_me=for_me and ticket_of(s, ev.id, user.id) is None,
                      total_cents=sum(tt.price_cents * q for tt, q in lines))
        s.add(order)
        s.flush()
        for tt, qty in lines:
            for _ in range(qty):
                s.add(Ticket(event_id=ev.id, order_id=order.id, ticket_type_id=tt.id, price_cents=tt.price_cents))
        s.commit()
        s.refresh(order)
        return order


def order_lines(s: Session, order: Order) -> list[tuple[TicketType, int, int]]:
    """(ticket type, unit price, quantity) for each kind of ticket in the order."""
    counts: dict[tuple[int, int], int] = {}
    for t in s.exec(select(Ticket).where(Ticket.order_id == order.id)).all():
        counts[(t.ticket_type_id, t.price_cents)] = counts.get((t.ticket_type_id, t.price_cents), 0) + 1
    return [(s.get(TicketType, tt_id), price, n) for (tt_id, price), n in counts.items()]


def order_tickets(s: Session, order: Order) -> list[Ticket]:
    return list(s.exec(select(Ticket).where(Ticket.order_id == order.id).order_by(Ticket.id)).all())


def check_expiry(s: Session, order: Order) -> Order:
    if order.status == "pending" and order.created_at <= hold_cutoff():
        order.status = "expired"
        for t in order_tickets(s, order):
            t.status = "void"
            s.add(t)
        s.add(order)
        s.commit()
    return order


def confirm(s: Session, order: Order, conf: Confirmation, provider: str) -> bool:
    """Issue tickets once the provider says the full amount for this order was paid."""
    if order.status == "paid":
        return True
    if not conf.paid:
        return False
    if conf.reference and conf.reference != str(order.id):
        log.error("Payment for order %s came back tagged %r. Not issuing tickets.", order.id, conf.reference)
        return False
    if conf.amount_cents != order.total_cents or conf.currency.upper() != order.currency.upper():
        log.error("Order %s: paid %s %s but owed %s %s. Not issuing tickets.", order.id, conf.amount_cents,
                  conf.currency, order.total_cents, order.currency)
        return False
    if order.status in ("cancelled", "refunded"):
        log.error("Order %s was paid after it was %s. Refund it from the %s dashboard.", order.id, order.status, provider)
        return False
    order.provider_payment = conf.payment_ref or order.provider_payment
    fulfill(s, order)
    return True


def fulfill(s: Session, order: Order):
    """Mark paid and activate the tickets. A late payment on an expired order still counts."""
    if order.status == "paid":
        return
    order.status, order.paid_at = "paid", datetime.now()
    s.add(order)
    tix = order_tickets(s, order)
    for t in tix:
        t.status = "active"
        s.add(t)
    s.flush()
    buyer = s.get(User, order.user_id)
    if order.for_me and buyer and tix and ticket_of(s, order.event_id, buyer.id) is None:
        issue(s, tix[0], buyer)
    s.commit()
    ev = s.get(Event, order.event_id)
    if buyer and buyer.email:
        n = len(tix)
        mailer.send(buyer.email, f"Your tickets for {ev.name}",
                    f"Thanks! Order #{order.id} for {n} ticket{'s' if n != 1 else ''} to {ev.name} is paid.\n\n"
                    f"See your tickets, and send spares to friends, from your account page.")


def issue(s: Session, ticket: Ticket, user: User) -> Attendee:
    """Give a ticket to someone: they get a spot on the attendee list."""
    a = Attendee(event_id=ticket.event_id, name=user.name or user.display, handle=user.handle,
                 contact=user.email or "", gear=user.gear, watts=user.watts if user.watts is not None else config.DEFAULT_WATTS,
                 paid=True, user_id=user.id)
    s.add(a)
    s.flush()
    ticket.holder_id, ticket.attendee_id = user.id, a.id
    s.add(ticket)
    return a


def claim(s: Session, ticket: Ticket, user: User) -> Attendee:
    with _sale_lock:
        s.refresh(ticket)
        if ticket.status != "active":
            raise ValueError("This ticket isn't valid anymore.")
        if ticket.holder_id:
            raise ValueError("Someone already claimed this ticket.")
        if ticket_of(s, ticket.event_id, user.id):
            raise ValueError("You already have a ticket for this event. Pass this one to someone else.")
        a = issue(s, ticket, user)
        ticket.claim_token = Ticket().claim_token  # the old link stops working
        s.add(ticket)
        s.commit()
        return a


def release(s: Session, ticket: Ticket):
    """Take a ticket back from its holder so it can be given to someone else."""
    a = s.get(Attendee, ticket.attendee_id) if ticket.attendee_id else None
    if a and a.checked_in_at:
        raise ValueError("This ticket was already used at the door.")
    if a and teams.in_started_tournament(s, a.id):
        raise ValueError("This player is in a tournament that has started. Ask an organizer.")
    if a:
        remove_attendee(s, a)
    ticket.holder_id = ticket.attendee_id = None
    ticket.claim_token = Ticket().claim_token
    s.add(ticket)


def remove_attendee(s: Session, a: Attendee):
    teams.drop_from_signups(s, a.id)
    s.delete(a)


def cancel(s: Session, order: Order, status: str = "cancelled"):
    """Void every ticket in an order and take the holders off the attendee list."""
    for t in order_tickets(s, order):
        if t.attendee_id:
            a = s.get(Attendee, t.attendee_id)
            if a:
                remove_attendee(s, a)
        t.status, t.holder_id, t.attendee_id = "void", None, None
        s.add(t)
    order.status = status
    s.add(order)


def sync_attendees(s: Session, user: User):
    """Copy profile changes onto the user's spots at events that haven't happened yet."""
    for a in s.exec(select(Attendee).where(Attendee.user_id == user.id)).all():
        ev = s.get(Event, a.event_id)
        if ev and ev.is_past:
            continue
        a.name, a.handle, a.contact = user.name or user.display, user.handle, user.email or ""
        a.gear = user.gear
        if user.watts is not None:
            a.watts = user.watts
        s.add(a)


def holdings(s: Session, user: User) -> list[dict]:
    """Tickets this user holds, newest event first."""
    out = []
    for t in s.exec(select(Ticket).where(Ticket.holder_id == user.id, Ticket.status == "active")
                    .order_by(col(Ticket.id).desc())).all():
        out.append(dict(ticket=t, event=s.get(Event, t.event_id), type=s.get(TicketType, t.ticket_type_id),
                        attendee=s.get(Attendee, t.attendee_id) if t.attendee_id else None))
    return out


def orders_for(s: Session, user: User) -> list[dict]:
    out = []
    for o in s.exec(select(Order).where(Order.user_id == user.id, Order.status != "expired")
                    .order_by(col(Order.id).desc())).all():
        check_expiry(s, o)
        if o.status == "expired":
            continue
        tix = order_tickets(s, o)
        out.append(dict(order=o, event=s.get(Event, o.event_id), count=len(tix),
                        unclaimed=sum(1 for t in tix if t.status == "active" and not t.holder_id)))
    return out
