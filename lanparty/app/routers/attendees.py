import csv
import io
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlmodel import Session, col, or_, select

from ..auth import require_admin
from ..config import DEFAULT_WATTS
from ..db import active_event, get_session
from ..models import Attendee, Entrant, LanTable, Tournament
from ..qr import qr_svg
from ..web import back, base_url, render

router = APIRouter(dependencies=[Depends(require_admin)])

FILTERS = {
    "all": "Everyone",
    "waiting": "Not checked in",
    "here": "Checked in",
    "unpaid": "Unpaid",
    "unseated": "No seat yet",
}


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in {"1", "y", "yes", "true", "paid", "x"}


def table_names(s: Session, event_id: int) -> dict[int, str]:
    return {t.id: t.name for t in s.exec(select(LanTable).where(LanTable.event_id == event_id)).all()}


def query_people(s: Session, event_id: int, q: str = "", flt: str = "all"):
    stmt = select(Attendee).where(Attendee.event_id == event_id)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(col(Attendee.name).ilike(like), col(Attendee.handle).ilike(like)))
    if flt == "waiting":
        stmt = stmt.where(Attendee.checked_in_at == None)  # noqa: E711
    elif flt == "here":
        stmt = stmt.where(Attendee.checked_in_at != None)  # noqa: E711
    elif flt == "unpaid":
        stmt = stmt.where(Attendee.paid == False)  # noqa: E712
    elif flt == "unseated":
        stmt = stmt.where(Attendee.table_id == None)  # noqa: E711
    return s.exec(stmt.order_by(col(Attendee.name))).all()


def _row(request, s, a):
    ev = active_event(s)
    return render(request, "_attendee_row.html", a=a, tables=table_names(s, ev.id))


@router.get("/attendees")
def page(request: Request, filter: str = "all", s: Session = Depends(get_session)):
    ev = active_event(s)
    everyone = s.exec(select(Attendee).where(Attendee.event_id == ev.id)).all()
    counts = dict(total=len(everyone), checked=sum(1 for a in everyone if a.checked_in_at), paid=sum(1 for a in everyone if a.paid))
    return render(
        request, "attendees.html", event=ev, section="attendees", people=query_people(s, ev.id, "", filter),
        tables=table_names(s, ev.id), counts=counts, filters=FILTERS, default_watts=DEFAULT_WATTS, flt=filter,
    )


@router.get("/attendees/rows")
def rows(request: Request, q: str = "", filter: str = "all", s: Session = Depends(get_session)):
    ev = active_event(s)
    return render(request, "_attendee_rows.html", people=query_people(s, ev.id, q, filter), tables=table_names(s, ev.id))


@router.post("/attendees")
def create(
    name: str = Form(...), handle: str = Form(""), contact: str = Form(""), gear: str = Form(""),
    watts: int = Form(DEFAULT_WATTS), paid: str = Form(""), s: Session = Depends(get_session),
):
    ev = active_event(s)
    if not name.strip():
        return back("/attendees", "Name is required.")
    s.add(Attendee(event_id=ev.id, name=name.strip(), handle=handle.strip(), contact=contact.strip(),
                   gear=gear.strip(), watts=max(0, watts), paid=bool(paid)))
    s.commit()
    return back("/attendees")


@router.post("/attendees/import")
async def import_csv(file: UploadFile = File(...), s: Session = Depends(get_session)):
    ev = active_event(s)
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in [f.strip().lower() for f in reader.fieldnames]:
        return back("/attendees", "The CSV needs a header row with at least a 'name' column.")
    added = 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not row.get("name"):
            continue
        try:
            watts = int(row.get("watts") or DEFAULT_WATTS)
        except ValueError:
            watts = DEFAULT_WATTS
        s.add(Attendee(event_id=ev.id, name=row["name"], handle=row.get("handle", ""), contact=row.get("contact", ""),
                       gear=row.get("gear", ""), watts=watts, paid=_truthy(row.get("paid", ""))))
        added += 1
    s.commit()
    return back("/attendees")


