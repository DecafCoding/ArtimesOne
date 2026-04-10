# ArtimesOne

ArtimesOne is a single-user personal AI assistant that collects content from sources
you subscribe to (YouTube channels first, more to come), summarizes it, and lets you
chat about the corpus through a web UI. Designed to run locally on your own machine.

## Quickstart

```bash
pip install -e .[dev]
python -m artimesone
```

Then open <http://127.0.0.1:8000/>.

The app boots with zero environment variables set; missing API keys simply disable the
features they unlock. To enable the YouTube collector, set `ARTIMESONE_YOUTUBE_API_KEY`
in your environment or in a local `.env` file (see [.env.example](./.env.example)).

## Project status

Phase 1 (Foundation): runnable skeleton — config, SQLite schema + migrations, scheduler,
collector framework, YouTube discovery, and a minimal web UI for managing sources.
Phase 2 onward adds Apify transcripts, summaries, browsing UI, chat agent, and Telegram.
