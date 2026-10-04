from fastapi import Request
from sqlmodel import Session

from .models import User


class LoginRequired(Exception):
    pass


class NotOrganizer(Exception):
    pass


def current_user(request: Request) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user
    user = None
    uid = request.session.get("uid")
    if uid:
        from .db import engine
        with Session(engine, expire_on_commit=False) as s:
            user = s.get(User, uid)
        if not user:
            request.session.pop("uid", None)
    request.state.user = user
    return user


def is_admin(request: Request) -> bool:
    if request.session.get("admin") is True:  # signed in with ADMIN_PASSWORD
        return True
    user = current_user(request)
    return bool(user and user.is_admin)


def require_admin(request: Request):
    if is_admin(request):
        return
    if current_user(request):
        raise NotOrganizer()
    raise LoginRequired()


def require_user(request: Request) -> User:
    user = current_user(request)
    if not user:
        raise LoginRequired()
    return user


def sign_in(request: Request, user: User):
    """Start a fresh session for this account. Clearing first avoids carrying state across logins."""
    request.session.clear()
    request.session["uid"] = user.id
    request.state.user = user


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow same-site paths, so ?next= can't bounce people to another site."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return default
    return target
