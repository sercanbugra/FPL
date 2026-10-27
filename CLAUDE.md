# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A Django web app ("FPL Dashboard") for Fantasy Premier League analytics.

## Commands

```bash
pip install -r requirements.txt

python manage.py runserver          # dev server at http://127.0.0.1:8000
python manage.py test -v 2          # full test suite (Django test runner, unittest-based)
python manage.py test fpldash.tests.CacheTests               # one test class
python manage.py test fpldash.tests.CacheTests.test_cache_refetches_after_ttl  # one test
python manage.py check --deploy --fail-level WARNING          # deploy/security checks (run in CI)
python manage.py collectstatic --noinput                      # bundles static assets (whitenoise)
```

CI (`.github/workflows/ci.yml`, Python 3.11) runs `manage.py check --deploy` and
`manage.py test` with `DEBUG=False` on pushes to `main`, `feature/**`, `fix/**` and on
PRs to `main`. Keep both green.

Config comes from env vars, loaded from `.env` via `python-dotenv`:
`SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` (comma-separated), `FPL_TEAM_ID`
(the default manager ID shown in My Squad), `FPL_LEAGUE_ID` (the classic league
shown in the Mini League tab, default `659612`), and `TWITTER_BEARER_TOKEN` (optional;
when set, price-change tweets come from the official X API instead of the scraping
fallbacks).

The app deploys to Fly.io (`fly.toml`, app `fpl-tonwvq`) via the `Dockerfile`
(gunicorn, `PORT=8080`, collectstatic runs at image build). The `app` name in `fly.toml`
must match an app that actually exists under the account (`flyctl apps list`).
A stale or renamed name still passes `flyctl deploy` validation but deploys nowhere
useful. Re-running `fly launch` regenerates `fly.toml` from scratch with a new app name
and resets `PORT` to `8000` while `internal_port` stays `8080`. That causes the classic
"not listening on 0.0.0.0:8080" 502. Deploy with `flyctl deploy --app <name>` and verify
with `flyctl status --app <name>` / `flyctl logs --app <name>` rather than assuming
`fly.toml` is already correct.

## Architecture

This is a single-app Django project: `fplsite` (settings/urls/wsgi) wraps one app, `fpldash`.
There is no database-backed domain model. `db.sqlite3` only holds Django's
built-in tables (admin/auth/sessions). All FPL data is fetched live from the
official FPL API and never persisted. Instead, each layer keeps its own in-process,
lock-guarded 30-min TTL cache, so a single gunicorn worker serves mostly from memory
and each worker keeps its own separate cache.

**Data flow**, request → response:

1. `fpldash/cache.py` is the shared cache for
   `https://fantasy.premierleague.com/api/bootstrap-static/` and `.../fixtures/`.
   `get_bootstrap()` and `get_fixtures()` are the only places that hit those two
   endpoints, and everything else reads through them. `compute_team_fdr()` derives
   per-team average fixture difficulty for the next N gameweeks.
2. `fpldash/fpl_data.py` and `fpldash/forecast.py` build pandas DataFrames from bootstrap
   data in the shapes the frontend tables expect. `get_fpl_data()` builds the main stats
   table. `get_forecast_data()` covers the *entire* player pool (~600+) by default
   (`limit=None`). It fetches per-player `element-summary` histories in parallel via
   `ThreadPoolExecutor` and caches them in its own store.
3. `fpldash/ml_predictions.py` trains a 4-model ensemble (Ridge, RandomForest,
   GradientBoosting, XGBoost) on all current-season players' per-90 underlying stats
   (xG, xA, influence, creativity, threat, etc.) against `points_per_game`. It retrains
   each time its cache expires and returns `{player_id: predicted_score}`. This
   ensemble average is the single source of truth for "Predicted Score". `forecast.py`,
   `/api/suggestions` and `squad_builder.py` all consume it, so don't add a parallel
   heuristic.
