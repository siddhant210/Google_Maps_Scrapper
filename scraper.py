"""
Google Maps scraper using Playwright — production-ready.

- HEADLESS controlled by env var BROWSER_HEADLESS (default false locally)
- SCRAPE_LIMIT controlled by env var SCRAPE_LIMIT (default 300)
- Deep scroll: keeps scrolling until limit reached or list truly ends
- logging instead of print()
"""

import os
import re
import time
import logging
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

log = logging.getLogger(__name__)

HEADLESS    = os.environ.get("BROWSER_HEADLESS", "false").lower() in ("1", "true", "yes")
SCRAPE_LIMIT = int(os.environ.get("SCRAPE_LIMIT", "300"))   # change via env if needed


def scrape_google_maps(query):
    results = []
    os.makedirs("debug", exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=50,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-notifications",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="en-US",
            permissions=[],
        )

        page = context.new_page()

        try:
            log.info("Scraper: opening Google Maps (headless=%s, limit=%d)...", HEADLESS, SCRAPE_LIMIT)
            page.goto("https://www.google.com/maps", wait_until="commit", timeout=30000)
            time.sleep(5)

            # Dismiss cookie / consent popups
            for sel in ['button[aria-label="Accept all"]', 'button[aria-label="Reject all"]',
                        'button[jsname="b3VHJd"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click(); time.sleep(1)
                except Exception:
                    pass

            log.info("Scraper: typing query %r", query)
            page.evaluate("""
                () => {
                    const box = document.querySelector('#searchboxinput');
                    if (box) { box.value = ''; box.focus(); box.click(); }
                }
            """)
            time.sleep(1)

            page.keyboard.type(query, delay=100)
            time.sleep(3)
            if not HEADLESS:
                page.screenshot(path="debug/01_typed.png")

            # Click search
            clicked_search = False
            for sel in [
                'button[aria-label="Search"]',
                '#searchbox-searchbutton',
                'button[jsaction*="search"]',
                'button.searchbox-button',
            ]:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        log.info("Scraper: clicked search %s", sel)
                        clicked_search = True
                        break
                except Exception:
                    pass

            if not clicked_search:
                try:
                    first = page.wait_for_selector(
                        'li.suggestions-list__item, div[data-index="0"], li[data-index="0"]',
                        timeout=3000
                    )
                    if first:
                        first.click(); clicked_search = True
                except Exception:
                    pass

            if not clicked_search:
                page.keyboard.press("Enter")

            time.sleep(4)

            # Dismiss any popups
            for _ in range(4):
                page.keyboard.press("Escape"); time.sleep(0.4)
            for sel in ['button[aria-label="Close"]', 'button[jsname="twnHdb"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click(); time.sleep(0.3)
                except Exception:
                    pass

            time.sleep(2)

            # Wait for results
            log.info("Scraper: waiting for results feed...")
            try:
                page.wait_for_selector('div[role="feed"]', timeout=20000)
                log.info("Scraper: feed found")
            except PlaywrightTimeout:
                try:
                    page.wait_for_selector('div[role="article"]', timeout=10000)
                    log.info("Scraper: articles found (no feed)")
                except PlaywrightTimeout:
                    log.warning("Scraper: no results found for query %r", query)
                    browser.close()
                    return []

            time.sleep(2)

            # Deep scroll to load up to SCRAPE_LIMIT cards
            log.info("Scraper: scrolling to load up to %d results...", SCRAPE_LIMIT)
            _scroll_results(page, SCRAPE_LIMIT)

            cards = page.query_selector_all('div[role="article"]')
            log.info("Scraper: scraping %d cards (limit=%d)", len(cards), SCRAPE_LIMIT)

            seen_names = set()   # deduplicate by name

            for i, card in enumerate(cards):
                try:
                    if len(results) >= SCRAPE_LIMIT:
                        log.info("Scraper: reached limit %d", SCRAPE_LIMIT)
                        break

                    name = _name(card)
                    if not name:
                        continue

                    # Skip duplicates (Google sometimes repeats cards after heavy scroll)
                    name_key = name.lower().strip()
                    if name_key in seen_names:
                        continue
                    seen_names.add(name_key)

                    addr, phone = _parse_card_fields(card)

                    row = {
                        "Name":          name,
                        "Category":      _category(card),
                        "Rating":        _rating(card),
                        "Reviews":       _reviews(card),
                        "Latest Review": _latest_review(card),
                        "Email":         _email(card),
                        "Address":       addr,
                        "Phone":         phone,
                        "Website":       _website(card),
                    }
                    results.append(row)
                    log.debug("Scraper [%d] %s | %s | phone: %s", len(results), name, row["Rating"], phone)

                except Exception as e:
                    log.warning("Scraper card %d error: %s", i, e)

        except Exception as e:
            log.exception("Scraper FATAL: %s", e)
            if not HEADLESS:
                try: page.screenshot(path="debug/fatal.png")
                except Exception: pass
        finally:
            time.sleep(2)
            browser.close()

    results.sort(key=lambda x: float(x["Rating"]) if x.get("Rating") else 0, reverse=True)
    log.info("Scraper done. Unique results: %d", len(results))
    return results


# ── Deep scroll ───────────────────────────────────────────────────────────────

def _scroll_results(page, limit: int):
    """
    Scroll the results feed until we have `limit` cards loaded or the list ends.
    Uses multiple strategies: feed.scrollBy, feed.scrollTop, and window scroll.
    Stale detection: if count doesn't grow for 5 consecutive scrolls → stop.
    """
    feed = page.query_selector('div[role="feed"]')
    if not feed:
        log.warning("Scraper: no feed element found, skipping scroll")
        return

    prev_count = 0
    stale_rounds = 0
    MAX_STALE = 5        # stop after 5 rounds with no new cards
    SCROLL_PAUSE = 2.0   # seconds between scrolls (too fast = Google throttles)
    scroll_round = 0

    while True:
        current_count = len(page.query_selector_all('div[role="article"]'))

        if current_count >= limit:
            log.info("Scraper scroll: reached %d cards (limit %d)", current_count, limit)
            break

        # Check for "end of list" marker
        if page.query_selector('span.HlvSq') or page.query_selector('div.PbZDve p.fontBodyMedium'):
            log.info("Scraper scroll: end-of-list marker detected at %d cards", current_count)
            break

        # Stale detection
        if current_count == prev_count:
            stale_rounds += 1
            if stale_rounds >= MAX_STALE:
                log.info("Scraper scroll: no new cards for %d rounds, stopping at %d", MAX_STALE, current_count)
                break
        else:
            stale_rounds = 0

        prev_count = current_count
        scroll_round += 1
        log.info("Scraper scroll round %d: %d/%d cards loaded", scroll_round, current_count, limit)

        # Strategy 1: scroll the feed element
        try:
            feed.evaluate("el => el.scrollBy(0, 3000)")
        except Exception:
            pass
        time.sleep(SCROLL_PAUSE)

        # Strategy 2: if feed went stale, also scroll via keyboard (page-down in feed)
        if stale_rounds >= 2:
            try:
                feed.click()
                for _ in range(5):
                    page.keyboard.press("PageDown")
                    time.sleep(0.3)
            except Exception:
                pass
            time.sleep(1)

        # Strategy 3: scroll feed to absolute bottom (catches lazy-load triggers)
        try:
            feed.evaluate("el => el.scrollTop = el.scrollHeight")
        except Exception:
            pass
        time.sleep(SCROLL_PAUSE)

    final_count = len(page.query_selector_all('div[role="article"]'))
    log.info("Scraper scroll done: %d cards loaded in %d rounds", final_count, scroll_round)


# ── Card field helpers (unchanged) ────────────────────────────────────────────

def _safe(card, selectors):
    for sel in selectors:
        try:
            el = card.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t: return t
        except Exception:
            pass
    return ""


def _name(card):
    try:
        link = card.query_selector("a[aria-label]")
        if link:
            v = (link.get_attribute("aria-label") or "").strip()
            if v: return v
    except Exception:
        pass
    return _safe(card, ["span.fontHeadlineSmall", "[class*='fontHeadline']"])


def _rating(card):
    try:
        el = card.query_selector('span[role="img"][aria-label]')
        if el:
            lbl = el.get_attribute("aria-label") or ""
            if "star" in lbl.lower():
                return lbl.split()[0]
    except Exception:
        pass
    return _safe(card, [".MW4etd"])


def _reviews(card):
    return _safe(card, [".UY7F9", ".e4rVHe"])


def _category(card):
    return _safe(card, [".DkEaL", "span.emkBOd"])


def _parse_card_fields(card) -> tuple[str, str]:
    address = ""
    phone   = ""

    skip_re   = re.compile(r'^(Open|Closed|Opens|Closes|open|closed)', re.I)
    price_re  = re.compile(r'[₹$€£]')
    rating_re = re.compile(r'^\d+\.\d+\s*\(\d+')

    try:
        rows = card.query_selector_all(".W4Efsd")
        for row in rows:
            raw = row.inner_text().strip()
            if not raw: continue
            segments = [s.strip() for s in raw.split('·') if s.strip()]
            for seg in segments:
                if len(seg) <= 2: continue
                if skip_re.match(seg): continue
                if price_re.search(seg): continue
                if rating_re.match(seg): continue
                non_space   = re.sub(r'\s', '', seg)
                digits      = re.sub(r'\D', '', seg)
                digit_count = len(digits)
                total_count = len(non_space) if non_space else 1
                digit_ratio = digit_count / total_count
                if digit_ratio >= 0.55 and digit_count >= 7:
                    if not phone: phone = seg.strip()
                    continue
                if not address and re.search(r'[A-Za-z]', seg) and len(seg) > 5:
                    address = seg.strip()
    except Exception:
        pass

    return address, phone


def _latest_review(card):
    try:
        text = card.inner_text() or ""
        patterns = [
            re.compile(r'\b\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago\b', re.I),
            re.compile(r'\b(?:today|yesterday)\b', re.I),
            re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b', re.I),
            re.compile(r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b'),
            re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),
        ]
        for pattern in patterns:
            match = pattern.search(text)
            if match: return match.group(0).strip()
    except Exception:
        pass
    return ""


def _email(card):
    try:
        email_re = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
        for a in card.query_selector_all('a[href^="mailto:"]'):
            href  = a.get_attribute("href") or ""
            email = href.split("mailto:")[-1].split("?")[0].strip()
            if email: return email
        for a in card.query_selector_all('a'):
            href  = a.get_attribute("href") or ""
            match = email_re.search(href)
            if match: return match.group(0).strip()
        raw   = card.inner_text() or ""
        match = email_re.search(raw)
        if match: return match.group(0).strip()
    except Exception:
        pass
    return ""


def _website(card):
    try:
        el = card.query_selector('a[data-item-id*="authority"]')
        if el:
            href = el.get_attribute("href") or ""
            if href: return href
        for a in card.query_selector_all('a[href^="http"]'):
            href  = a.get_attribute("href") or ""
            lower = href.lower()
            if not href or 'mailto:' in lower: continue
            if any(b in lower for b in ["maps.google.com", "google.com/maps", "g.page",
                                        "plus.codes", "googleusercontent.com"]):
                continue
            return href
    except Exception:
        pass
    return ""