from sqlalchemy import event as sa_event
from sqlmodel import Session, SQLModel, create_engine, select

from .config import DB_URL
from .models import Event

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


@sa_event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def init_db():
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as s:
        yield s


def active_event(s: Session) -> Event:
    ev = s.exec(select(Event).where(Event.active == True)).first()  # noqa: E712
    if ev:
        return ev
    ev = s.exec(select(Event).order_by(Event.id.desc())).first()
    if not ev:
        ev = Event(name="LAN Party", active=True)
    ev.active = True
    s.add(ev)
    s.commit()
    s.refresh(ev)
    return ev
