"""Third-party sign-in: Steam (OpenID 2.0), Discord, Google and Apple (OAuth 2 / OpenID Connect).

Each provider has a start URL builder and a callback handler that returns a Profile. The
callback handlers talk to the provider's token endpoint directly over TLS, which is what lets
us read Apple's id_token claims without fetching its signing keys (OpenID Connect Core 3.1.3.7).
"""
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import jwt

from . import config

TIMEOUT = 15


class OAuthError(Exception):
    pass


@dataclass
class Profile:
    provider: str
    subject: str
    name: str = ""
    handle: str = ""
    email: str = ""
    email_verified: bool = False
    avatar_url: str = ""
    label: str = ""


PROVIDERS = {
    "steam": "Steam",
    "discord": "Discord",
    "google": "Google",
    "apple": "Apple",
}


def enabled() -> list[str]:
    on = {
        "steam": config.STEAM_LOGIN,
        "discord": bool(config.DISCORD_CLIENT_ID and config.DISCORD_CLIENT_SECRET),
        "google": bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
        "apple": bool(config.APPLE_CLIENT_ID and config.APPLE_TEAM_ID and config.APPLE_KEY_ID and config.apple_private_key()),
    }
    return [p for p in PROVIDERS if on[p]]


def _http() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "lanparty-manager"})


# ---- start URLs ---------------------------------------------------------------

STEAM_OPENID = "https://steamcommunity.com/openid/login"


def start_url(provider: str, redirect_uri: str, realm: str, state: str, nonce: str) -> str:
    if provider == "steam":
        return STEAM_OPENID + "?" + urlencode({
            "openid.ns": "http://specs.openid.net/auth/2.0",
            "openid.mode": "checkid_setup",
            "openid.return_to": f"{redirect_uri}?state={state}",
            "openid.realm": realm,
            "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
            "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
        })
    if provider == "discord":
        return "https://discord.com/oauth2/authorize?" + urlencode({
            "response_type": "code", "client_id": config.DISCORD_CLIENT_ID, "scope": "identify email",
            "state": state, "redirect_uri": redirect_uri, "prompt": "none",
        })
    if provider == "google":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "response_type": "code", "client_id": config.GOOGLE_CLIENT_ID, "scope": "openid email profile",
            "state": state, "nonce": nonce, "redirect_uri": redirect_uri, "prompt": "select_account",
        })
    if provider == "apple":
        return "https://appleid.apple.com/auth/authorize?" + urlencode({
            "response_type": "code", "response_mode": "form_post", "client_id": config.APPLE_CLIENT_ID,
            "scope": "name email", "state": state, "nonce": nonce, "redirect_uri": redirect_uri,
        })
    raise OAuthError("Unknown sign-in provider.")


# ---- callbacks ----------------------------------------------------------------

STEAM_ID = re.compile(r"^https://steamcommunity\.com/openid/id/(\d{17})$")


def steam_profile(params: dict, redirect_uri: str) -> Profile:
    if params.get("openid.mode") != "id_res":
        raise OAuthError("Steam sign-in was cancelled.")
    if params.get("openid.op_endpoint") != STEAM_OPENID:
        raise OAuthError("That sign-in didn't come from Steam.")
    if not str(params.get("openid.return_to", "")).startswith(redirect_uri + "?"):
        raise OAuthError("Steam sent the sign-in back to a different address.")
    match = STEAM_ID.match(params.get("openid.claimed_id", ""))
    if not match:
        raise OAuthError("Steam didn't return an account id.")
    check = {k: v for k, v in params.items() if k.startswith("openid.")}
    check["openid.mode"] = "check_authentication"
    with _http() as c:
        r = c.post(STEAM_OPENID, data=check)
    if r.status_code != 200 or "is_valid:true" not in r.text:
        raise OAuthError("Steam couldn't confirm the sign-in. Try again.")
    steamid = match.group(1)
    profile = Profile(provider="steam", subject=steamid, label=f"Steam {steamid}")
    if config.STEAM_API_KEY:
        try:
            with _http() as c:
                r = c.get("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/",
                          params={"key": config.STEAM_API_KEY, "steamids": steamid})
            player = (r.json().get("response", {}).get("players") or [{}])[0]
            profile.handle = player.get("personaname", "")
            profile.avatar_url = player.get("avatarfull", "")
            profile.label = profile.handle or profile.label
        except (httpx.HTTPError, ValueError):
            pass  # profile details are a nice-to-have
    return profile


