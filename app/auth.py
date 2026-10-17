import secrets
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, TimestampSigner, URLSafeSerializer

from . import config

router = APIRouter(prefix="/api/auth")

serializer = URLSafeSerializer(config.SESSION_SECRET, salt="plush_session")
_state = TimestampSigner(config.SESSION_SECRET, salt="oauth_state")
COOKIE = "plush_session"
COOKIE_MAX_AGE = 30 * 24 * 3600

JWKS = jwt.PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")


def session_email(request: Request) -> str | None:
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        return None
    try:
        return serializer.loads(cookie)["email"]
    except (BadSignature, KeyError):
        return None


def require_user(request: Request) -> str:
    email = session_email(request)
    if not email:
        raise HTTPException(status_code=401)
    return email


def _redirect_uri() -> str:
    return f"{config.PUBLIC_BASE_URL}/api/auth/callback"


def _set_session(resp: RedirectResponse, email: str) -> RedirectResponse:
    # `secure` follows PUBLIC_BASE_URL scheme: prod sits behind an HTTPS proxy;
    # Secure cookies over http://localhost break some browsers (Safari).
    resp.set_cookie(
        COOKIE,
        serializer.dumps({"email": email}),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=config.PUBLIC_BASE_URL.startswith("https"),
        samesite="lax",
    )
    return resp


@router.get("/mode")
async def auth_mode():
    """Public: tells the login page whether the local dev bypass exists."""
    return {"dev_bypass": config.APP_ENV == "local"}


@router.post("/dev-login")
async def dev_login():
    """LAN/dev bypass: session without the Google roundtrip (phone testing).
    Refuses unless APP_ENV=local — never available in prod. Returns JSON (no
    redirect) — the login page navigates itself once the cookie is set."""
    if config.APP_ENV != "local" or not config.ALLOWED_EMAILS:
        raise HTTPException(status_code=404)
    resp = JSONResponse({"ok": True})
    return _set_session(resp, config.ALLOWED_EMAILS[0])


@router.get("/google")
async def login():
    """OAuth start. /auth/login itself belongs to the SPA (the login screen) —
    the button navigates here explicitly, OAuth never fires automatically."""
    state = _state.sign(secrets.token_urlsafe(16)).decode()
    params = {
        "client_id": config.OAUTH_CLIENT_ID,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    }
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))


@router.get("/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    try:
        _state.unsign(state, max_age=600)
    except BadSignature:
        raise HTTPException(status_code=400, detail="bad oauth state")

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": config.OAUTH_CLIENT_ID,
                "client_secret": config.OAUTH_CLIENT_SECRET,
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    id_token = r.json().get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="token exchange failed")

    try:
        key = JWKS.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=config.OAUTH_CLIENT_ID,
            issuer="https://accounts.google.com",
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="id_token verification failed")

    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in config.ALLOWED_EMAILS:
        return HTMLResponse(
            "<h1>403 — not allowed</h1><p>Single-user dashboard. This email is not on the allowlist.</p>",
            status_code=403,
        )

    resp = RedirectResponse("/")
    return _set_session(resp, email)


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/auth/login")
    resp.delete_cookie(COOKIE)
    return resp
