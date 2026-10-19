"""Sign in / sign up with Google and GitHub (#2336, ADR-0119).

INBOUND identity: a person proves who they are with a Google or GitHub
account and gets the same JWT ``POST /token`` issues. This is NOT
:mod:`cairn.core.oauth`, which is OUTBOUND (connecting cairn *to* GitHub/Slack
for operators); the two share Authlib and nothing else — different scopes,
different callbacks, no token is stored here.

The flow is the authorization-code grant, run in cairn's own process:

1. :func:`begin` builds the provider URL with a signed ``state`` (+ PKCE for
   Google) and a signed, HttpOnly cookie carrying the same nonce, so the
   callback is bound to the browser that started it (login CSRF).
2. :func:`complete` checks state against the cookie, exchanges the code and
   reads the **verified** email: Google's ``email_verified`` claim from the ID
   token, GitHub's primary verified address from ``/user/emails``.
3. :func:`sign_in` decides, in order: a linked identity → that user; a user
   whose login IS that verified email → link and sign in; otherwise the
   sign-up door (:func:`cairn.core.signup.signup`), where ``CAIRN_SIGNUP`` and
   the first-run bootstrap decide exactly as they do for a password.

Refusals carry a machine-readable ``reason`` the console explains.

Config (inventoried in :mod:`cairn.core.settings`): ``CAIRN_OAUTH_GOOGLE_CLIENT_ID``
/ ``_SECRET``, ``CAIRN_OAUTH_GITHUB_CLIENT_ID`` / ``_SECRET``, ``CAIRN_PUBLIC_URL``.
A provider with no client id is not offered. ``authlib`` stays the optional
``[oauth]`` extra: without it no provider is offered.
"""
from __future__ import annotations
from cairn.core.settings import getenv

import hashlib
import hmac
import importlib.util
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

import jwt

from cairn.core.account import Identity, User, get_account_store, link_identity
from cairn.core.signup import SignupError, signup

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Provider:
    authorize_url: str
    token_url: str
    scope: str
    pkce: bool


#: Sign-in scopes only — identity and email, nothing that reaches a repo or a
#: mailbox. Deliberately not the outbound catalog (``oauth/providers.yaml``),
#: whose GitHub entry asks for ``repo``.
PROVIDERS: dict[str, _Provider] = {
    "google": _Provider(
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email profile",
        pkce=True,
    ),
    "github": _Provider(
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email",
        pkce=False,
    ),
}
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_GITHUB_API = "https://api.github.com"

#: How long a started sign-in stays completable. Long enough for a consent
#: screen and a 2FA prompt; short enough that a leaked state is worthless.
STATE_TTL_SECONDS = 600
#: The cookie that binds a callback to the browser that started the sign-in.
COOKIE_NAME = "cairn_oauth"
COOKIE_PATH = "/auth/oauth/"

#: Test seam: an ``httpx`` transport (MockTransport) for every provider call.
_transport: Any = None


