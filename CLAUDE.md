# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

RSS Cross-Post Importer — a single-file Python CLI that monitors the signalreads.com RSS feed and auto-imports new posts to Medium and Substack using Playwright browser automation with persistent sessions.

## Setup & Commands

```bash
pip install -r requirements.txt    # playwright, feedparser
playwright install chromium

python cross_post_importer.py --login              # Interactive: login to Medium + Substack, saves session to browser_data/
python cross_post_importer.py --dry-run            # Check RSS feed without importing
python cross_post_importer.py                      # Import new posts (headed browser)
python cross_post_importer.py --headless           # Import new posts (headless, for cron)
python cross_post_importer.py --force "URL"        # Force-import a specific URL
python cross_post_importer.py --check              # Verify sessions are still valid (exit 0=ok, 1=expired)
python cross_post_importer.py --check --headless   # Same, headless
```

Exit codes: 0 on success, 1 on any import failure or expired session. Cron can use this to detect problems.

No tests or linting configured.

## Architecture

Single script (`cross_post_importer.py`) with this flow:

1. **RSS fetch** — `feedparser` pulls from `signalreads.com/feed/`, returns `{title, url, published}` list
2. **Dedup check** — compares against `imported.json` (tracks per-platform: `medium[]`, `substack[]`)
3. **Browser automation** — Playwright persistent context (`browser_data/`) navigates to each platform's import page, fills the URL input, clicks import
4. **Logging** — all actions written to `import_log.txt` and stdout

Key design details:
- Uses persistent browser context (not cookies) so sessions survive across runs
- Medium and Substack import functions use cascading selector strategies: specific selectors first, then fallback to broader `input` queries
- Debug screenshots (`*_debug.png`) are saved on import failure for troubleshooting selector breakage
- `imported.json` is saved after each successful import (not batched) to avoid re-importing on partial failures

## Runtime Files (gitignored or generated)

- `browser_data/` — Chromium user data dir with auth sessions
- `imported.json` — dedup tracker
- `import_log.txt` — action log
- `*_debug.png` — failure screenshots

## Known Fragility

The Medium and Substack import page selectors are the most likely breakpoint. When they change their UI, check debug screenshots and update selectors in `import_to_medium()` / `import_to_substack()`.
