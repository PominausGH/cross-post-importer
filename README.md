# RSS Cross-Post Importer

Monitors your **signalreads.com** RSS feed and auto-imports new posts to:
- **Medium** via `https://medium.com/p/import`
- **Substack** via `https://pominaus.substack.com/publish/import`

Uses Playwright browser automation with persistent sessions.

## Setup

```bash
cd rss_cross_poster
pip install -r requirements.txt
playwright install chromium
```

## Usage

### 1. First Run — Login to both platforms
```bash
python cross_post_importer.py --login
```
This opens a browser window. Log in to Medium, press Enter, then log in to Substack, press Enter. Sessions are saved to `browser_data/`.

### 2. Check for new posts (dry run)
```bash
python cross_post_importer.py --dry-run
```

### 3. Import new posts
```bash
# With browser visible (debug/first time):
python cross_post_importer.py

# Headless (server/cron):
python cross_post_importer.py --headless
```

### 4. Force import a specific URL
```bash
python cross_post_importer.py --force "https://signalreads.com/your-post-slug/"
```

## Cron Setup (auto-run every 6 hours)
```bash
crontab -e
# Add:
0 */6 * * * cd /path/to/rss_cross_poster && python3 cross_post_importer.py --headless >> cron.log 2>&1
```

## Files
- `imported.json` — Tracks which URLs have been imported (prevents duplicates)
- `import_log.txt` — Full log of all actions
- `browser_data/` — Persistent browser session (cookies/auth)
- `*_debug.png` — Screenshots if import fails (for troubleshooting selectors)

## Notes
- Both Medium and Substack import pages may change their UI — if selectors break, check the debug screenshots and update selectors in the script
- Sessions may expire periodically — re-run `--login` if imports start failing
- The script waits between imports to avoid rate limiting
