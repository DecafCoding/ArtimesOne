# ArtimesOne

ArtimesOne is a single-user personal AI assistant that collects content from sources
you subscribe to (YouTube channels first, more to come), summarizes it, and lets you
chat about the corpus through a web UI. Designed to run locally on your own machine.

## What works today (Phases 1–3 in progress)

- **Config** — pydantic-settings driven, graceful degradation with zero env vars
- **SQLite schema** — full v1 schema with WAL mode, FTS5 search, hand-rolled migrations
- **YouTube collector** — discovers new videos via the YouTube Data API, filters by duration, fetches transcripts via Apify, and records everything in the DB
- **Summarization pipeline** — pydantic-ai agent turns transcripts into 1–2 paragraph prose summaries with 3–7 topic tags; summaries stored as markdown with YAML front matter
- **Scheduler** — APScheduler 3.x runs the full discover → fetch → summarize pipeline per source on a cron schedule, with bounded auto-retry and graceful degradation
- **Web UI** — topic-grouped dashboard showing recent items with thumbnails, summaries, topic chips, and YouTube links; `/sources` page for managing YouTube channel sources
- **Entry point** — `python -m artimesone` starts FastAPI + scheduler in one process

## What's next

- Phase 3 (continued): Item detail, topic browsing, search, collection run log
- Phase 4: Chat agent with tool access over the corpus
- Phase 5: Telegram as a secondary chat surface

## Installation

### Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager (recommended)

### Setup

```bash
# Clone the repository
git clone https://github.com/DecafCoding/ArtimesOne.git
cd ArtimesOne

# Install dependencies
uv sync

# Copy the example env file and fill in your API keys
cp .env.example .env
```

Edit `.env` with your keys. All variables are optional — the app boots and serves the
web UI with zero env vars set. Missing keys disable the features they unlock:

| Variable | Required for | How to get it |
|----------|-------------|---------------|
| `ARTIMESONE_YOUTUBE_API_KEY` | YouTube video discovery | [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → YouTube Data API v3 |
| `APIFY_TOKEN` | Transcript fetching | [Apify Console](https://console.apify.com/) → Settings → Integrations |
| `OPENAI_API_KEY` | Summarization and chat | [OpenAI Platform](https://platform.openai.com/api-keys) |

### Running

```bash
uv run python -m artimesone
```

Then open <http://127.0.0.1:8000/>.

On first run the app creates the `data/` and `content/` directories automatically,
runs database migrations, and starts the scheduler. No separate `init` or `migrate`
step is needed.

### Using a local LLM instead of OpenAI

ArtimesOne uses [pydantic-ai](https://ai.pydantic.dev) model strings, so any
OpenAI-compatible backend works. Point `OPENAI_BASE_URL` at your local server:

```bash
# .env — example for Ollama
OPENAI_BASE_URL=http://localhost:11434/v1
ARTIMESONE_SUMMARY_MODEL=openai:llama3
ARTIMESONE_CHAT_MODEL=openai:llama3
```

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Lint + format + type check
uv run ruff check .
uv run ruff format --check .
uv run mypy artimesone

# Tests (86 tests, all offline, no live API calls)
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
    filters.py         # Jinja2 template filters (duration, dates, text)
    routes/            # FastAPI routers (dashboard, sources)
    templates/         # Jinja2 templates (Pico CSS + HTMX)
    static/            # Static assets (custom CSS)
tests/                 # pytest + respx, zero live API calls
```

## Configuration reference

See [.env.example](./.env.example) for the full list of environment variables with
descriptions. Key settings:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ARTIMESONE_HOST` | `127.0.0.1` | Web server bind address |
| `ARTIMESONE_PORT` | `8000` | Web server port |
| `ARTIMESONE_DATA_DIR` | `./data` | SQLite database directory |
| `ARTIMESONE_CONTENT_DIR` | `./content` | Markdown files (transcripts, summaries) |
| `ARTIMESONE_SUMMARY_MODEL` | `openai:gpt-4o-mini` | Model for summarization |
| `ARTIMESONE_CHAT_MODEL` | `openai:gpt-4o` | Model for the chat agent |
| `ARTIMESONE_MAX_VIDEO_DURATION_MINUTES` | `60` | Skip videos longer than this |
