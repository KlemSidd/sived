"""Confirm an author is an active member of one editor's Patreon campaign.

The author signs in with Patreon. This module asks Patreon, with that login's
token, whether the account is currently entitled to the editor's campaign.
A name and email typed into this desk are not a membership check.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from app import config

AUTHORIZE_URL = "https://www.patreon.com/oauth2/authorize"
TOKEN_URL = "https://www.patreon.com/api/oauth2/token"
IDENTITY_URL = "https://www.patreon.com/api/oauth2/v2/identity"
SCOPES = "identity identity[email] identity.memberships"
USER_AGENT = "SIVED membership check"
# How long a stored confirmation lasts before the desk asks Patreon again.
RECHECK_SECONDS = 15 * 60


class PatreonError(Exception):
    """Patreon did not confirm a membership. The message is safe to show."""


def redirect_uri(host: str, scheme: str = "http") -> str:
    if config.PATREON_REDIRECT_URI:
        return config.PATREON_REDIRECT_URI
    scheme = "https" if scheme == "https" else "http"
    if host:
        return f"{scheme}://{host}/patreon/callback"
    shown = config.HOST if config.HOST not in ("0.0.0.0", "::") else "127.0.0.1"
    return f"{scheme}://{shown}:{config.PORT}/patreon/callback"


def webhook_url(host: str, scheme: str = "http") -> str:
    """The one webhook address each editor registers on their own client."""
    parts = urlsplit(redirect_uri(host, scheme))
    return urlunsplit((parts.scheme, parts.netloc, "/patreon/webhook", "", ""))


def authorize_url(state: str, host: str, client_id: str, scheme: str = "http") -> str:
    if not client_id:
        raise PatreonError("Patreon is not configured")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri(host, scheme),
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def login_from_code(code: str, host: str, client_id: str, client_secret: str, scheme: str = "http") -> dict:
    """Exchange the OAuth code with this editor's client and read entitlements."""
    tokens = _token(
        {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(host, scheme),
        }
    )
    return _member(tokens)


def confirm_with_refresh(refresh_token: str, campaign_id: str, client_id: str, client_secret: str) -> dict:
    """Ask Patreon again, with this editor's client, whether the login is entitled."""
    if not refresh_token:
        raise PatreonError("sign in with Patreon again")
    tokens = _token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    )
    member = _member(tokens)
    member["active"] = campaign_id in member["campaigns"]
    return member


def signature_ok(body: bytes, signature: str, secret: str) -> bool:
    """Patreon signs the raw body with HMAC-MD5 and that editor's webhook secret."""
    offered = (signature or "").strip().lower()
    if not secret or not offered:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.md5).hexdigest()
    if len(digest) != len(offered):
        return False
    return hmac.compare_digest(digest, offered)


def entitled_campaigns(payload: dict) -> set[str]:
    """Campaign ids this identity is entitled to right now.

    A campaign counts when Patreon reports a current entitled amount or at
    least one currently entitled tier. A former member matches neither.
    """
    found = set()
    for item in payload.get("included") or []:
        if item.get("type") != "member":
            continue
        attrs = item.get("attributes") or {}
        rel = item.get("relationships") or {}
        campaign = ((rel.get("campaign") or {}).get("data") or {}).get("id")
        if not campaign:
            continue
        if _entitled(attrs, rel):
            found.add(str(campaign))
    return found


def webhook_active(event: str, payload: dict) -> bool | None:
    """Whether this webhook says the member is still entitled.

    None means the payload did not say, and the stored status should stay.
    """
    if event == "members:delete":
        return False
    data = payload.get("data") or {}
    if data.get("type") != "member":
        return None
    attrs = data.get("attributes") or {}
    rel = data.get("relationships") or {}
    has_cents = "currently_entitled_amount_cents" in attrs
    has_tiers = "currently_entitled_tiers" in rel
    if has_cents or has_tiers:
        return _entitled(attrs, rel)
    status = attrs.get("patron_status")
    if status == "active_patron":
        return True
    if status in ("former_patron", "declined_patron"):
        return False
    return None


def _entitled(attrs: dict, rel: dict) -> bool:
    """True when Patreon reports money or a currently entitled tier."""
    try:
        cents = int(attrs.get("currently_entitled_amount_cents") or 0)
    except (TypeError, ValueError):
        cents = 0
    tiers = (rel.get("currently_entitled_tiers") or {}).get("data") or []
    if not isinstance(tiers, list):
        tiers = []
    return cents > 0 or len(tiers) > 0


def _member(tokens: dict) -> dict:
    access = tokens.get("access_token") or ""
    if not access:
        raise PatreonError("Patreon did not authorize this login")
    status, raw = _request(
        "GET",
        IDENTITY_URL + "?" + urlencode(
            {
                "include": "memberships.campaign,memberships.currently_entitled_tiers",
                "fields[user]": "email,full_name,is_email_verified",
                "fields[member]": "patron_status,currently_entitled_amount_cents",
            }
        ),
        {"Authorization": f"Bearer {access}", "User-Agent": USER_AGENT},
    )
    if status != 200:
        raise PatreonError("Patreon could not confirm this membership")
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatreonError("Patreon could not confirm this membership") from exc
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    email = (attrs.get("email") or "").strip().lower()
    if not data.get("id") or "@" not in email:
        raise PatreonError("Patreon did not share an email")
    # Patreon OAuth: do not create or link an account from an unverified email.
    if attrs.get("is_email_verified") is not True:
        raise PatreonError("verify your email at Patreon before signing in")
    return {
        "patreon_user_id": str(data["id"]),
        "email": email,
        "name": (attrs.get("full_name") or email).strip(),
        "refresh_token": tokens.get("refresh_token") or "",
        "campaigns": entitled_campaigns(payload),
    }


def _token(fields: dict) -> dict:
    if not fields.get("client_id") or not fields.get("client_secret"):
        raise PatreonError("Patreon is not configured")
    status, raw = _request(
        "POST",
        TOKEN_URL,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        urlencode(fields).encode(),
    )
    if status != 200:
        raise PatreonError("Patreon did not authorize this login")
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatreonError("Patreon did not authorize this login") from exc
    if not payload.get("access_token"):
        raise PatreonError("Patreon did not authorize this login")
    return payload


def _request(method: str, url: str, headers: dict, body: bytes | None = None, timeout: int = 15):
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
    except URLError as exc:
        raise PatreonError("Patreon could not be reached") from exc
