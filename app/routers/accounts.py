"""Sign-in (Steam, Discord, Google, Apple, email link or the organizer password) and the account page."""
import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlmodel import Session, col, func, select
from starlette.concurrency import run_in_threadpool

from .. import config, mailer, oauth, tickets
from ..auth import current_user, require_user, safe_next, sign_in
from ..config import ADMIN_PASSWORD, secret_key
from ..db import get_session
from ..models import Attendee, EmailCode, Identity, User
from ..web import back, base_url, render

router = APIRouter()
log = logging.getLogger("lanparty.auth")

STATE_COOKIE = "lp_oauth"
STATE_MAX_AGE = 600
EMAIL_CODE_MINUTES = 15
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key(), salt="oauth-state")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _redirect_uri(request: Request, provider: str) -> str:
    return f"{base_url(request)}/auth/{provider}/callback"


# ---- sign-in page -------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if current_user(request):
        return RedirectResponse(safe_next(next, "/account"), status_code=303)
    return render(request, "login.html", next=safe_next(next), providers=oauth.enabled(), names=oauth.PROVIDERS,
                  email_login=mailer.enabled(), event=None)


@router.post("/login")
def password_login(request: Request, password: str = Form(...), next: str = Form("/admin")):
    """Organizer fallback: the shared ADMIN_PASSWORD still works so nobody gets locked out."""
    if hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
        request.session["admin"] = True
        return RedirectResponse(safe_next(next, "/admin"), status_code=303)
    return render(request, "login.html", next=safe_next(next), providers=oauth.enabled(), names=oauth.PROVIDERS,
                  email_login=mailer.enabled(), event=None, error="That password didn't match. Try again.")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


# ---- third-party providers ----------------------------------------------------

@router.get("/auth/{provider}/start")
def provider_start(request: Request, provider: str, next: str = "/"):
    if provider not in oauth.enabled():
        return back("/login", "That sign-in option isn't set up on this site.")
    user = current_user(request)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    payload = {"p": provider, "s": state, "n": nonce, "next": safe_next(next, "/account" if user else "/"),
               "link": user.id if user else None}
    url = oauth.start_url(provider, _redirect_uri(request, provider), base_url(request), state, nonce)
    resp = RedirectResponse(url, status_code=303)
    secure = base_url(request).startswith("https://")
    # Apple posts the callback cross-site, so the state cookie must be SameSite=None (which needs HTTPS).
    resp.set_cookie(STATE_COOKIE, _signer().dumps(payload), max_age=STATE_MAX_AGE, httponly=True,
                    secure=secure, samesite="none" if secure else "lax", path="/auth/")
    return resp


@router.get("/auth/{provider}/callback")
def provider_callback_get(request: Request, provider: str, s: Session = Depends(get_session)):
    return _finish(request, provider, dict(request.query_params), s)


@router.post("/auth/{provider}/callback")
async def provider_callback_post(request: Request, provider: str, s: Session = Depends(get_session)):
    form = await request.form()
    return await run_in_threadpool(_finish, request, provider, {k: str(v) for k, v in form.items()}, s)


def _finish(request: Request, provider: str, params: dict, s: Session):
    try:
        st = _signer().loads(request.cookies.get(STATE_COOKIE, ""), max_age=STATE_MAX_AGE)
    except BadSignature:
        st = None
    if not st or st.get("p") != provider or not hmac.compare_digest(str(params.get("state", "")), st["s"]):
        return _fail("That sign-in expired or was started in another browser. Try again.")
    if params.get("error"):
        return _fail("Sign-in was cancelled.")
    redirect_uri = _redirect_uri(request, provider)
    try:
        if provider == "steam":
            profile = oauth.steam_profile(params, redirect_uri)
        elif provider == "discord":
            profile = oauth.discord_profile(params.get("code", ""), redirect_uri)
        elif provider == "google":
            profile = oauth.google_profile(params.get("code", ""), redirect_uri, st["n"])
        elif provider == "apple":
            try:
                user_json = json.loads(params.get("user") or "null")
            except ValueError:
                user_json = None
            profile = oauth.apple_profile(params.get("code", ""), redirect_uri, st["n"], user_json)
        else:
            return _fail("Unknown sign-in provider.")
    except oauth.OAuthError as e:
        return _fail(str(e))
    except Exception:  # network trouble, unexpected JSON
        log.exception("%s sign-in failed", provider)
        return _fail(f"Couldn't reach {oauth.PROVIDERS[provider]}. Try again in a minute.")

    linking = s.get(User, st["link"]) if st.get("link") else None
    try:
        user = resolve_user(s, profile, linking)
    except ValueError as e:
        return _fail(str(e), "/account" if linking else "/login")
    sign_in(request, user)
    resp = RedirectResponse(safe_next(st.get("next")), status_code=303)
    resp.delete_cookie(STATE_COOKIE, path="/auth/")
    return resp