class OAuthLoginError(Exception):
    """A sign-in was refused. ``reason`` is what the console explains:
    ``oauth_unavailable`` · ``oauth_state`` · ``oauth_denied`` · ``oauth_failed``
    · ``email_not_verified`` · ``account_disabled`` · or a sign-up door reason
    (``signup_closed``, ``domain_not_registered``, ``public_domain``,
    ``bad_email``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProviderIdentity:
    """What the provider vouched for after the code exchange."""

    provider: str
    subject: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class SignInOutcome:
    user: User
    account_id: Optional[str]
    created_account: bool
    linked: bool = False


@dataclass(frozen=True)
class StartedSignIn:
    authorize_url: str
    cookie: str
    redirect_uri: str


# ── config ────────────────────────────────────────────────────────────────


def client_credentials(provider: str) -> tuple[str, str]:
    p = provider.strip().upper()
    return (getenv(f"CAIRN_OAUTH_{p}_CLIENT_ID", "").strip(),
            getenv(f"CAIRN_OAUTH_{p}_CLIENT_SECRET", "").strip())


def configured_providers() -> list[str]:
    """The providers this deployment offers: a client id is set and Authlib is
    installed. Order is stable (the console renders it as-is)."""
    named = [p for p in PROVIDERS if client_credentials(p)[0]]
    if named and importlib.util.find_spec("authlib") is None:
        logger.warning("OAuth sign-in configured for %s but authlib is not installed "
                       "(the [oauth] extra) — no provider is offered", ", ".join(named))
        return []
    return named


def redirect_uri(provider: str, request_base_url: str) -> str:
    """The callback the provider returns to: ``CAIRN_PUBLIC_URL`` when set (a
    deployment behind a proxy must set it), else the URL this request came in
    on. Either way the provider only honours a URI registered with it."""
    base = (getenv("CAIRN_PUBLIC_URL", "").strip() or request_base_url).rstrip("/")
    return f"{base}/auth/oauth/{provider}/callback"


# ── state ────────────────────────────────────────────────────────────────


def _key(secret: str) -> bytes:
    # A key DERIVED from the JWT secret: a state or cookie can never verify as
    # an access token, and an access token never verifies as a state.
    return hmac.new(secret.encode(), b"cairn.oauth-login.state", hashlib.sha256).digest()


def _sign(payload: dict[str, Any], secret: str) -> str:
    return jwt.encode({**payload, "exp": int(time.time()) + STATE_TTL_SECONDS},
                      _key(secret), algorithm="HS256")


def _verify(token: str, secret: str, typ: str) -> Optional[dict[str, Any]]:
    try:
        claims = jwt.decode(token or "", _key(secret), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    return claims if claims.get("typ") == typ else None


def _bound_state(provider: str, state: str, cookie: str, secret: str) -> dict[str, Any]:
    """The cookie's claims, when ``state`` and ``cookie`` are both genuine, in
    date, for this provider, and carry the same nonce. Else ``oauth_state``."""
    st = _verify(state, secret, "oauth_state") or {}
    ck = _verify(cookie, secret, "oauth_cookie") or {}
    nonce = str(st.get("n", ""))
    if not (nonce and st.get("p") == provider == ck.get("p")
            and hmac.compare_digest(nonce, str(ck.get("n", "")))):
        raise OAuthLoginError("oauth_state")
    return ck


# ── the flow ─────────────────────────────────────────────────────────────


def _client(**kw: Any) -> Any:
    from authlib.integrations.httpx_client import AsyncOAuth2Client  # [oauth] extra

    if _transport is not None:
        kw["transport"] = _transport
    return AsyncOAuth2Client(**{k: v for k, v in kw.items() if v is not None})


async def begin(provider: str, *, redirect_uri: str, secret: str) -> StartedSignIn:
    """The provider URL to send the browser to, and the cookie to set."""
    spec = PROVIDERS.get(provider)
    if spec is None or provider not in configured_providers():
        raise OAuthLoginError("oauth_unavailable")
    cid, _ = client_credentials(provider)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64) if spec.pkce else ""
    state = _sign({"typ": "oauth_state", "p": provider, "n": nonce}, secret)
    cookie = _sign({"typ": "oauth_cookie", "p": provider, "n": nonce, "v": verifier}, secret)
    client = _client(client_id=cid, scope=spec.scope, redirect_uri=redirect_uri,
                     code_challenge_method="S256" if spec.pkce else None)
    try:
        extra = {"prompt": "select_account"} if provider == "google" else {}
        url, _ = client.create_authorization_url(
            spec.authorize_url, state=state, code_verifier=verifier or None, **extra)
    finally:
        await client.aclose()
    return StartedSignIn(authorize_url=url, cookie=cookie, redirect_uri=redirect_uri)


async def complete(
    provider: str, *, code: str, state: str, cookie: str, redirect_uri: str, secret: str,
) -> ProviderIdentity:
    """Check the state, exchange the code, and read who the provider says this is."""
    spec = PROVIDERS.get(provider)
    if spec is None or provider not in configured_providers():
        raise OAuthLoginError("oauth_unavailable")
    ck = _bound_state(provider, state, cookie, secret)
    if not code:
        raise OAuthLoginError("oauth_failed")
    cid, csecret = client_credentials(provider)
    client = _client(client_id=cid, client_secret=csecret or None, redirect_uri=redirect_uri,
                     token_endpoint_auth_method="client_secret_post")
    try:
        token = await client.fetch_token(spec.token_url, code=code,
                                         code_verifier=(ck.get("v") or None))
        if provider == "google":
            return _google_identity(dict(token), cid)
        return await _github_identity(client)
    except OAuthLoginError:
        raise
    except Exception as e:  # noqa: BLE001 — any provider/transport failure is one refusal
        logger.warning("OAuth sign-in with %s failed at the provider: %s", provider, type(e).__name__)
        raise OAuthLoginError("oauth_failed") from None
    finally:
        await client.aclose()


def _google_identity(token: dict[str, Any], client_id: str) -> ProviderIdentity:
    """Claims from the ID token. The token came straight from Google's token
    endpoint over TLS in the code exchange, so its signature is not re-checked
    (OIDC Core 3.1.3.7); issuer, audience and expiry are."""
    raw = token.get("id_token") or ""
    claims = jwt.decode(raw, options={"verify_signature": False})
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if (claims.get("iss") not in _GOOGLE_ISSUERS or client_id not in auds
            or float(claims.get("exp") or 0) < time.time() or not claims.get("sub")):
        raise OAuthLoginError("oauth_failed")
    verified = claims.get("email_verified")
    return ProviderIdentity(
        provider="google", subject=str(claims["sub"]), email=str(claims.get("email") or ""),
        email_verified=verified is True or str(verified).lower() == "true")


async def _github_identity(client: Any) -> ProviderIdentity:
    """GitHub's numeric user id, and its PRIMARY address only if verified. A
    verified secondary address is not used: the person chose the primary."""
    headers = {"Accept": "application/vnd.github+json"}
    who = await client.get(f"{_GITHUB_API}/user", headers=headers)
    who.raise_for_status()
    subject = str((who.json() or {}).get("id") or "")
    if not subject:
        raise OAuthLoginError("oauth_failed")
    mails = await client.get(f"{_GITHUB_API}/user/emails", headers=headers)
    mails.raise_for_status()
    primary = next((m for m in (mails.json() or []) if m.get("primary")), None) or {}
    return ProviderIdentity(provider="github", subject=subject,
                            email=str(primary.get("email") or ""),
                            email_verified=bool(primary.get("verified")))


async def sign_in(pid: ProviderIdentity, *, store: Any = None) -> SignInOutcome:
    """Who this identity signs in as — or the sign-up door's answer.

    Linking happens ONLY on a provider-verified email that exactly matches an
    existing login, and an account is never created from an unverified email."""
    store = store or get_account_store()
    email = (pid.email or "").strip().lower()
    if not pid.email_verified or not email:
        raise OAuthLoginError("email_not_verified")
    linked = False
    user = await store.get_user_by_identity(pid.provider, pid.subject)
    if user is None:
        user = await store.get_user_by_username(email)
        if user is not None and not user.disabled:
            user = await link_identity(
                user, Identity(provider=pid.provider, subject=pid.subject, email=email), store=store)
            linked = True
    if user is not None:
        if user.disabled:
            raise OAuthLoginError("account_disabled")
        return SignInOutcome(user=user, account_id=await store.get_account_for_user(user.id),
                             created_account=False, linked=linked)
    try:
        acct, user, created = await signup(
            "", email, "", store=store,
            identity=Identity(provider=pid.provider, subject=pid.subject, email=email))
    except SignupError as e:
        raise OAuthLoginError(e.reason) from None
    return SignInOutcome(user=user, account_id=acct.id, created_account=created)
