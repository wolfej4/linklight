import secrets
from datetime import datetime
from typing import Optional

from pydantic import NaiveDatetime
from sqlmodel import Field, SQLModel, UniqueConstraint


def new_token() -> str:
    return secrets.token_urlsafe(9)


def long_token() -> str:
    return secrets.token_urlsafe(24)


def parse_when(value: str) -> Optional[datetime]:
    """Event times are stored as datetime-local strings. Older free-text values parse to None."""
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    starts_at: str = ""  # "2026-10-17T14:00", or legacy free text
    ends_at: str = ""
    venue: str = ""
    address: str = ""
    description: str = ""
    announcement: str = ""
    published: bool = False
    active: bool = False
    created_at: NaiveDatetime = Field(default_factory=datetime.now)

    @property
    def start(self) -> Optional[datetime]:
        return parse_when(self.starts_at)

    @property
    def end(self) -> Optional[datetime]:
        return parse_when(self.ends_at)

    @property
    def is_past(self) -> bool:
        last = self.end or self.start
        return bool(last and last < datetime.now())


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
    user_id: Optional[int] = Field(default=None, index=True)

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
    team_size: int = 1  # 1 = solo; otherwise bracket slots are teams
    signups_open: bool = False  # ticket holders can enter themselves from the public page
    created_at: NaiveDatetime = Field(default_factory=datetime.now)

    @property
    def teams(self) -> bool:
        return self.team_size > 1


class Entrant(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(index=True)
    attendee_id: int  # the player, or a team's captain
    team_id: Optional[int] = Field(default=None, index=True)
    seed: int = 0


class Team(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(index=True)
    name: str
    captain_id: int  # attendee id
    join_code: str = Field(default_factory=lambda: secrets.token_hex(3).upper(), index=True)

    @property
    def display(self) -> str:
        return self.name


class TeamMember(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(index=True)
    attendee_id: int = Field(index=True)


class Match(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    tournament_id: int = Field(index=True)
    round: int
    slot: int
    p1: Optional[int] = None  # attendee ids, or team ids in team tournaments
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


# ---- accounts ---------------------------------------------------------------

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = ""
    handle: str = ""
    email: Optional[str] = Field(default=None, index=True, unique=True)  # verified addresses only
    avatar_url: str = ""
    gear: str = ""
    watts: Optional[int] = None
    is_admin: bool = False
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
    last_login_at: Optional[NaiveDatetime] = None

    @property
    def display(self) -> str:
        return self.handle or self.name or (self.email or "").split("@")[0] or f"Player {self.id}"


class Identity(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("provider", "subject"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    provider: str  # steam | discord | google | apple
    subject: str
    label: str = ""  # what to show on the account page, e.g. the Discord username
    created_at: NaiveDatetime = Field(default_factory=datetime.now)


class EmailCode(SQLModel, table=True):
    """One-time link for signing in by email, or for confirming an address added to an account."""
    id: Optional[int] = Field(default=None, primary_key=True)
    token_hash: str = Field(index=True, unique=True)
    email: str = Field(index=True)
    purpose: str = "login"  # login | verify
    user_id: Optional[int] = None
    next: str = "/"
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
    used_at: Optional[NaiveDatetime] = None


# ---- tickets ----------------------------------------------------------------

class TicketType(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    name: str
    description: str = ""
    price_cents: int = 0
    quantity: int = 0  # 0 = unlimited
    max_per_order: int = 4
    on_sale: bool = True
    sort: int = 0


class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    user_id: int = Field(index=True)
    status: str = "pending"  # pending | paid | expired | cancelled | refunded
    total_cents: int = 0
    currency: str = "USD"
    provider: str = ""  # stripe | paypal | free
    provider_ref: str = ""  # Stripe Checkout session id, or PayPal order id
    provider_payment: str = ""  # Stripe payment intent, or PayPal capture id (used for refunds)
    for_me: bool = True  # give the buyer one of the tickets when it's paid
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
    paid_at: Optional[NaiveDatetime] = None


class Ticket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    order_id: int = Field(index=True)
    ticket_type_id: int = Field(index=True)
    price_cents: int = 0
    status: str = "pending"  # pending | active | void
    holder_id: Optional[int] = Field(default=None, index=True)  # user id; None until claimed
    attendee_id: Optional[int] = None
    claim_token: str = Field(default_factory=long_token, index=True, unique=True)


# ---- event content ------------------------------------------------------------

class TimetableItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(index=True)
    starts_at: NaiveDatetime
    ends_at: Optional[NaiveDatetime] = None
    title: str
    detail: str = ""


class Post(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    body: str = ""
    event_id: Optional[int] = Field(default=None, index=True)
    published: bool = False
    author_id: Optional[int] = None
    created_at: NaiveDatetime = Field(default_factory=datetime.now)
    published_at: Optional[NaiveDatetime] = None
