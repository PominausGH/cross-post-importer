#!/usr/bin/env python3
"""
RSS Cross-Post Importer
Monitors signalreads.com RSS feed and auto-imports new posts to:
  - Medium (https://medium.com/p/import)
  - Substack (https://pominaus.substack.com/publish/import)

Requires: playwright, feedparser
Setup:
  pip install playwright feedparser
  playwright install chromium

First run: use --login to authenticate both platforms manually.
Subsequent runs: uses saved browser session.

Usage:
  python cross_post_importer.py --login       # First time: login to Medium + Substack
  python cross_post_importer.py               # Run import for new posts
  python cross_post_importer.py --force URL   # Force import a specific URL
  python cross_post_importer.py --dry-run     # Check RSS without importing
  python cross_post_importer.py --check       # Check if sessions are still valid
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import feedparser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ──────────────────────────────────────────────────────────────
RSS_FEED_URL = "https://signalreads.com/feed/"
MEDIUM_IMPORT_URL = "https://medium.com/p/import"
SUBSTACK_IMPORT_URL = "https://pominaus.substack.com/publish/import"

BASE_DIR = Path(__file__).parent
BROWSER_DATA_DIR = BASE_DIR / "browser_data"
IMPORTED_LOG = BASE_DIR / "imported.json"
LOG_FILE = BASE_DIR / "import_log.txt"

# Timeouts (ms)
NAV_TIMEOUT = 30_000
IMPORT_TIMEOUT = 60_000


# ── Helpers ─────────────────────────────────────────────────────────────
def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_imported() -> dict:
    if IMPORTED_LOG.exists():
        return json.loads(IMPORTED_LOG.read_text())
    return {"medium": [], "substack": []}


def save_imported(data: dict):
    IMPORTED_LOG.write_text(json.dumps(data, indent=2))


def get_new_posts() -> list[dict]:
    """Fetch RSS feed and return list of {title, url}."""
    feed = feedparser.parse(RSS_FEED_URL)
    posts = []
    for entry in feed.entries:
        posts.append({
            "title": entry.get("title", "Untitled"),
            "url": entry.get("link", ""),
            "published": entry.get("published", ""),
        })
    return posts


# ── Browser Automation ──────────────────────────────────────────────────
def get_browser_context(playwright):
    """Launch persistent browser context with saved cookies/session."""
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_DATA_DIR),
        headless=False,  # Set True for server/cron, False for login
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 900},
    )
    return context


def login_flow(playwright):
    """Interactive login — opens browsers for user to authenticate."""
    log("Starting login flow — please log in to both platforms...")
    context = get_browser_context(playwright)

    # Open Medium login
    page_medium = context.new_page()
    page_medium.goto("https://medium.com/m/signin", timeout=NAV_TIMEOUT)
    log("→ Please log in to MEDIUM in the browser window.")
    log("  Press ENTER here when done...")
    input()

    # Verify Medium session
    page_medium.goto(MEDIUM_IMPORT_URL, timeout=NAV_TIMEOUT)
    time.sleep(2)
    if "sign" in page_medium.url.lower():
        log("⚠ Medium login may not have worked. Try again.")
    else:
        log("✓ Medium login looks good.")

    # Open Substack login
    page_sub = context.new_page()
    page_sub.goto("https://substack.com/sign-in", timeout=NAV_TIMEOUT)
    log("→ Please log in to SUBSTACK in the browser window.")
    log("  Press ENTER here when done...")
    input()

    # Verify Substack session
    page_sub.goto(SUBSTACK_IMPORT_URL, timeout=NAV_TIMEOUT)
    time.sleep(2)
    log("✓ Substack login attempted. Check browser window.")

    log("Login complete. Sessions saved to browser_data/")
    log("Press ENTER to close browser...")
    input()
    context.close()


def check_sessions(headless: bool = True) -> bool:
    """Check if Medium and Substack sessions are still valid. Returns True if both are OK."""
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    medium_ok = False
    substack_ok = False

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )

        # Check Medium
        page = context.new_page()
        try:
            page.goto(MEDIUM_IMPORT_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
            if "sign" in page.url.lower():
                log("Medium: ✗ Not logged in.")
            else:
                log(f"Medium: ✓ Session valid (landed on {page.url})")
                medium_ok = True
        except Exception as e:
            log(f"Medium: ✗ Error checking session: {e}")
        finally:
            page.close()

        # Check Substack
        page = context.new_page()
        try:
            page.goto(SUBSTACK_IMPORT_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
            if "sign" in page.url.lower() and "publish" not in page.url.lower():
                log("Substack: ✗ Not logged in.")
            else:
                log(f"Substack: ✓ Session valid (landed on {page.url})")
                substack_ok = True
        except Exception as e:
            log(f"Substack: ✗ Error checking session: {e}")
        finally:
            page.close()

        context.close()

    if medium_ok and substack_ok:
        log("All sessions valid.")
    else:
        log("One or more sessions expired. Run with --login to re-authenticate.")
    return medium_ok and substack_ok


def import_to_medium(context, url: str) -> bool:
    """Import a URL to Medium via their import tool."""
    page = context.new_page()
    try:
        log(f"  Medium: Navigating to import page...")
        page.goto(MEDIUM_IMPORT_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(2)

        # Check if logged in
        if "sign" in page.url.lower():
            log("  Medium: ✗ Not logged in. Run with --login first.")
            return False

        # Find the URL input field and paste the link
        # Medium's import page has an input field for the URL
        input_selector = 'input[type="url"], input[type="text"], input[placeholder*="URL"], input[placeholder*="url"], input[placeholder*="link"]'
        try:
            input_el = page.wait_for_selector(input_selector, timeout=10_000)
            input_el.click()
            input_el.fill(url)
            log(f"  Medium: Pasted URL: {url}")
        except PlaywrightTimeout:
            # Try broader selector
            log("  Medium: Trying alternative input detection...")
            inputs = page.query_selector_all("input")
            if inputs:
                inputs[0].click()
                inputs[0].fill(url)
                log(f"  Medium: Pasted URL via fallback: {url}")
            else:
                log("  Medium: ✗ Could not find URL input field.")
                page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
                return False

        # Click the import button
        time.sleep(1)
        import_btn = page.query_selector('button:has-text("Import"), button:has-text("import")')
        if not import_btn:
            # Try finding any submit-like button
            import_btn = page.query_selector('button[type="submit"], form button')

        if import_btn:
            import_btn.click()
            log("  Medium: Clicked import button.")
            time.sleep(5)  # Wait for import to process
            log("  Medium: ✓ Import initiated.")
            return True
        else:
            log("  Medium: ✗ Could not find import button.")
            page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
            return False

    except Exception as e:
        log(f"  Medium: ✗ Error: {e}")
        try:
            page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
        except:
            pass
        return False
    finally:
        page.close()


def import_to_substack(context, url: str) -> bool:
    """Import a URL to Substack via their import tool."""
    page = context.new_page()
    try:
        log(f"  Substack: Navigating to import page...")
        page.goto(SUBSTACK_IMPORT_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(2)

        # Check if logged in
        if "sign" in page.url.lower() and "publish" not in page.url.lower():
            log("  Substack: ✗ Not logged in. Run with --login first.")
            return False

        # Find URL input field
        input_selector = 'input[type="url"], input[type="text"], input[placeholder*="URL"], input[placeholder*="url"], input[placeholder*="link"], input[placeholder*="paste"]'
        try:
            input_el = page.wait_for_selector(input_selector, timeout=10_000)
            input_el.click()
            input_el.fill(url)
            log(f"  Substack: Pasted URL: {url}")
        except PlaywrightTimeout:
            log("  Substack: Trying alternative input detection...")
            inputs = page.query_selector_all("input")
            if inputs:
                inputs[0].click()
                inputs[0].fill(url)
                log(f"  Substack: Pasted URL via fallback: {url}")
            else:
                log("  Substack: ✗ Could not find URL input field.")
                page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
                return False

        # Click import button
        time.sleep(1)
        import_btn = page.query_selector('button:has-text("Import"), button:has-text("import"), button:has-text("Upload")')
        if not import_btn:
            import_btn = page.query_selector('button[type="submit"], form button')

        if import_btn:
            import_btn.click()
            log("  Substack: Clicked import button.")
            time.sleep(5)
            log("  Substack: ✓ Import initiated.")
            return True
        else:
            log("  Substack: ✗ Could not find import button.")
            page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
            return False

    except Exception as e:
        log(f"  Substack: ✗ Error: {e}")
        try:
            page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
        except:
            pass
        return False
    finally:
        page.close()


# ── Main Flows ──────────────────────────────────────────────────────────
def run_import(force_url: str = None, dry_run: bool = False, headless: bool = True) -> bool:
    """Main import flow: check RSS, import new posts. Returns True if all imports succeeded."""
    imported = load_imported()
    failures = 0

    if force_url:
        posts = [{"title": "Manual Import", "url": force_url, "published": ""}]
    else:
        log(f"Fetching RSS feed: {RSS_FEED_URL}")
        posts = get_new_posts()
        log(f"Found {len(posts)} posts in feed.")

    # Filter to unimported posts
    new_posts = []
    for post in posts:
        url = post["url"]
        if not url:
            continue
        medium_done = url in imported.get("medium", [])
        substack_done = url in imported.get("substack", [])
        if not medium_done or not substack_done or force_url:
            new_posts.append({**post, "medium_done": medium_done, "substack_done": substack_done})

    if not new_posts:
        log("No new posts to import.")
        return True

    log(f"{len(new_posts)} post(s) to process.")

    if dry_run:
        for p in new_posts:
            status_m = "✓" if p["medium_done"] else "pending"
            status_s = "✓" if p["substack_done"] else "pending"
            log(f"  [{status_m}/{status_s}] {p['title']} — {p['url']}")
        return True

    with sync_playwright() as pw:
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )

        for post in new_posts:
            url = post["url"]
            log(f"Processing: {post['title']}")
            log(f"  URL: {url}")

            # Import to Medium
            if not post.get("medium_done") or force_url:
                if import_to_medium(context, url):
                    imported.setdefault("medium", []).append(url)
                    save_imported(imported)
                else:
                    failures += 1
                time.sleep(3)

            # Import to Substack
            if not post.get("substack_done") or force_url:
                if import_to_substack(context, url):
                    imported.setdefault("substack", []).append(url)
                    save_imported(imported)
                else:
                    failures += 1
                time.sleep(3)

        context.close()

    if failures:
        log(f"Done with {failures} failure(s).")
        return False

    log("Done.")
    return True


def main():
    parser = argparse.ArgumentParser(description="RSS Cross-Post Importer")
    parser.add_argument("--login", action="store_true", help="Interactive login flow")
    parser.add_argument("--check", action="store_true", help="Check if sessions are still valid")
    parser.add_argument("--force", type=str, help="Force import a specific URL")
    parser.add_argument("--dry-run", action="store_true", help="Check feed without importing")
    parser.add_argument("--headless", action="store_true", help="Run browser headless (for cron/server)")
    args = parser.parse_args()

    if args.login:
        with sync_playwright() as pw:
            login_flow(pw)
    elif args.check:
        ok = check_sessions(headless=args.headless)
        sys.exit(0 if ok else 1)
    else:
        ok = run_import(
            force_url=args.force,
            dry_run=args.dry_run,
            headless=args.headless,
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
