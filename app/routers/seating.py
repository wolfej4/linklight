from fastapi import APIRouter, Depends, Form, Request
from sqlmodel import Session, col, select

from ..auth import require_admin
from ..db import active_event, get_session
from ..models import Attendee, Circuit, LanTable
from ..web import back, render

router = APIRouter(dependencies=[Depends(require_admin)])


def circuit_loads(circuits, tables, people) -> list[dict]:
    table_circuit = {t.id: t.circuit_id for t in tables}
    out = []
    for c in circuits:
        draw = sum(a.watts for a in people if a.table_id and table_circuit.get(a.table_id) == c.id)
        cap = c.usable_watts or 1
        out.append(dict(c=c, draw=draw, cap=cap, pct=round(draw * 100 / cap)))
    return out


def _state(s: Session, event_id: int):
    circuits = s.exec(select(Circuit).where(Circuit.event_id == event_id).order_by(Circuit.id)).all()
    tables = s.exec(select(LanTable).where(LanTable.event_id == event_id).order_by(LanTable.sort, LanTable.id)).all()
    people = s.exec(select(Attendee).where(Attendee.event_id == event_id)).all()
    seatmap = {(a.table_id, a.seat_no): a for a in people if a.table_id}
    return circuits, tables, people, seatmap


def _map(request: Request, s: Session, **extra):
    ev = active_event(s)
    circuits, tables, people, seatmap = _state(s, ev.id)
    return render(
        request, extra.pop("template", "_seatmap.html"), event=ev, section="seating", circuits=circuits,
        tables=tables, seatmap=seatmap, loads=circuit_loads(circuits, tables, people),
        circuit_names={c.id: c.name for c in circuits},
        unseated=sorted([a for a in people if not a.table_id], key=lambda a: a.name.lower()),
        **extra,
    )


@router.get("/seating")
def page(request: Request, s: Session = Depends(get_session)):
    return _map(request, s, template="seating.html")


@router.post("/seating/circuits")
def add_circuit(name: str = Form(...), amps: int = Form(15), volts: int = Form(120), s: Session = Depends(get_session)):
    ev = active_event(s)
    s.add(Circuit(event_id=ev.id, name=name.strip() or "Circuit", amps=max(1, amps), volts=max(1, volts)))
    s.commit()
    return back("/seating")


@router.post("/seating/circuits/{cid}/delete")
def delete_circuit(cid: int, s: Session = Depends(get_session)):
    for t in s.exec(select(LanTable).where(LanTable.circuit_id == cid)).all():
        t.circuit_id = None
        s.add(t)
    c = s.get(Circuit, cid)
    if c:
        s.delete(c)
    s.commit()
    return back("/seating")


@router.post("/seating/tables")
def add_tables(
    name: str = Form("Table"), seats: int = Form(4), count: int = Form(1), circuit_id: int = Form(0),
    s: Session = Depends(get_session),
):
    ev = active_event(s)
    existing = s.exec(select(LanTable).where(LanTable.event_id == ev.id)).all()
    start = len(existing) + 1
    count = max(1, min(count, 200))
    base = name.strip() or "Table"
    for i in range(count):
        label = base if count == 1 else f"{base} {start + i}"
        s.add(LanTable(event_id=ev.id, name=label, seats=max(1, min(seats, 48)),
                       circuit_id=circuit_id or None, sort=start + i))
    s.commit()
    return back("/seating")


@router.post("/seating/tables/{tid}")
def update_table(tid: int, name: str = Form(...), seats: int = Form(...), circuit_id: int = Form(0),
                 s: Session = Depends(get_session)):
    t = s.get(LanTable, tid)
    t.name, t.seats, t.circuit_id = name.strip() or t.name, max(1, min(seats, 48)), circuit_id or None
    for a in s.exec(select(Attendee).where(Attendee.table_id == tid)).all():
        if a.seat_no and a.seat_no > t.seats:
            a.table_id = a.seat_no = None
            s.add(a)
    s.add(t)
    s.commit()
    return back("/seating")


@router.post("/seating/tables/{tid}/delete")
def delete_table(tid: int, s: Session = Depends(get_session)):
    for a in s.exec(select(Attendee).where(Attendee.table_id == tid)).all():
        a.table_id = a.seat_no = None
        s.add(a)
    t = s.get(LanTable, tid)
    if t:
        s.delete(t)
    s.commit()
    return back("/seating")


@router.get("/seating/seat/{tid}/{n}")
def seat_panel(request: Request, tid: int, n: int, s: Session = Depends(get_session)):
    ev = active_event(s)
    t = s.get(LanTable, tid)
    occupant = s.exec(select(Attendee).where(Attendee.table_id == tid, Attendee.seat_no == n)).first()
    others = s.exec(select(Attendee).where(Attendee.event_id == ev.id).order_by(col(Attendee.name))).all()
    return render(request, "_seat_panel.html", t=t, n=n, occupant=occupant,
                  unseated=[a for a in others if not a.table_id], seated=[a for a in others if a.table_id and a is not occupant])


@router.post("/seating/seat/{tid}/{n}")
def assign_seat(request: Request, tid: int, n: int, attendee_id: int = Form(0), s: Session = Depends(get_session)):
    current = s.exec(select(Attendee).where(Attendee.table_id == tid, Attendee.seat_no == n)).first()
    if current:
        current.table_id = current.seat_no = None
        s.add(current)
    if attendee_id:
        a = s.get(Attendee, attendee_id)
        a.table_id, a.seat_no = tid, n
        s.add(a)
    s.commit()
    return _map(request, s)


@router.post("/seating/autoseat")
def autoseat(request: Request, only_here: str = Form(""), s: Session = Depends(get_session)):
    """Fill open seats in table order, skipping seats whose circuit can't take the extra load."""
    ev = active_event(s)
    circuits, tables, people, seatmap = _state(s, ev.id)
    loads = {row["c"].id: row for row in circuit_loads(circuits, tables, people)}
    queue = [a for a in people if not a.table_id and (a.checked_in_at or not only_here)]
    queue.sort(key=lambda a: (a.checked_in_at is None, a.checked_in_at or 0, a.name.lower()))
    for t in tables:
        for n in range(1, t.seats + 1):
            if not queue:
                break
            if (t.id, n) in seatmap:
                continue
            load = loads.get(t.circuit_id)
            pick = next((i for i, p in enumerate(queue) if not load or load["draw"] + p.watts <= load["cap"]), None)
            if pick is None:
                continue
            a = queue.pop(pick)
            a.table_id, a.seat_no = t.id, n
            seatmap[(t.id, n)] = a
            if load:
                load["draw"] += a.watts
            s.add(a)
    s.commit()
    msg = None
    if queue:
        msg = f"{len(queue)} people still need seats. Add tables or circuits with spare capacity."
    return _map(request, s, notice=msg)
