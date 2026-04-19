# ArtimesOne

ArtimesOne is a single-user personal AI assistant that collects content from sources
you subscribe to (YouTube channels in v1), summarizes it, and lets you chat about the
corpus through a web UI. Designed to run locally on your own machine.

## v1 features

- **Config** — pydantic-settings driven, graceful degradation with zero env vars
- **SQLite schema** — full v1 schema with WAL mode, FTS5 search, hand-rolled migrations
- **YouTube collector** — discovers new videos via the YouTube Data API, filters by duration, fetches transcripts via Apify, and records everything in the DB
- **Summarization pipeline** — pydantic-ai agent turns transcripts into 1–2 paragraph prose summaries with 3–7 topic tags; summaries stored as markdown with YAML front matter
- **Scheduler** — APScheduler 3.x runs the full discover → fetch → summarize pipeline per source on a cron schedule, with bounded auto-retry and graceful degradation
- **Web UI** — full browsing + chat surface:
  - `/` — topic-grouped dashboard (last 7 days + "today" callout) with thumbnails, summaries, topic chips, and YouTube links
  - `/items` — browse all items with FTS5 search (HTMX keyup)
  - `/items/{id}` — item detail with summary, collapsible transcript, and metadata
  - `/topics` — all topics sorted by last activity with item/rollup counts
  - `/topics/{slug}` — topic detail with rollups above and items below
  - `/sources` — add, enable, disable, and delete YouTube channel sources
  - `/sources/{id}` — source detail with items list and collection run history
  - `/runs` — collection run log with status, counts, and errors
  - `/chat` — conversational interface with SSE streaming, tool-call indicators, and persisted history
  - `/rollups` — browse agent-authored rollup documents with topic filtering
  - `/rollups/{id}` — rollup detail with body text and cited source items
- **Chat agent** — pydantic-ai agent with 17 tools (11 read, 3 write, 3 source-management) for querying the corpus, creating rollup syntheses, tagging items, and managing sources — all with streaming responses
- **Telegram surface** — same chat agent reachable from a phone via a Telegram bot. Webhook mode, ~750 ms throttled edit-in-place streaming, markdown → Telegram-HTML conversion, paragraph-aware splitting under Telegram's 4096-char limit, single-user guard, `/start` welcome, and a phone-friendly prompt addendum for shorter replies
- **Manual retry** — retry button on item detail pages for items that failed automatic recovery, resetting status and clearing the last-error metadata so the next scheduled run picks them up
- **Entry point** — `python -m artimesone` starts FastAPI + scheduler in one process

## v1.1 — Pass, libraries, projects

Triage and curation layered onto v1 without touching the collectors:

- **Pass** — a one-click dismissal on any item card. Passed items disappear from the dashboard, `/items`, topic views, source views, and default chat-agent queries. Nothing is deleted — `/items?show=passed` surfaces dismissed items again with an un-pass affordance.
- **Libraries** — exclusive consumption buckets (e.g. "Entertainment", "Education"). An item belongs to at most one library at a time; moving it between libraries is atomic. Filing an item into a library hides it from the main feed. Available at `/libraries` and `/libraries/{id}`.
- **Projects** — non-exclusive research collections (e.g. "AI Skills") that stage items for later synthesis into a rollup. Project membership does **not** hide the item — projects are active work you still want to see in the feed. Available at `/projects` and `/projects/{id}`.
- **Chat agent** — read-only access to lists via `get_lists` and `get_list`. The agent can summarize what's in a project but cannot create lists or add items — list management remains user-only through the web UI.

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
| `ARTIMESONE_TELEGRAM_BOT_TOKEN` | Telegram chat surface | Create a bot via BotFather on Telegram |
| `ARTIMESONE_TELEGRAM_ALLOWED_CHAT_ID` | Telegram single-user guard | Your Telegram user/chat ID (any other sender is silently rejected) |

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

# Tests (all offline, no live API calls)
uv run python -m pytest -v

# Fast smoke test — v1 end-to-end in under 2 seconds
uv run python -m pytest tests/test_smoke.py -v
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
  agents/              # pydantic-ai agents (summarizer, chat agent + 17 tools)
  lists.py             # Shared data layer for libraries + projects
  pipeline/            # Processing pipelines (summarize: transcript → summary + tags)
  telegram/            # Telegram chat surface (webhook, streaming, markdown → HTML)
  web/
    filters.py         # Jinja2 template filters (duration, dates, text)
    routes/            # FastAPI routers (dashboard, items, topics, sources, runs, chat, rollups, libraries, projects)
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
| `ARTIMESONE_SUMMARY_MODEL` | `openai:gpt-4o-mini` | Model for summarization pipeline |
| `ARTIMESONE_CHAT_MODEL` | `openai:gpt-4o` | Model for the chat agent |
| `ARTIMESONE_MAX_VIDEO_DURATION_MINUTES` | `60` | Skip videos longer than this |