def _exchange(url: str, data: dict) -> dict:
    with _http() as c:
        r = c.post(url, data=data, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError("The sign-in provider rejected the login. Try again.")
    return r.json()


def discord_profile(code: str, redirect_uri: str) -> Profile:
    tok = _exchange("https://discord.com/api/oauth2/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": config.DISCORD_CLIENT_ID, "client_secret": config.DISCORD_CLIENT_SECRET,
    })
    with _http() as c:
        r = c.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    if r.status_code != 200:
        raise OAuthError("Couldn't read your Discord profile.")
    me = r.json()
    avatar = f"https://cdn.discordapp.com/avatars/{me['id']}/{me['avatar']}.png" if me.get("avatar") else ""
    return Profile(
        provider="discord", subject=str(me["id"]), name=me.get("global_name") or "", handle=me.get("username", ""),
        email=me.get("email") or "", email_verified=bool(me.get("verified")), avatar_url=avatar,
        label=me.get("username", ""),
    )


def google_profile(code: str, redirect_uri: str, nonce: str) -> Profile:
    tok = _exchange("https://oauth2.googleapis.com/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
    })
    claims = _id_claims(tok.get("id_token", ""), ("https://accounts.google.com", "accounts.google.com"),
                        config.GOOGLE_CLIENT_ID, nonce)
    return Profile(
        provider="google", subject=str(claims["sub"]), name=claims.get("name", ""), email=claims.get("email", ""),
        email_verified=claims.get("email_verified") in (True, "true"), avatar_url=claims.get("picture", ""),
        label=claims.get("email", ""),
    )


def apple_client_secret() -> str:
    now = int(time.time())
    return jwt.encode(
        {"iss": config.APPLE_TEAM_ID, "iat": now, "exp": now + 300, "aud": "https://appleid.apple.com",
         "sub": config.APPLE_CLIENT_ID},
        config.apple_private_key(), algorithm="ES256", headers={"kid": config.APPLE_KEY_ID},
    )


def apple_profile(code: str, redirect_uri: str, nonce: str, user_json: dict | None) -> Profile:
    tok = _exchange("https://appleid.apple.com/auth/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": config.APPLE_CLIENT_ID, "client_secret": apple_client_secret(),
    })
    claims = _id_claims(tok.get("id_token", ""), ("https://appleid.apple.com",), config.APPLE_CLIENT_ID, nonce)
    name = ""
    if user_json:  # Apple sends the name once, on the very first sign-in
        n = user_json.get("name") or {}
        name = " ".join(x for x in (n.get("firstName"), n.get("lastName")) if x)
    email = claims.get("email", "")
    return Profile(
        provider="apple", subject=str(claims["sub"]), name=name, email=email,
        email_verified=claims.get("email_verified") in (True, "true"), label=email or "Apple ID",
    )


def _id_claims(id_token: str, issuers: tuple, audience: str, nonce: str) -> dict:
    """Claims from an id_token received straight from the provider's token endpoint over TLS."""
    if not id_token:
        raise OAuthError("The sign-in provider didn't return an identity.")
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise OAuthError("The sign-in provider returned an unreadable identity.") from e
    aud = claims.get("aud")
    if claims.get("iss") not in issuers or audience not in (aud if isinstance(aud, list) else [aud]):
        raise OAuthError("That sign-in was meant for a different site.")
    if claims.get("nonce") != nonce:
        raise OAuthError("That sign-in link was already used or has expired. Try again.")
    if int(claims.get("exp", 0)) < time.time():
        raise OAuthError("That sign-in expired. Try again.")
    if not claims.get("sub"):
        raise OAuthError("The sign-in provider didn't return an account id.")
    return claims
