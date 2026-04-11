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
features they unlock. Copy `.env.example` to `.env` and fill in the keys you have:

| Variable | Unlocks |
|----------|---------|
| `ARTIMESONE_YOUTUBE_API_KEY` | YouTube video discovery |
| `APIFY_TOKEN` | Transcript fetching via Apify |
| `OPENAI_API_KEY` | Summarization (prose + topic tags) |

See [.env.example](./.env.example) for the full list.

## What works today (Phases 1–2)

- **Config** — pydantic-settings driven, graceful degradation with zero env vars
- **SQLite schema** — full v1 schema with WAL mode, FTS5 search, hand-rolled migrations
- **YouTube collector** — discovers new videos via the YouTube Data API, filters by duration, fetches transcripts via Apify, and records everything in the DB
- **Summarization pipeline** — pydantic-ai agent turns transcripts into 1–2 paragraph prose summaries with 3–7 topic tags; summaries stored as markdown with YAML front matter
- **Scheduler** — APScheduler 3.x runs the full discover → fetch → summarize pipeline per source on a cron schedule, with bounded auto-retry and graceful degradation
- **Web UI** — Pico CSS dashboard and `/sources` page for adding, enabling, disabling, and deleting YouTube channel sources
- **Entry point** — `python -m artimesone` starts FastAPI + scheduler in one process

## What's next

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

# Tests (62 tests, all offline, no live API calls)
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
    youtube/           # YouTube Data API + Apify transcript client + channel collector
  agents/              # pydantic-ai agents (summarizer)
  pipeline/            # Processing pipelines (summarize: transcript → summary + tags)
  web/
    routes/            # FastAPI routers (dashboard, sources)
    templates/         # Jinja2 templates (Pico CSS)
    static/            # Static assets
tests/                 # pytest + respx, zero live API calls
```