@router.get("/attendees/export.csv")
def export_csv(s: Session = Depends(get_session)):
    ev = active_event(s)
    names = table_names(s, ev.id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "handle", "contact", "gear", "watts", "paid", "checked_in", "table", "seat", "notes"])
    for a in query_people(s, ev.id):
        w.writerow([a.name, a.handle, a.contact, a.gear, a.watts, "yes" if a.paid else "no",
                    a.checked_in_at.isoformat(timespec="minutes") if a.checked_in_at else "",
                    names.get(a.table_id, ""), a.seat_no or "", a.notes])
    buf.seek(0)
    fname = f"{ev.name.replace(' ', '_')}_attendees.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/attendees/badges")
def badges(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    base = base_url(request)
    people = query_people(s, ev.id)
    return render(request, "badges.html", event=ev, tables=table_names(s, ev.id),
                  cards=[(a, qr_svg(f"{base}/p/{a.token}")) for a in people])


@router.post("/attendees/{aid}/paid")
def toggle_paid(request: Request, aid: int, s: Session = Depends(get_session)):
    a = s.get(Attendee, aid)
    a.paid = not a.paid
    s.add(a)
    s.commit()
    return _row(request, s, a)


@router.post("/attendees/{aid}/checkin")
def toggle_checkin(request: Request, aid: int, s: Session = Depends(get_session)):
    a = s.get(Attendee, aid)
    a.checked_in_at = None if a.checked_in_at else datetime.now()
    s.add(a)
    s.commit()
    return _row(request, s, a)


@router.get("/attendees/{aid}")
def edit_page(request: Request, aid: int, s: Session = Depends(get_session)):
    ev = active_event(s)
    a = s.get(Attendee, aid)
    if not a:
        return back("/attendees", "That attendee no longer exists.")
    return render(request, "attendee_edit.html", event=ev, section="attendees", a=a)


@router.post("/attendees/{aid}")
def update(
    aid: int, name: str = Form(...), handle: str = Form(""), contact: str = Form(""), gear: str = Form(""),
    watts: int = Form(DEFAULT_WATTS), paid: str = Form(""), notes: str = Form(""), s: Session = Depends(get_session),
):
    a = s.get(Attendee, aid)
    a.name, a.handle, a.contact, a.gear = name.strip() or a.name, handle.strip(), contact.strip(), gear.strip()
    a.watts, a.paid, a.notes = max(0, watts), bool(paid), notes.strip()
    s.add(a)
    s.commit()
    return back("/attendees")


@router.post("/attendees/{aid}/delete")
def delete(aid: int, s: Session = Depends(get_session)):
    a = s.get(Attendee, aid)
    if a:
        setup_ids = [t.id for t in s.exec(select(Tournament).where(Tournament.status == "setup")).all()]
        for e in s.exec(select(Entrant).where(Entrant.attendee_id == aid)).all():
            if e.tournament_id in setup_ids:
                s.delete(e)
        s.delete(a)
        s.commit()
    return back("/attendees")


# ---- door check-in station -------------------------------------------------

@router.get("/checkin")
def checkin_page(request: Request, s: Session = Depends(get_session)):
    ev = active_event(s)
    return render(request, "checkin.html", event=ev, section="checkin")


@router.get("/checkin/search")
def checkin_search(request: Request, q: str = "", s: Session = Depends(get_session)):
    ev = active_event(s)
    people = query_people(s, ev.id, q)[:12] if q.strip() else []
    return render(request, "_checkin_results.html", people=people, tables=table_names(s, ev.id), q=q)


@router.post("/checkin/{aid}")
def checkin_one(request: Request, aid: int, s: Session = Depends(get_session)):
    ev = active_event(s)
    a = s.get(Attendee, aid)
    if not a.checked_in_at:
        a.checked_in_at = datetime.now()
        s.add(a)
        s.commit()
    return render(request, "_checkin_card.html", a=a, tables=table_names(s, ev.id))
