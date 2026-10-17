# plushboard-back

FastAPI backend of Plushboard — REST + MCP + scheduler. Pairs with
[plushboard-front](https://github.com/SartoRiccardo/plushboard-front)
(static SPA, served by the VPS's Caddy) and
[plushboard-infra](https://github.com/SartoRiccardo/plushboard-infra)
(compose + Caddy site).

## Dev

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
cp .env.example .env   # fill it
.venv/bin/uvicorn app.main:app --reload
.venv/bin/python test_engine.py  # …and the other test_*.py suites
```

Note: day-to-day development happens in the plushboard monolith checkout
(single tree, `make dev` runs backend + Vite); this repo is what CI builds
and ships.

## Deploy

CI (`.github/workflows/ci.yml`) runs the suites on every PR/push to main and
publishes `ghcr.io/sartoriccardo/plushboard-back:latest` (tags → version
tags). plushboard-infra's compose pulls it.
