"""Source registry. Each slice adds its module here."""
from dataclasses import dataclass
from typing import Awaitable, Callable

from .. import config, engine
from . import dataforseo, giphy, gsc, mc, plausible


@dataclass
class Source:
    id: str
    label: str
    free: bool
    has_creds: Callable[[], bool]
    refresh: Callable[[engine.Ctx], Awaitable[dict]] | None
    view: Callable | None       # async (c, f, t) -> composed payload
    card: Callable | None       # (c) -> overview card dict
    inspect: Callable | None = None  # async (path) -> inspection result (gsc only)


SOURCES: dict[str, Source] = {
    "plausible": Source(
        id="plausible",
        label="Traffic",
        free=True,
        has_creds=lambda: bool(config.env("PLAUSIBLE_BASE_URL") and config.env("PLAUSIBLE_KEY")),
        refresh=plausible.refresh,
        view=plausible.view,
        card=plausible.card,
    ),
    "gsc": Source(
        id="gsc",
        label="Search",
        free=True,
        has_creds=lambda: bool(config.env("GOOGLE_SA_JSON_B64") and config.env("GSC_SITE_URL")),
        refresh=gsc.refresh,
        view=gsc.view,
        card=gsc.card,
        inspect=gsc.inspect_url,
    ),
    # pseudo-source: URL inspection as its own 12h background job (free API);
    # rides the scheduler for cadence/runs/error isolation; no view or card —
    # the frontend nav is hardcoded, so no nav dot appears for it
    "inspect": Source(
        id="inspect",
        label="Inspect",
        free=True,
        has_creds=lambda: bool(config.env("GOOGLE_SA_JSON_B64") and config.env("GSC_SITE_URL")),
        refresh=gsc.inspect_all,
        view=None,
        card=None,
    ),
    "mc": Source(
        id="mc",
        label="Merchant",
        free=True,
        has_creds=lambda: bool(config.env("GOOGLE_SA_JSON_B64") and config.env("GMC_MERCHANT_ID")),
        refresh=mc.refresh,
        view=mc.view,
        card=mc.card,
    ),
    "giphy": Source(
        id="giphy",
        label="GEO",
        free=True,
        has_creds=lambda: bool(config.env("GIPHY_API_KEY")),
        refresh=giphy.refresh,
        view=giphy.view,
        card=giphy.card,
    ),
    # slice 5: PAID (§5.5) — free=False keeps it out of the scheduler and the
    # generic refresh route; its only entry points are the /api/keywords buttons
    "dataforseo": Source(
        id="dataforseo",
        label="Keywords",
        free=False,
        has_creds=lambda: bool(config.env("DATAFORSEO_LOGIN")
                               and config.env("DATAFORSEO_PASSWORD")),
        refresh=None,
        view=None,
        card=dataforseo.card,
    ),
}
