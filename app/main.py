import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from . import config, db, engine
from .api import router as api_router
from .auth import router as auth_router, session_email
from .config import missing_env
from .mcp_server import mcp

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if missing := missing_env():
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)} — see .env.example")
    db.init_db()
    with db.db() as c:
        engine.seed_settings(c)
    scheduler = asyncio.create_task(engine.scheduler_loop())
    # a mounted sub-app's lifespan never runs — the host must enter the MCP
    # session manager (streamable_http_app() is called at module level)
    async with mcp.session_manager.run():
        yield
    scheduler.cancel()


app = FastAPI(title="Plushboard", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(api_router)

if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

# MCP (§8): bearer-authed, NOT behind OAuth. Bearer check happens in ASGI
# middleware before the MCP app sees the request. Registered as a direct Route
# (not a Mount) so the endpoint is exactly /mcp — a Mount would 307-redirect
# to /mcp/, and the SDK manager matches the raw request path.
app.router.routes.append(
    Route("/mcp", mcp.streamable_http_app(streamable_http_path="/mcp"),
          methods=["GET", "POST", "DELETE"])
)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    resp = await call_next(request)
    # anti-SEO belt and braces: nothing from this app belongs in any index
    resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    # quick-release caching policy: hashed assets live forever, the HTML shell
    # revalidates every load, and API/MCP responses are never cached anywhere
    # (browser or Cloudflare) — stale JSON on a live dashboard is a bug
    if request.url.path.startswith("/assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith(("/api/", "/mcp")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        token = config.env("DASH_MCP_TOKEN")
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {token}" if token else ""
        if not token or not secrets.compare_digest(auth, expected):
            return JSONResponse({"error": "bad or missing bearer token"}, status_code=401)
    return await call_next(request)


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str, user: str | None = Depends(session_email)):
    if path.startswith(("api/", "mcp")):
        raise HTTPException(status_code=404)
    if user is None and path != "auth/login":
        # auth/login is the login screen itself — serve it logged-out too
        return RedirectResponse("/auth/login")
    if path and (f := DIST / path).is_file() and f.resolve().is_relative_to(DIST):
        return FileResponse(f)
    if (DIST / "index.html").is_file():
        return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
    return PlainTextResponse("Frontend not built. Dev: open http://localhost:5173")
