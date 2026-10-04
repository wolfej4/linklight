import secrets
from datetime import datetime
from typing import Optional

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel


def new_token() -> str:
    return secrets.token_urlsafe(9)


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    starts_at: str = ""
    venue: str = ""
    announcement: str = ""
    active: bool = False
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class Circuit(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    amps: int = 15
    volts: int = 120

    @property
    def usable_watts(self) -> int:
        # 80% continuous-load rule for breakers
        return int(self.amps * self.volts * 0.8)


class LanTable(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    seats: int = 4
    circuit_id: Optional[int] = None
    sort: int = 0


class Attendee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    handle: str = ""
    contact: str = ""
    gear: str = ""
    watts: int = 450
    paid: bool = False
    notes: str = ""
    checked_in_at: Optional[NaiveDatetime] = None
    table_id: Optional[int] = Field(default=None, index=True)
    seat_no: Optional[int] = None
    token: str = Field(default_factory=new_token, index=True, unique=True)

    @property
    def display(self) -> str:
        return self.handle or self.name


class Tournament(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    game: str = ""
    fmt: str = "single"  # single | roundrobin
    status: str = "setup"  # setup | live | done
    best_of: int = 1
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class Entrant(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(index=True)
    attendee_id: int
    seed: int = 0


class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(index=True)
    round: int
    slot: int
    p1: Optional[int] = None  # attendee ids
    p2: Optional[int] = None
    s1: Optional[int] = None
    s2: Optional[int] = None
    winner: Optional[int] = None
    done: bool = False
    is_bye: bool = False
    next_match_id: Optional[int] = None
    next_side: Optional[int] = None


class GameServer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    template: str
    host_port: int
    container_name: str = Field(unique=True)
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