4. `fpldash/squad_builder.py` formulates 15-man squad selection as an integer program
   (PuLP + bundled CBC). The constraints are 2-5-5-3 position quotas, the budget, and a
   3-per-club cap. It also jointly picks the best formation-legal starting XI. It solves
   once per entry in `STRATEGIES` (different quality/value weightings) to get genuinely
   different squads instead of near-duplicates. Results are cached per budget. If
   `ml_predictions` can't train yet (pre-GW1, returns `{}`), it falls back to the FPL
   API's own estimate (`ep_next` → `ep_this` → last season's PPG).
5. `fpldash/league.py` holds pure functions over the cached bootstrap/fixtures payloads.
   They build the Premier League table, computed from the final scores in FPL fixtures,
   and per-gameweek fixture lists. "This week's" gameweek is the current GW until all
   of its matches are played, then the next one. FPL's event `finished` flag lags behind
   bonus processing, so it isn't used for this. Live scores are only as fresh as the
   30-min fixtures cache.
6. `fpldash/mini_league.py` builds the Mini League report for a classic league:
   standings, then for every manager `entry/{id}/history/` and the captain from
   `entry/{id}/event/{gw}/picks/` for every GW, scored with `event/{gw}/live/`. That's
   roughly managers × GWs requests (capped at `MAX_MANAGERS`), fetched in parallel.
   Responses for *finished* GWs are cached permanently because they never change.
   Everything else uses the usual 30-min TTL. `build_weeks` / `build_awards` / `build_chips` / `build_insights` /
   `build_records` are pure and tested without the network.
   The same module also serves `/api/captaincy`, the worldwide captain split. FPL has no
   endpoint for that distribution, so it's *estimated* by sampling ~400 managers
   evenly across the overall league (id 314). FPL throttles to ~10 req/s, so a sample
   takes ~30–45 s. It runs once per GW in a background thread (picks lock at the
   deadline), and the endpoint returns `{"status": "pending"}` until it's done, so the
   frontend polls.
7. `fpldash/views.py` is kept thin. Each `api_*` view builds data via the modules above
   and returns `JsonResponse`. Business logic beyond request parsing and error wrapping
   doesn't belong here. A few views call the FPL API directly and are **uncached**:
   - `/api/myteam` (optional `?id=` for any manager, default `FPL_TEAM_ID`) calls
     `entry/{id}/event/{gw}/picks/`.
   - `/api/player-summary/<id>` calls `element-summary`.
   - `/api/pricechanges` tries three sources in order: official X API (needs the
     token), Nitter RSS mirrors, then a Twitter syndication endpoint scrape.
     `/api/pricechanges_fpl` instead derives changes from bootstrap `cost_change_event`.
8. `fpldash/templates/fpldash/index.html`, `static/fpldash/polygon.js` and
   `static/fpldash/mini_league.js` make up a
   single-page frontend (jQuery + DataTables + Bootstrap 5 + Chart.js via CDN, no build
   step). The layout is a left **My Squad** sidebar (team-ID input, resizable) plus main
   tabs: Player Stats (`/api/data`), GW Forecast (`/api/forecast`), Polygon Filter (a
   radar chart whose metrics are read dynamically from `/api/data`, with no hardcoded
   whitelist) and Squad Builder (`/api/squad-builder`), Table & Fixtures (league table from
   `/api/table` beside the GW fixtures from `/api/fixtures?gw=`; `#fixtures` aliases it) and Mini League (`/api/mini-league`, an animated report).
   `/#<tab>` deep-links to a tab. Static asset URLs carry a `?v=N` cache-buster, so bump
   it when you edit a CSS/JS file. A right rail (Price Changes /
   Smart Picks) shows only alongside Player Stats. All server communication is plain
   `fetch()` to the `api_*` endpoints.

Because nothing is persisted, correctness work is almost always about the
transform/scoring logic in `fpl_data.py` / `forecast.py` / `ml_predictions.py` /
`squad_builder.py`, or about cache TTL/threading. `fpldash/tests.py` covers these with
no real network calls: `SmokeTests` (rendered page), `CacheTests` (`requests.get`
mocked), `SquadBuilderTests` (ILP squad legality against a small synthetic bootstrap
fixture) and `SquadBuilderViewTests`.
