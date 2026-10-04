from sqlalchemy import event as sa_event
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import DB_URL
from .models import Event

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})


@sa_event.listens_for(engine, "connect")
def _pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def _sql_default(column) -> str:
    """A literal for ALTER TABLE ... ADD COLUMN, taken from the model's default."""
    default = column.default.arg if column.default is not None and not callable(column.default.arg) else None
    if default is None:
        return "NULL"
    if isinstance(default, bool):
        return "1" if default else "0"
    if isinstance(default, (int, float)):
        return str(default)
    return "'" + str(default).replace("'", "''") + "'"


def _add_missing_columns():
    """create_all() never alters existing tables, so add columns that newer models introduced."""
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            added = set()
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = column.type.compile(dialect=engine.dialect)
                default = _sql_default(column)
                null = "" if column.nullable or default == "NULL" else " NOT NULL"
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}{null} DEFAULT {default}'))
                added.add(column.name)
            for index in table.indexes:
                if added & {c.name for c in index.columns}:
                    index.create(conn, checkfirst=True)


def init_db():
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


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
