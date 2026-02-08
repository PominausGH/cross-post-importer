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
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ──────────────────────────────────────────────────────────────
RSS_FEED_URL = "https://ghost.daintytrading.com/feed-today.xml"
SUBSTACK_FEED_URL = "https://ghost.daintytrading.com/feed-today.xml"
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
    """Fetch RSS feed and return posts from the last 24 hours."""
    feed = feedparser.parse(RSS_FEED_URL)
    cutoff = datetime.now().astimezone() - timedelta(hours=24)
    posts = []
    for entry in feed.entries:
        published_str = entry.get("published", "")
        # Filter to last 24 hours
        if published_str:
            try:
                pub_date = parsedate_to_datetime(published_str)
                if pub_date < cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # If we can't parse the date, include the post
        posts.append({
            "title": entry.get("title", "Untitled"),
            "url": entry.get("link", ""),
            "published": published_str,
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
            landed = page.url.lower()
            if "sign-in" in landed or "sign_in" in landed or "signin" in landed:
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

        # Find the URL input — Medium uses a contenteditable div, not a regular input
        input_selector = '.js-importUrl, [contenteditable][data-default-value*="yoursite"]'
        try:
            input_el = page.wait_for_selector(input_selector, timeout=10_000)
            input_el.click()
            # Clear default text and type the URL (fill() doesn't work on contenteditable)
            page.keyboard.press("Control+a")
            page.keyboard.type(url)
            log(f"  Medium: Typed URL: {url}")
        except PlaywrightTimeout:
            log("  Medium: ✗ Could not find URL input field.")
            page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
            return False

        # Click the import button
        time.sleep(1)
        import_btn = page.query_selector('button[data-action="import-url"], button:has-text("Import")')
        if not import_btn:
            import_btn = page.query_selector('button[type="submit"], form button')

        if import_btn:
            import_btn.click()
            log("  Medium: Clicked import button. Waiting for import...")

            # Wait for "See your story" button to confirm import succeeded
            time.sleep(10)  # Medium needs time to process the import
            try:
                see_story = page.wait_for_selector('button:has-text("See your story")', timeout=IMPORT_TIMEOUT)
                see_story.click()
                log("  Medium: Clicked 'See your story'.")
                time.sleep(8)
            except PlaywrightTimeout:
                log("  Medium: 'See your story' not found — import may still be processing.")
                page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
                return True

            # Close any remaining overlay
            page.keyboard.press("Escape")
            time.sleep(2)

            # Now on the draft editor — click Publish to open publish dialog
            try:
                publish_btn = page.wait_for_selector('button:has-text("Publish")', timeout=10_000)
                publish_btn.click()
                log("  Medium: Opened publish dialog.")
                time.sleep(3)

                # Add topics — Medium uses a contenteditable div with class js-tagInput
                tag_input = page.query_selector('.js-tagInput, [data-testid="publishTopicsInput"]')
                if tag_input:
                    for topic in ["Technology", "AI", "Software Development"]:
                        tag_input.click()
                        page.keyboard.type(topic)
                        time.sleep(1)
                        page.keyboard.press("Enter")
                        time.sleep(0.5)
                    log("  Medium: Added topics.")
                else:
                    log("  Medium: Could not find topic input — skipping topics.")

                # Click "Publish and send now" or "Publish now"
                time.sleep(1)
                confirm_btn = page.query_selector('button:has-text("Publish and send now"), button:has-text("Publish now")')
                if confirm_btn:
                    confirm_btn.click()
                    log("  Medium: Published!")
                    time.sleep(5)
                else:
                    log("  Medium: Could not find final publish button.")
            except PlaywrightTimeout:
                log("  Medium: Could not find publish button on draft page.")

            page.screenshot(path=str(BASE_DIR / "medium_debug.png"))
            log("  Medium: ✓ Import complete (screenshot saved for verification).")
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


def import_to_substack(context) -> bool:
    """Import RSS feed to Substack via their import tool (imports all posts at once)."""
    page = context.new_page()
    try:
        log(f"  Substack: Navigating to import page...")
        page.goto(SUBSTACK_IMPORT_URL, timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(2)

        # Check if logged in
        landed = page.url.lower()
        if "sign-in" in landed or "sign_in" in landed or "signin" in landed:
            log("  Substack: ✗ Not logged in. Run with --login first.")
            return False

        # Find URL input field and paste the RSS feed URL
        input_selector = 'input[type="url"], input[type="text"], input[placeholder*="URL"], input[placeholder*="url"], input[placeholder*="link"], input[placeholder*="paste"]'
        try:
            input_el = page.wait_for_selector(input_selector, timeout=10_000)
            input_el.click()
            input_el.fill(SUBSTACK_FEED_URL)
            log(f"  Substack: Pasted site URL: {SUBSTACK_FEED_URL}")
        except PlaywrightTimeout:
            log("  Substack: Trying alternative input detection...")
            inputs = page.query_selector_all("input")
            if inputs:
                inputs[0].click()
                inputs[0].fill(SUBSTACK_FEED_URL)
                log(f"  Substack: Pasted site URL via fallback: {SUBSTACK_FEED_URL}")
            else:
                log("  Substack: ✗ Could not find URL input field.")
                page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
                return False

        # Step 1: Wait for "Get started" button to enable, then click it
        time.sleep(1)
        get_started_btn = page.query_selector('button:has-text("Get started")')
        if not get_started_btn:
            get_started_btn = page.query_selector('button[type="submit"], form button')

        if not get_started_btn:
            log("  Substack: ✗ Could not find 'Get started' button.")
            page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
            return False

        # Wait for button to become enabled (Substack validates URL async)
        for _ in range(10):
            time.sleep(1)
            if not page.evaluate('(el) => el.disabled', get_started_btn):
                break
        else:
            log("  Substack: ✗ 'Get started' button never enabled — URL may be invalid.")
            page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
            return False

        get_started_btn.click()
        log("  Substack: Clicked 'Get started' — waiting for feed to load...")

        # Step 2: Wait for the "Import" button to appear (Substack parses the feed)
        try:
            import_btn = page.wait_for_selector('button:has-text("Import")', timeout=30_000)
        except PlaywrightTimeout:
            log("  Substack: ✗ Feed loaded but 'Import' button never appeared.")
            page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
            return False

        import_btn.click()
        log("  Substack: Clicked 'Import' button.")

        # Step 3: Handle "Confirm Ownership" dialog if it appears
        try:
            page.wait_for_selector('text="Confirm Ownership"', timeout=10_000)
            log("  Substack: Ownership dialog appeared.")
            # Substack uses a custom <button role="checkbox"> element
            checkbox = page.query_selector('button[role="checkbox"][aria-label*="Yes, this is my publication"]')
            if not checkbox:
                checkbox = page.query_selector('button[role="checkbox"]')
            if checkbox:
                checkbox.click()
                log("  Substack: Ticked ownership checkbox.")
            else:
                log("  Substack: ✗ Could not find ownership checkbox.")
            time.sleep(1)
            # Wait for Next button to enable and click it
            next_btn = page.query_selector('button:has-text("Next")')
            if next_btn:
                for _ in range(5):
                    time.sleep(1)
                    if not page.evaluate('(el) => el.disabled', next_btn):
                        break
                next_btn.click()
                log("  Substack: Clicked 'Next'.")
                time.sleep(10)  # Wait for import to process
        except PlaywrightTimeout:
            log("  Substack: No ownership dialog — continuing.")

        time.sleep(10)
        page.screenshot(path=str(BASE_DIR / "substack_debug.png"))
        log("  Substack: ✓ Import initiated (screenshot saved for verification).")
        return True

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
def run_import(force_url: str = None, dry_run: bool = False, headless: bool = True,
               medium: bool = True, substack: bool = False) -> bool:
    """Main import flow: check RSS, import new posts. Returns True if all imports succeeded."""
    imported = load_imported()
    failures = 0

    if force_url:
        posts = [{"title": "Manual Import", "url": force_url, "published": ""}]
    else:
        log(f"Fetching RSS feed: {RSS_FEED_URL}")
        posts = get_new_posts()
        log(f"Found {len(posts)} posts in feed.")

    # Filter to unimported posts (only check enabled platforms)
    new_posts = []
    for post in posts:
        url = post["url"]
        if not url:
            continue
        medium_done = url in imported.get("medium", [])
        substack_done = url in imported.get("substack", [])
        needs_medium = medium and not medium_done
        needs_substack = substack and not substack_done
        if needs_medium or needs_substack or force_url:
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

    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Import each post to Medium individually
    if medium:
        medium_pending = [p for p in new_posts if not p.get("medium_done") or force_url]
        if medium_pending:
            log(f"Medium: {len(medium_pending)} post(s) to import.")
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DATA_DIR),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 900},
                )
                for post in medium_pending:
                    url = post["url"]
                    log(f"Processing: {post['title']}")
                    log(f"  URL: {url}")
                    if import_to_medium(context, url):
                        imported.setdefault("medium", []).append(url)
                        save_imported(imported)
                    else:
                        failures += 1
                    time.sleep(3)
                context.close()

    # Import to Substack once via RSS feed (separate browser context)
    if substack:
        substack_pending = [p for p in new_posts if not p.get("substack_done") or force_url]
        if substack_pending:
            log(f"Substack: Importing {len(substack_pending)} post(s) via RSS feed...")
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DATA_DIR),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 900},
                )
                if import_to_substack(context):
                    for p in substack_pending:
                        imported.setdefault("substack", []).append(p["url"])
                    save_imported(imported)
                else:
                    failures += 1
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
    parser.add_argument("--medium-only", action="store_true", help="Only import to Medium")
    parser.add_argument("--substack-only", action="store_true", help="Only import to Substack")
    args = parser.parse_args()

    # Default: both platforms. Use --medium-only or --substack-only to restrict.
    if args.medium_only:
        do_medium, do_substack = True, False
    elif args.substack_only:
        do_medium, do_substack = False, True
    else:
        do_medium, do_substack = True, True

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
            medium=do_medium,
            substack=do_substack,
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
