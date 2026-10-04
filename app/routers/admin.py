"""Organizer pages: overview, event details, tickets and orders, timetable, news and users."""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, col, select

from .. import config, payments, tickets
from .. import gameservers as gs
from ..auth import current_user, require_admin
from ..db import active_event, get_session
from ..models import (Attendee, Circuit, Entrant, Event, GameServer, Identity, LanTable, Match, Order, Post,
                      Team, TeamMember, Ticket, TicketType, TimetableItem, Tournament, User, parse_when)
from ..web import back, render
from . import seating

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@router.get("")
def dashboard(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    people = s.exec(select(Attendee).where(Attendee.event_id == ev.id)).all()
    tables = s.exec(select(LanTable).where(LanTable.event_id == ev.id)).all()
    circuits = s.exec(select(Circuit).where(Circuit.event_id == ev.id)).all()
    tourneys = s.exec(select(Tournament).where(Tournament.event_id == ev.id)).all()
    servers_ = s.exec(select(GameServer).where(GameServer.event_id == ev.id)).all()
    paid = s.exec(select(Order).where(Order.event_id == ev.id, Order.status == "paid")).all()
    seats = sum(t.seats for t in tables)
    seated = sum(1 for a in people if a.table_id)
    load = seating.circuit_loads(circuits, tables, people)
    return render(
        request, "dashboard.html", event=ev, section="home",
        stats=dict(
            total=len(people), checked=sum(1 for a in people if a.checked_in_at),
            paid=sum(1 for a in people if a.paid), seats=seats, open=seats - seated,
            live=sum(1 for t in tourneys if t.status == "live"),
            running=sum(1 for g in servers_ if gs.status(g.container_name)[0] == "running") if servers_ else 0,
            servers=len(servers_), revenue=sum(o.total_cents for o in paid), orders=len(paid),
        ),
        hot=[c for c in load if c["pct"] >= 90],
        events=s.exec(select(Event).order_by(col(Event.id).desc())).all(),
    )


@router.post("/event")
def update_event(
    request: Request, name: str = Form(...), starts_at: str = Form(""), ends_at: str = Form(""), venue: str = Form(""),
    address: str = Form(""), description: str = Form(""), announcement: str = Form(""), published: str = Form(""),
    s: Session = Depends(get_session),
):
    ev = active_event(s)
    start, end = parse_when(starts_at), parse_when(ends_at)
    if starts_at and not start:
        return back("/admin", "Pick a start date and time.")
    if start and end and end < start:
        return back("/admin", "The event can't end before it starts.")
    ev.name = name.strip() or ev.name
    ev.starts_at, ev.ends_at = starts_at, ends_at if end else ""
    ev.venue, ev.address, ev.description = venue.strip(), address.strip(), description.strip()
    ev.announcement, ev.published = announcement.strip(), bool(published)
    s.add(ev)
    s.commit()
    return back("/admin", notice="Event saved.")


@router.post("/events")
def create_event(name: str = Form(...), s: Session = Depends(get_session)):
    for e in s.exec(select(Event)).all():
        e.active = False
        s.add(e)
    s.add(Event(name=name.strip() or "LAN Party", active=True))
    s.commit()
    return back("/admin")


@router.post("/events/{event_id}/activate")
def activate_event(event_id: int, s: Session = Depends(get_session)):
    for e in s.exec(select(Event)).all():
        e.active = e.id == event_id
        s.add(e)
    s.commit()
    return back("/admin")


@router.post("/events/{event_id}/delete")
def delete_event(event_id: int, s: Session = Depends(get_session)):
    ev = s.get(Event, event_id)
    if not ev:
        return back("/admin")
    if ev.active:
        return back("/admin", "Switch to another event before deleting this one.")
    if s.exec(select(Order).where(Order.event_id == event_id, Order.status == "paid")).first():
        return back("/admin", "This event has paid orders. Refund or cancel them before deleting it.")
    for g in s.exec(select(GameServer).where(GameServer.event_id == event_id)).all():
        gs.remove(g.container_name)
        s.delete(g)
    for t in s.exec(select(Tournament).where(Tournament.event_id == event_id)).all():
        for team in s.exec(select(Team).where(Team.tournament_id == t.id)).all():
            for row in s.exec(select(TeamMember).where(TeamMember.team_id == team.id)).all():
                s.delete(row)
            s.delete(team)
        for row in s.exec(select(Match).where(Match.tournament_id == t.id)).all():
            s.delete(row)
        for row in s.exec(select(Entrant).where(Entrant.tournament_id == t.id)).all():
            s.delete(row)
        s.delete(t)
    for model in (Attendee, LanTable, Circuit, Ticket, Order, TicketType, TimetableItem):
        for row in s.exec(select(model).where(model.event_id == event_id)).all():
            s.delete(row)
    for p in s.exec(select(Post).where(Post.event_id == event_id)).all():
        p.event_id = None
        s.add(p)
    s.delete(ev)
    s.commit()
    return back("/admin")


# ---- tickets and orders -------------------------------------------------------

PRICE = re.compile(r"^(\d{0,6})(?:\.(\d{1,2}))?$")


def _cents(price: str) -> int:
    m = PRICE.match(price.strip().replace(",", "").lstrip("$€£"))
    if not m:
        raise ValueError("Enter a price like 25 or 25.00.")
    return int(m.group(1) or 0) * 100 + int(((m.group(2) or "") + "00")[:2])


@router.get("/tickets")
def tickets_page(request: Request, status: str = "", s: Session = Depends(get_session)):
    ev = active_event(s)
    types = s.exec(select(TicketType).where(TicketType.event_id == ev.id).order_by(TicketType.sort, TicketType.id)).all()
    stmt = select(Order).where(Order.event_id == ev.id, Order.status != "expired")
    if status:
        stmt = stmt.where(Order.status == status)
    orders = []
    for o in s.exec(stmt.order_by(col(Order.id).desc())).all():
        tickets.check_expiry(s, o)
        if o.status == "expired" and status != "expired":
            continue
        tix = tickets.order_tickets(s, o)
        orders.append(dict(o=o, buyer=s.get(User, o.user_id), count=len(tix),
                           assigned=sum(1 for t in tix if t.holder_id)))
    sold = {t.id: len(s.exec(select(Ticket).where(Ticket.ticket_type_id == t.id, Ticket.status == "active")).all())
            for t in types}
    return render(request, "admin_tickets.html", event=ev, section="tickets", types=types, sold=sold,
                  left={t.id: tickets.remaining(s, t) for t in types}, orders=orders, status=status,
                  providers=payments.enabled(), currency=config.CURRENCY)


@router.post("/tickets")
def add_type(name: str = Form(...), description: str = Form(""), price: str = Form("0"), quantity: int = Form(0),
             max_per_order: int = Form(4), s: Session = Depends(get_session)):
    ev = active_event(s)
    try:
        cents = _cents(price)
    except ValueError as e:
        return back("/admin/tickets", str(e))
    n = len(s.exec(select(TicketType).where(TicketType.event_id == ev.id)).all())
    s.add(TicketType(event_id=ev.id, name=name.strip() or "General admission", description=description.strip(),
                     price_cents=cents, quantity=max(0, quantity), max_per_order=max(1, min(max_per_order, 20)), sort=n))
    s.commit()
    return back("/admin/tickets")


@router.post("/tickets/{tid}")
def update_type(tid: int, name: str = Form(...), description: str = Form(""), price: str = Form("0"),
                quantity: int = Form(0), max_per_order: int = Form(4), on_sale: str = Form(""),
                s: Session = Depends(get_session)):
    tt = s.get(TicketType, tid)
    if not tt:
        return back("/admin/tickets")
    try:
        cents = _cents(price)
    except ValueError as e:
        return back("/admin/tickets", str(e))
    tt.name, tt.description = name.strip() or tt.name, description.strip()
    tt.price_cents, tt.quantity = cents, max(0, quantity)
    tt.max_per_order, tt.on_sale = max(1, min(max_per_order, 20)), bool(on_sale)
    s.add(tt)
    s.commit()
    return back("/admin/tickets", notice=f"{tt.name} saved.")


@router.post("/tickets/{tid}/delete")
def delete_type(tid: int, s: Session = Depends(get_session)):
    tt = s.get(TicketType, tid)
    if tt and s.exec(select(Ticket).where(Ticket.ticket_type_id == tid, Ticket.status != "void")).first():
        return back("/admin/tickets", "People have bought this ticket. Take it off sale instead.")
    if tt:
        for t in s.exec(select(Ticket).where(Ticket.ticket_type_id == tid)).all():
            s.delete(t)
        s.delete(tt)
        s.commit()
    return back("/admin/tickets")


@router.post("/orders/{oid}/refund")
def refund_order(oid: int, s: Session = Depends(get_session)):
    """Refund through the payment provider, then void the tickets."""
    order = s.get(Order, oid)
    if not order or order.status != "paid":
        return back("/admin/tickets", "Only paid orders can be refunded.")
    try:
        if order.total_cents and order.provider == "stripe":
            payments.stripe_refund(order.provider_payment, order.id)
        elif order.total_cents and order.provider == "paypal":
            payments.paypal_refund(order.provider_payment, order.id)
    except payments.PaymentError as e:
        return back("/admin/tickets", f"The refund didn't go through: {e}")
    tickets.cancel(s, order, "refunded" if order.total_cents else "cancelled")
    s.commit()
    return back("/admin/tickets", notice=f"Order #{oid} refunded and its tickets cancelled.")


@router.post("/orders/{oid}/cancel")
def cancel_order(oid: int, s: Session = Depends(get_session)):
    """Void the tickets without touching the money, e.g. after refunding by hand in the provider's dashboard."""
    order = s.get(Order, oid)
    if not order or order.status not in ("paid", "pending"):
        return back("/admin/tickets")
    if order.status == "pending" and order.provider == "stripe" and order.provider_ref:
        payments.stripe_expire(order.provider_ref)
    tickets.cancel(s, order, "cancelled")
    s.commit()
    return back("/admin/tickets", notice=f"Order #{oid} cancelled.")


# ---- timetable ----------------------------------------------------------------

@router.get("/timetable")
def timetable(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    items = s.exec(select(TimetableItem).where(TimetableItem.event_id == ev.id).order_by(TimetableItem.starts_at)).all()
    return render(request, "admin_timetable.html", event=ev, section="timetable", items=items)


def _when(value: str) -> datetime | None:
    return parse_when(value.strip())


@router.post("/timetable")
def add_slot(title: str = Form(...), starts_at: str = Form(...), ends_at: str = Form(""), detail: str = Form(""),
             s: Session = Depends(get_session)):
    ev = active_event(s)
    start, end = _when(starts_at), _when(ends_at)
    if not start:
        return back("/admin/timetable", "Pick a start time.")
    if end and end < start:
        return back("/admin/timetable", "It can't end before it starts.")
    s.add(TimetableItem(event_id=ev.id, title=title.strip() or "Untitled", detail=detail.strip(), starts_at=start, ends_at=end))
    s.commit()
    return back("/admin/timetable")


@router.post("/timetable/{iid}/delete")
def delete_slot(iid: int, s: Session = Depends(get_session)):
    item = s.get(TimetableItem, iid)
    if item:
        s.delete(item)
        s.commit()
    return back("/admin/timetable")


# ---- news ---------------------------------------------------------------------

@router.get("/news")
def news(request: Request, edit: int = 0, s: Session = Depends(get_session)):
    ev = active_event(s)
    posts = s.exec(select(Post).order_by(col(Post.created_at).desc())).all()
    return render(request, "admin_news.html", event=ev, section="news", posts=posts, editing=s.get(Post, edit) if edit else None,
                  events=s.exec(select(Event).order_by(col(Event.id).desc())).all())


@router.post("/news")
def save_post(request: Request, title: str = Form(...), body: str = Form(""), event_id: int = Form(0),
              published: str = Form(""), pid: int = Form(0), s: Session = Depends(get_session)):
    p = s.get(Post, pid) if pid else None
    if not p:
        me = current_user(request)
        p = Post(title="", author_id=me.id if me else None)
    p.title, p.body = title.strip() or "Untitled", body.strip()
    p.event_id = event_id or None
    if published and not p.published:
        p.published_at = datetime.now()
    p.published = bool(published)
    s.add(p)
    s.commit()
    return back("/admin/news", notice="Post published." if p.published else "Draft saved.")


@router.post("/news/{pid}/delete")
def delete_post(pid: int, s: Session = Depends(get_session)):
    p = s.get(Post, pid)
    if p:
        s.delete(p)
        s.commit()
    return back("/admin/news")


# ---- users --------------------------------------------------------------------

@router.get("/users")
def users(request: Request, q: str = "", s: Session = Depends(get_session)):
    ev = active_event(s)
    stmt = select(User)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(col(User.name).ilike(like) | col(User.handle).ilike(like) | col(User.email).ilike(like))
    rows = s.exec(stmt.order_by(col(User.is_admin).desc(), col(User.id).desc()).limit(200)).all()
    idents: dict[int, list[str]] = {}
    for i in s.exec(select(Identity).where(col(Identity.user_id).in_([u.id for u in rows]))).all():
        idents.setdefault(i.user_id, []).append(i.provider)
    return render(request, "admin_users.html", event=ev, section="users", users=rows, idents=idents, q=q,
                  me=current_user(request))


@router.post("/users/{uid}/admin")
def toggle_admin(request: Request, uid: int, s: Session = Depends(get_session)):
    u = s.get(User, uid)
    me = current_user(request)
    if not u:
        return back("/admin/users")
    if me and me.id == u.id and u.is_admin:
        return back("/admin/users", "You can't remove your own organizer access. Ask another organizer.")
    u.is_admin = not u.is_admin
    s.add(u)
    s.commit()
    return back("/admin/users", notice=f"{u.display} is {'now' if u.is_admin else 'no longer'} an organizer.")