def _fail(message: str, where: str = "/login"):
    resp = back(where, message)
    resp.delete_cookie(STATE_COOKIE, path="/auth/")
    return resp


def _email_owner(s: Session, email: str) -> User | None:
    return s.exec(select(User).where(User.email == email.lower())).first()


def resolve_user(s: Session, p: oauth.Profile, linking: User | None) -> User:
    """Find or create the account for a provider identity, or attach it to the signed-in account."""
    ident = s.exec(select(Identity).where(Identity.provider == p.provider, Identity.subject == p.subject)).first()
    email = p.email.strip().lower() if p.email and p.email_verified else ""
    if ident:
        if linking and ident.user_id != linking.id:
            raise ValueError(f"That {oauth.PROVIDERS[p.provider]} account is already linked to a different account here.")
        user = s.get(User, ident.user_id)
    else:
        user = linking or (_email_owner(s, email) if email else None)
        if not user:
            user = User()
            s.add(user)
            s.flush()
        s.add(Identity(user_id=user.id, provider=p.provider, subject=p.subject, label=p.label))
    user.name = user.name or p.name
    user.handle = user.handle or p.handle
    user.avatar_url = user.avatar_url or p.avatar_url
    if email and not user.email and not _email_owner(s, email):
        user.email = email
    _finish_login(s, user)
    return user


def _finish_login(s: Session, user: User):
    if user.email and user.email in config.ADMIN_EMAILS:
        user.is_admin = True
    user.last_login_at = datetime.now()
    s.add(user)
    s.commit()
    s.refresh(user)


# ---- email links --------------------------------------------------------------

def _send_code(request: Request, s: Session, email: str, purpose: str, next_: str, user_id: int | None = None) -> str | None:
    """Email a one-time link. Returns an error message when rate limited."""
    since = datetime.now() - timedelta(hours=1)
    recent = s.exec(select(EmailCode).where(EmailCode.email == email, EmailCode.created_at > since)
                    .order_by(col(EmailCode.created_at).desc())).all()
    if len(recent) >= 5 or (recent and recent[0].created_at > datetime.now() - timedelta(seconds=60)):
        return "We just sent a link to that address. Give it a minute, and check your spam folder."
    token = secrets.token_urlsafe(32)
    s.add(EmailCode(token_hash=_hash(token), email=email, purpose=purpose, user_id=user_id, next=next_))
    s.commit()
    link = f"{base_url(request)}/login/email/{token}"
    if purpose == "login":
        body = (f"Use this link to sign in to {config.SITE_NAME}:\n\n{link}\n\n"
                f"It works once and expires in {EMAIL_CODE_MINUTES} minutes. If you didn't ask for it, ignore this email.")
        mailer.send(email, f"Sign in to {config.SITE_NAME}", body)
    else:
        body = (f"Confirm this address for your {config.SITE_NAME} account:\n\n{link}\n\n"
                f"It expires in {EMAIL_CODE_MINUTES} minutes. If you didn't ask for it, ignore this email.")
        mailer.send(email, f"Confirm your email for {config.SITE_NAME}", body)
    return None


@router.post("/login/email")
def email_login(request: Request, email: str = Form(...), next: str = Form("/"), s: Session = Depends(get_session)):
    if not mailer.enabled():
        return back("/login", "Email sign-in isn't set up on this site.")
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        return back("/login", "That doesn't look like an email address.")
    err = _send_code(request, s, email, "login", safe_next(next))
    if err:
        return back("/login", err)
    # Same response whether or not an account exists, so this can't be used to probe for members.
    return render(request, "email_sent.html", email=email, event=None)


def _live_code(s: Session, token: str) -> EmailCode | None:
    code = s.exec(select(EmailCode).where(EmailCode.token_hash == _hash(token))).first()
    if not code or code.used_at or code.created_at < datetime.now() - timedelta(minutes=EMAIL_CODE_MINUTES):
        return None
    return code


