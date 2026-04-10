# ArtimesOne

ArtimesOne is a single-user personal AI assistant that collects content from sources
you subscribe to (YouTube channels first, more to come), summarizes it, and lets you
chat about the corpus through a web UI. Designed to run locally on your own machine.

## Quickstart

```bash
uv sync
uv run python -m artimesone
```

Then open <http://127.0.0.1:8000/>.

The app boots with zero environment variables set; missing API keys simply disable the
features they unlock. To enable the YouTube collector, set `ARTIMESONE_YOUTUBE_API_KEY`
in your environment or in a local `.env` file (see [.env.example](./.env.example)).

## What works today (Phase 1)

- **Config** — pydantic-settings driven, graceful degradation with zero env vars
- **SQLite schema** — full v1 schema with WAL mode, FTS5 search, hand-rolled migrations
- **YouTube collector** — discovers new videos via the YouTube Data API, filters by duration, records them in the DB. Transcripts and summaries are Phase 2.
- **Scheduler** — APScheduler 3.x runs collection on a per-source cron schedule
- **Web UI** — Pico CSS dashboard and `/sources` page for adding, enabling, disabling, and deleting YouTube channel sources
- **Entry point** — `python -m artimesone` starts FastAPI + scheduler in one process

## What's next

- Phase 2: Apify transcript fetching, OpenAI summarization, topic tagging
- Phase 3: Browsing UI (topic-grouped dashboard, item detail, search)
- Phase 4: Chat agent with tool access over the corpus
- Phase 5: Telegram as a secondary chat surface

## Development

```bash
uv sync --extra dev

# Lint + format + type check
uv run ruff check .
uv run ruff format --check .
uv run mypy artimesone

# Tests (27 tests, all offline, <1s)
uv run python -m pytest -v
```

## Project structure

```
artimesone/
  __main__.py          # python -m artimesone entry point
  app.py               # FastAPI factory + lifespan + dependency helpers
  config.py            # pydantic-settings (env vars + .env)
  db.py                # SQLite connection helper (WAL, FKs)
  scheduler.py         # APScheduler wiring for collection runs
  migrations/          # Hand-rolled SQL, forward-only
    0001_initial.sql   # Full v1 schema
  collectors/          # Collector protocol + registry
    youtube/           # YouTube Data API client + channel collector
  web/
    routes/            # FastAPI routers (dashboard, sources)
    templates/         # Jinja2 templates (Pico CSS)
    static/            # Static assets
tests/                 # pytest + respx, zero live API calls
```
