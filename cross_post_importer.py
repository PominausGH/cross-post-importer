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
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Config ──────────────────────────────────────────────────────────────
RSS_FEED_URLS = [
    "https://ghost.daintytrading.com/feed-today.xml",
    "https://ghost.daintytrading.com/feed.xml",
]
SUBSTACK_FEED_URL = "https://ghost.daintytrading.com/feed.xml"
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
    """Fetch all RSS feeds and return combined posts (deduplicated by URL)."""
    seen_urls = set()
    posts = []
    for feed_url in RSS_FEED_URLS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            url = entry.get("link", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            posts.append({
                "title": entry.get("title", "Untitled"),
                "url": url,
                "published": entry.get("published", ""),
            })
    return posts


def normalize_title(title: str) -> str:
    """Normalize title for comparison: lowercase, strip suffix, remove punctuation."""
    t = title.lower().replace("\u200a", " ")
    t = t.replace(" - a developer's story", "").replace("— a developer's story", "")
    t = t.replace("-", " ")  # hyphens to spaces before stripping punctuation
    t = re.sub(r"[^a-z0-9\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def get_medium_published_titles(context) -> set:
    """Scrape Medium published stories page to get existing titles for dedup."""
    page = context.new_page()
    titles = set()
    try:
        page.goto("https://medium.com/me/stories?tab=posts-published", timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(3)
        # Scroll to load all stories (Medium uses infinite scroll)
        prev_count = 0
        for _ in range(20):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
            count = page.evaluate("() => document.querySelectorAll('h2, h3').length")
            if count == prev_count:
                break
            prev_count = count
        raw_titles = page.evaluate("""() => {
            const headings = document.querySelectorAll("h2, h3");
            return Array.from(headings).map(h => h.textContent.trim().toLowerCase());
        }""")
        titles = set(normalize_title(t) for t in raw_titles if t)
        log(f"  Medium: Found {len(titles)} published stories for dedup.")
    except Exception as e:
        log(f"  Medium: Could not fetch published stories for dedup: {e}")
    finally:
        page.close()
    return titles


def get_substack_published_titles(context) -> set:
    """Scrape all pages of Substack published posts for dedup."""
    page = context.new_page()
    titles = set()
    try:
        page.goto("https://pominaus.substack.com/publish/posts/published", timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(5)

        while True:
            # Scrape titles from post links only (filter by href pattern)
            raw_titles = page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="/publish/posts/detail/"]');
                return Array.from(links)
                    .map(a => a.textContent.trim())
                    .filter(t => t.length > 5)
                    .map(t => {
                        const match = t.match(/^(.+?)\\d{1,2}\\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\u2022/);
                        return match ? match[1].trim() : t;
                    });
            }""")
            for t in raw_titles:
                if t:
                    titles.add(normalize_title(t))

            # Check if "Chevron Right" (next page) button exists and is enabled
            next_disabled = page.evaluate("""() => {
                const btn = document.querySelector('button[aria-label="Chevron Right"]');
                return !btn || btn.disabled;
            }""")
            if next_disabled:
                break

            # Click next page and wait for content to load
            page.click('button[aria-label="Chevron Right"]')
            time.sleep(3)

        log(f"  Substack: Found {len(titles)} published posts for dedup.")
    except Exception as e:
        log(f"  Substack: Could not fetch published posts for dedup: {e}")
    finally:
        page.close()
    return titles


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


def get_article_topics(url: str) -> list[str]:
    """Fetch meta keywords from article page via HTTP (no browser needed). Max 5 topics."""
    import urllib.request
    import html.parser
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            # Read just the head section (first 10KB is enough for meta tags)
            raw = resp.read(10240).decode("utf-8", errors="ignore")
        # Parse out meta keywords
        import re
        match = re.search(r'<meta\s+name=["\']keywords["\']\s+content=["\']([^"\']+)["\']', raw, re.IGNORECASE)
        if not match:
            match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+name=["\']keywords["\']', raw, re.IGNORECASE)
        if match:
            topics = [k.strip() for k in match.group(1).split(",") if k.strip()]
            return topics[:5]
    except Exception as e:
        log(f"  Could not fetch topics from {url}: {e}")
    return []


def generate_share_text(title: str, topics: list[str], short: bool = False) -> str:
    """Generate a witty sharing note for social media based on article title and topics.

    Args:
        short: If True, keep under 200 chars (for Twitter/X where URL eats into 280 limit).
    """
    clean = re.sub(r"\s*[-—]\s*A Developer'?s Story\s*$", "", title).strip()
    topic_str = topics[0] if topics else "tech"

    if short:
        # Keep very short for Twitter/X (280 char limit, URL takes ~23)
        max_title = 50
        if len(clean) > max_title:
            clean = clean[:max_title].rsplit(" ", 1)[0] + "..."
        templates = [
            f'{clean}',
            f'New: {clean}',
            f'{clean} — thoughts?',
            f'{clean} — worth a read',
        ]
    else:
        templates = [
            f'New read: "{clean}" — a deep dive into {topic_str}. Worth your time.',
            f'Just published: "{clean}" — thoughts welcome!',
            f'If {topic_str} is your thing, this one\'s for you: "{clean}"',
            f'New article dropped: "{clean}" — let me know what you think',
            f'"{clean}" — fresh take on {topic_str}. Give it a read.',
            f'"{clean}" just went live. Worth checking out.',
            f'Hot off the press: "{clean}" — {topic_str} and then some.',
            f'Wrote about {topic_str}: "{clean}" — check it out!',
        ]
    return random.choice(templates)


def share_to_social(page, title: str, topics: list[str], article_url: str = "") -> None:
    """Click social share buttons on current page and post witty notes in popups.

    Works on both Medium and Substack post-publish/share pages.
    Finds Twitter/X, LinkedIn, and Facebook share buttons, opens each
    popup, fills a witty note, and clicks share.
    """
    share_text = generate_share_text(title, topics)
    log(f"    Share text: {share_text[:80]}...")
    time.sleep(2)

    social_buttons = {
        "Twitter/X": [
            'a[href*="twitter.com/intent"]', 'a[href*="x.com/intent"]',
            'button[aria-label*="Twitter" i]', 'button[aria-label*="tweet" i]',
            'button:has-text("Twitter")', 'button:has-text("Tweet")',
            'button:has-text("𝕏")', '[data-testid*="twitter" i]',
        ],
        "LinkedIn": [
            'a[href*="linkedin.com/share"]', 'a[href*="linkedin.com/sharing"]',
            'button[aria-label*="LinkedIn" i]', 'button:has-text("LinkedIn")',
            '[data-testid*="linkedin" i]',
        ],
        "Facebook": [
            'a[href*="facebook.com/sharer"]', 'a[href*="facebook.com/share"]',
            'button[aria-label*="Facebook" i]', 'button:has-text("Facebook")',
            '[data-testid*="facebook" i]',
        ],
    }

    for platform, selectors in social_buttons.items():
        btn = None
        for sel in selectors:
            btn = page.query_selector(sel)
            if btn:
                break
        if not btn:
            log(f"    Share: {platform} button not found — skipping.")
            continue

        try:
            # Click share button — expect a popup/new tab to open
            with page.context.expect_page(timeout=10_000) as popup_info:
                btn.click()
            popup = popup_info.value
            popup.wait_for_load_state("domcontentloaded")
            time.sleep(3)

            is_twitter = "x.com" in popup.url or "twitter.com" in popup.url
            is_linkedin = "linkedin.com" in popup.url

            platform_text = (
                generate_share_text(title, topics, short=True)
                if is_twitter else share_text
            )
            # LinkedIn uses a contenteditable div with role="textbox"
            if is_linkedin:
                text_area = popup.query_selector('[role="textbox"][contenteditable="true"]')
            else:
                text_area = (
                    popup.query_selector('textarea') or
                    popup.query_selector('[role="textbox"]') or
                    popup.query_selector('[contenteditable="true"]')
                )
            if text_area:
                if is_twitter:
                    # Clear pre-filled intent text and type short text + URL
                    text_area.click()
                    popup.keyboard.press("Control+a")
                    tweet = platform_text + ("\n" + article_url if article_url else "")
                    popup.keyboard.type(tweet, delay=10)
                else:
                    text_area.click()
                    popup.keyboard.type(platform_text)
                log(f"    Share: Set text for {platform}")

            # Find and click the post/share/tweet button
            time.sleep(1)
            submit = None
            for sel in [
                'button[data-testid="tweetButton"]',
                'input[type="submit"]',
                'button:has-text("Post")',
                'button:has-text("Tweet")',
                'button:has-text("Share")',
                'button[type="submit"]',
            ]:
                submit = popup.query_selector(sel)
                if submit:
                    break

            if submit:
                disabled = submit.evaluate(
                    "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                )
                if disabled:
                    log(f"    Share: {platform} submit disabled (text may be too long)")
                else:
                    submit.evaluate("el => el.click()")
                    time.sleep(3)
                    log(f"    Share: ✓ Shared to {platform}")
            else:
                log(f"    Share: No submit button found for {platform}")
                try:
                    popup.screenshot(path=str(BASE_DIR / f"share_{platform.lower().replace('/', '_')}_debug.png"))
                except Exception:
                    pass

            if not popup.is_closed():
                popup.close()

        except PlaywrightTimeout:
            log(f"    Share: {platform} popup didn't open — may need login.")
        except Exception as e:
            log(f"    Share: {platform} error: {e}")


def share_substack_posts(context, posts: list[dict]) -> None:
    """Share Substack posts via the post-publish share page.

    Flow per post: editor → Continue → Update now → share page
    Share page has: Substack Note, Twitter, Facebook, LinkedIn, Instagram buttons.
    """
    if not posts:
        return

    log(f"  Substack: Sharing {len(posts)} post(s) to social platforms...")
    page = context.new_page()
    try:
        # Get all post IDs from dashboard (with pagination)
        page.goto("https://pominaus.substack.com/publish/posts/published",
                   timeout=NAV_TIMEOUT, wait_until="networkidle")
        time.sleep(3)

        all_dashboard_posts = []
        while True:
            post_data = page.evaluate("""() => {
                const links = document.querySelectorAll('a[href*="/publish/posts/detail/"]');
                return Array.from(links).map(a => ({
                    id: (a.href.match(/detail\\/(\\d+)/) || [])[1] || '',
                    title: a.textContent.trim(),
                })).filter(p => p.id);
            }""")
            all_dashboard_posts.extend(post_data)

            next_disabled = page.evaluate("""() => {
                const btn = document.querySelector('button[aria-label="Chevron Right"]');
                return !btn || btn.disabled;
            }""")
            if next_disabled:
                break
            page.click('button[aria-label="Chevron Right"]')
            time.sleep(3)

        log(f"  Substack: Found {len(all_dashboard_posts)} posts in dashboard.")

        for post in posts:
            norm = normalize_title(post["title"])
            norm_prefix = " ".join(norm.split()[:5])

            matched = None
            for dp in all_dashboard_posts:
                dp_norm = normalize_title(dp["title"])
                if dp_norm.startswith(norm_prefix) or norm in dp_norm or dp_norm in norm:
                    matched = dp
                    break

            if not matched:
                log(f"    Share: Could not find post: {post['title'][:60]}")
                continue

            log(f"    Share: {post['title'][:60]}...")
            topics = get_article_topics(post["url"])
            share_text = generate_share_text(post["title"], topics)
            log(f"    Share text: {share_text[:80]}...")

            # Navigate to editor → Continue → Update now to reach share page
            page.goto(f"https://pominaus.substack.com/publish/post/{matched['id']}",
                       timeout=NAV_TIMEOUT, wait_until="networkidle")
            time.sleep(3)

            try:
                page.click('button:has-text("Continue")', timeout=5_000)
                time.sleep(3)
            except PlaywrightTimeout:
                log(f"    Share: No Continue button — skipping.")
                continue

            try:
                page.click('button:has-text("Update now")', timeout=5_000)
                time.sleep(5)
            except PlaywrightTimeout:
                log(f"    Share: No Update button — skipping.")
                continue

            # -- Share page: "Your post is live!" --

            # 1) Click social share buttons FIRST (Note navigates away)
            #    Buttons: Twitter, Facebook, LinkedIn
            page.evaluate('window.scrollTo(0, 500)')
            time.sleep(1)

            for platform in ["Twitter", "Facebook", "LinkedIn"]:
                # Close any leftover popups from previous platform
                for p in page.context.pages:
                    if p != page and not p.is_closed():
                        try:
                            p.close()
                        except Exception:
                            pass
                time.sleep(1)
                try:
                    with page.expect_popup(timeout=10_000) as popup_info:
                        page.evaluate(f"""() => {{
                            const btns = document.querySelectorAll('button');
                            for (const b of btns) {{
                                if (b.textContent.trim() === '{platform}') {{
                                    b.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}""")
                    popup = popup_info.value
                    popup.wait_for_load_state("domcontentloaded")
                    time.sleep(3)

                    is_twitter = "x.com" in popup.url or "twitter.com" in popup.url
                    is_linkedin = "linkedin.com" in popup.url

                    platform_text = (
                        generate_share_text(post["title"], topics, short=True)
                        if is_twitter else share_text
                    )
                    # LinkedIn uses a contenteditable div with role="textbox"
                    if is_linkedin:
                        text_area = popup.query_selector('[role="textbox"][contenteditable="true"]')
                    else:
                        text_area = (
                            popup.query_selector('textarea') or
                            popup.query_selector('[role="textbox"]') or
                            popup.query_selector('[contenteditable="true"]')
                        )
                    if text_area:
                        if is_twitter:
                            # Clear pre-filled intent text (title + tracking URL)
                            # and type short text + clean article URL
                            text_area.click()
                            popup.keyboard.press("Control+a")
                            tweet = platform_text + "\n" + post["url"]
                            popup.keyboard.type(tweet, delay=10)
                        else:
                            text_area.click()
                            popup.keyboard.type(platform_text)
                        log(f"    Share: Set text for {platform} ({len(platform_text)} chars)")

                    # Click submit/post button
                    time.sleep(2)
                    submit = None
                    for sel in [
                        'button[data-testid="tweetButton"]',
                        'input[type="submit"]',
                        'button:has-text("Post")',
                        'button:has-text("Tweet")',
                        'button:has-text("Share")',
                        'button[type="submit"]',
                    ]:
                        submit = popup.query_selector(sel)
                        if submit:
                            break
                    if submit:
                        # Check if button is disabled (e.g. tweet too long)
                        disabled = submit.evaluate(
                            "el => el.disabled || el.getAttribute('aria-disabled') === 'true'"
                        )
                        if disabled:
                            log(f"    Share: {platform} submit disabled (text may be too long)")
                            try:
                                popup.screenshot(path=str(
                                    BASE_DIR / f"share_{platform.lower()}_debug.png"))
                            except Exception:
                                pass
                        else:
                            submit.evaluate("el => el.click()")
                            time.sleep(3)
                            log(f"    Share: ✓ Shared to {platform}")
                    else:
                        log(f"    Share: {platform} — no submit button (may need login)")
                        try:
                            popup.screenshot(path=str(
                                BASE_DIR / f"share_{platform.lower()}_debug.png"))
                        except Exception:
                            pass

                    if not popup.is_closed():
                        popup.close()

                except PlaywrightTimeout:
                    log(f"    Share: {platform} popup didn't open")
                except Exception as e:
                    log(f"    Share: {platform} error: {e}")

            # 2) Write a Substack Note LAST (clicking Create navigates away)
            page.evaluate('window.scrollTo(0, 0)')
            time.sleep(1)
            page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    if (b.textContent.includes('Share something about this post')) {
                        b.click();
                        return true;
                    }
                }
                return false;
            }""")
            time.sleep(2)
            note_editor = page.query_selector(
                '[contenteditable="true"].ProseMirror, [contenteditable="true"].tiptap'
            )
            if note_editor:
                note_editor.click()
                page.keyboard.type(share_text)
                time.sleep(1)
                page.evaluate("""() => {
                    const btns = document.querySelectorAll('button');
                    for (const b of btns) {
                        if (b.textContent.trim() === 'Create') {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }""")
                time.sleep(3)
                log(f"    Share: ✓ Posted Substack Note")
            else:
                log(f"    Share: Could not find Note editor")

            time.sleep(2)

    except Exception as e:
        log(f"  Substack share error: {e}")
    finally:
        page.close()


def import_to_medium(context, url: str, title: str = "", share: bool = True) -> bool:
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

                # Add topics scraped from article page
                topics = get_article_topics(url)
                if not topics:
                    topics = ["Technology", "Software Development"]
                tag_input = page.query_selector('.js-tagInput, [data-testid="publishTopicsInput"]')
                if tag_input:
                    for topic in topics:
                        tag_input.click()
                        page.keyboard.type(topic)
                        time.sleep(1)
                        page.keyboard.press("Enter")
                        time.sleep(0.5)
                    log(f"  Medium: Added topics: {topics}")
                else:
                    log("  Medium: Could not find topic input — skipping topics.")

                # Click "Publish and send now" or "Publish now"
                time.sleep(1)
                confirm_btn = page.query_selector('button:has-text("Publish and send now"), button:has-text("Publish now")')
                if confirm_btn:
                    confirm_btn.click()
                    log("  Medium: Published!")
                    time.sleep(5)

                    # Share to social platforms after publishing
                    if share:
                        share_to_social(page, title or "New article", topics, article_url=url)
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
               medium: bool = True, substack: bool = False, share: bool = True) -> bool:
    """Main import flow: check RSS, import new posts. Returns True if all imports succeeded."""
    imported = load_imported()
    failures = 0

    if force_url:
        posts = [{"title": "Manual Import", "url": force_url, "published": ""}]
    else:
        log(f"Fetching RSS feeds: {len(RSS_FEED_URLS)} source(s)")
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
                # Check already-published titles on Medium to avoid duplicates
                published_titles = get_medium_published_titles(context)

                for post in medium_pending:
                    url = post["url"]
                    # Skip if title already published on Medium (fuzzy: first 5 words match)
                    norm_title = normalize_title(post["title"])
                    norm_words = norm_title.split()[:5]
                    prefix = " ".join(norm_words)
                    if any(pt.startswith(prefix) or norm_title in pt or pt in norm_title for pt in published_titles):
                        log(f"Skipping (already on Medium): {post['title']}")
                        imported.setdefault("medium", []).append(url)
                        save_imported(imported)
                        continue

                    log(f"Processing: {post['title']}")
                    log(f"  URL: {url}")
                    if import_to_medium(context, url, title=post["title"], share=share):
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
            log(f"Substack: {len(substack_pending)} post(s) in feed — checking for duplicates...")
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DATA_DIR),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 900},
                )
                # Check already-published titles on Substack to avoid duplicates
                sub_published = get_substack_published_titles(context)
                actually_new = []
                for p in substack_pending:
                    norm = normalize_title(p["title"])
                    norm_words = norm.split()[:5]
                    prefix = " ".join(norm_words)
                    if any(pt.startswith(prefix) or norm in pt or pt in norm for pt in sub_published):
                        log(f"  Skipping (already on Substack): {p['title']}")
                        imported.setdefault("substack", []).append(p["url"])
                    else:
                        actually_new.append(p)

                if actually_new:
                    log(f"  Substack: {len(actually_new)} genuinely new post(s) — importing via RSS...")
                    if import_to_substack(context):
                        for p in actually_new:
                            imported.setdefault("substack", []).append(p["url"])
                        # Share newly imported posts to social platforms
                        if share:
                            share_substack_posts(context, actually_new)
                    else:
                        failures += 1
                else:
                    log("  Substack: All posts already published — skipping import.")
                save_imported(imported)
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
    parser.add_argument("--no-share", action="store_true", help="Skip social sharing after publish")
    parser.add_argument("--share-only", type=int, nargs="?", const=1, metavar="N",
                        help="Only share existing posts (no import). N = number of posts to share (default 1)")
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
    elif args.share_only is not None:
        # Share existing posts without importing
        n = args.share_only
        posts = get_new_posts()
        to_share = posts[:n]
        log(f"Share-only mode: sharing {len(to_share)} post(s)...")
        BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        if do_substack:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(BROWSER_DATA_DIR),
                    headless=args.headless,
                    args=["--disable-blink-features=AutomationControlled"],
                    viewport={"width": 1280, "height": 900},
                )
                share_substack_posts(context, to_share)
                context.close()
        if do_medium:
            log("Medium share-only not yet implemented — share happens during import.")
    else:
        ok = run_import(
            force_url=args.force,
            dry_run=args.dry_run,
            headless=args.headless,
            medium=do_medium,
            substack=do_substack,
            share=not args.no_share,
        )
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