@router.get("/login/email/{token}", response_class=HTMLResponse)
def email_link(request: Request, token: str, s: Session = Depends(get_session)):
    # Confirm with a button, so mail scanners that open links don't use the code up.
    code = _live_code(s, token)
    return render(request, "email_confirm.html", code=code, token=token, event=None)


@router.post("/login/email/{token}")
def email_link_use(request: Request, token: str, s: Session = Depends(get_session)):
    code = _live_code(s, token)
    if not code:
        return back("/login", "That link has expired or was already used. Ask for a new one.")
    code.used_at = datetime.now()
    s.add(code)
    if code.purpose == "verify":
        user = s.get(User, code.user_id) if code.user_id else None
        owner = _email_owner(s, code.email)
        if not user:
            s.commit()
            return back("/login", "That account no longer exists.")
        if owner and owner.id != user.id:
            s.commit()
            return back("/account", "That email already belongs to another account. Sign in with it and link from there.")
        user.email = code.email
        _finish_login(s, user)
        tickets.sync_attendees(s, user)
        s.commit()
        if current_user(request) and current_user(request).id == user.id:
            request.state.user = user
        return back("/account", notice="Email confirmed.")
    user = _email_owner(s, code.email)
    if not user:
        user = User(email=code.email)
        s.add(user)
    _finish_login(s, user)
    sign_in(request, user)
    return RedirectResponse(safe_next(code.next), status_code=303)


# ---- account page -------------------------------------------------------------

@router.get("/account", response_class=HTMLResponse)
def account(request: Request, s: Session = Depends(get_session)):
    user = require_user(request)
    idents = s.exec(select(Identity).where(Identity.user_id == user.id).order_by(Identity.id)).all()
    linked = {i.provider for i in idents}
    return render(
        request, "account.html", event=None, me=user, identities=idents, names=oauth.PROVIDERS,
        linkable=[p for p in oauth.enabled() if p not in linked], email_login=mailer.enabled(),
        holdings=tickets.holdings(s, user), orders=tickets.orders_for(s, user),
        default_watts=config.DEFAULT_WATTS,
    )


@router.post("/account")
def account_update(request: Request, name: str = Form(""), handle: str = Form(""), gear: str = Form(""),
                   watts: str = Form(""), s: Session = Depends(get_session)):
    user = s.get(User, require_user(request).id)
    user.name, user.handle, user.gear = name.strip()[:80], handle.strip()[:40], gear.strip()[:200]
    try:
        user.watts = max(0, min(int(watts), 5000)) if watts.strip() else None
    except ValueError:
        return back("/account", "Watts should be a number.")
    s.add(user)
    tickets.sync_attendees(s, user)
    s.commit()
    return back("/account", notice="Profile saved.")


@router.post("/account/email")
def account_email(request: Request, email: str = Form(...), s: Session = Depends(get_session)):
    user = require_user(request)
    email = email.strip().lower()
    if not mailer.enabled():
        return back("/account", "Email isn't set up on this site.")
    if not EMAIL_RE.match(email) or len(email) > 254:
        return back("/account", "That doesn't look like an email address.")
    err = _send_code(request, s, email, "verify", "/account", user.id)
    return back("/account", err) if err else back("/account", notice=f"We sent a confirmation link to {email}.")


def _login_methods(s: Session, user: User) -> int:
    n = s.exec(select(func.count()).select_from(Identity).where(Identity.user_id == user.id)).one()
    return n + (1 if user.email and mailer.enabled() else 0)


@router.post("/account/unlink/{iid}")
def account_unlink(request: Request, iid: int, s: Session = Depends(get_session)):
    user = require_user(request)
    ident = s.get(Identity, iid)
    if not ident or ident.user_id != user.id:
        return back("/account")
    if _login_methods(s, user) <= 1:
        return back("/account", "Link another sign-in method first, or you won't be able to get back in.")
    s.delete(ident)
    s.commit()
    return back("/account", notice=f"{oauth.PROVIDERS.get(ident.provider, ident.provider)} unlinked.")


@router.post("/account/email/remove")
def account_email_remove(request: Request, s: Session = Depends(get_session)):
    user = s.get(User, require_user(request).id)
    if user.email and mailer.enabled() and _login_methods(s, user) <= 1:
        return back("/account", "Link another sign-in method first, or you won't be able to get back in.")
    user.email = None
    s.add(user)
    for a in s.exec(select(Attendee).where(Attendee.user_id == user.id)).all():
        a.contact = ""
        s.add(a)
    s.commit()
    return back("/account", notice="Email removed.")
