from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import time
import re
import os


def scrape_google_maps(query):
    results = []
    os.makedirs("debug", exist_ok=True)

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
            slow_mo=50,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-notifications",
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
            # ── Step 1: Open Maps ────────────────────────────────────────────
            print("[scraper] Opening Google Maps...")
            page.goto("https://www.google.com/maps", wait_until="commit", timeout=30000)
            time.sleep(5)

            # ── Step 2: Consent banner ───────────────────────────────────────
            for sel in ['button[aria-label="Accept all"]', 'button[aria-label="Reject all"]', 'button[jsname="b3VHJd"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(1)
                except Exception:
                    pass

            # ── Step 3: Focus search box via JS ──────────────────────────────
            print("[scraper] Focusing search box...")
            page.evaluate("""
                () => {
                    const box = document.querySelector('#searchboxinput');
                    if (box) { box.value = ''; box.focus(); box.click(); }
                }
            """)
            time.sleep(1)

            # ── Step 4: Type query ───────────────────────────────────────────
            print(f"[scraper] Typing: '{query}'")
            page.keyboard.type(query, delay=100)
            time.sleep(3)   # wait for autocomplete dropdown to appear
            page.screenshot(path="debug/01_typed.png")

            # ── Step 5: Click the SEARCH BUTTON (magnifier icon) ─────────────
            # From your screenshot the search suggestions appeared fine.
            # Instead of Enter (which traffic popup can intercept),
            # click the blue search button directly.
            print("[scraper] Clicking search button...")
            clicked_search = False

            # Try clicking the search icon button
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
                        print(f"[scraper] Clicked search button: {sel}")
                        clicked_search = True
                        break
                except Exception:
                    pass

            if not clicked_search:
                # Fallback: click first autocomplete suggestion
                print("[scraper] Fallback: clicking first autocomplete suggestion...")
                try:
                    # Autocomplete items are li elements in the dropdown
                    first = page.wait_for_selector(
                        'li.suggestions-list__item, div[data-index="0"], li[data-index="0"]',
                        timeout=3000
                    )
                    if first:
                        first.click()
                        clicked_search = True
                        print("[scraper] Clicked first suggestion.")
                except Exception:
                    pass

            if not clicked_search:
                # Last resort: press Enter
                print("[scraper] Last resort: pressing Enter...")
                page.keyboard.press("Enter")

            time.sleep(4)
            page.screenshot(path="debug/02_search_submitted.png")

            # ── Step 6: Kill any popup that appeared ─────────────────────────
            print("[scraper] Killing any popups...")
            for _ in range(4):
                page.keyboard.press("Escape")
                time.sleep(0.4)

            # Also try clicking close buttons
            for sel in ['button[aria-label="Close"]', 'button[jsname="twnHdb"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(0.3)
                except Exception:
                    pass

            time.sleep(2)
            page.screenshot(path="debug/03_after_popup_kill.png")

            # ── Step 7: Wait for results feed ────────────────────────────────
            print("[scraper] Waiting for results...")
            try:
                page.wait_for_selector('div[role="feed"]', timeout=20000)
                print("[scraper] ✓ Feed found!")
            except PlaywrightTimeout:
                page.screenshot(path="debug/04_timeout.png")
                print("[scraper] Trying articles fallback...")
                try:
                    page.wait_for_selector('div[role="article"]', timeout=10000)
                    print("[scraper] ✓ Articles found!")
                except PlaywrightTimeout:
                    print("[scraper] No results. Check debug/04_timeout.png")
                    browser.close()
                    return []

            time.sleep(2)
            page.screenshot(path="debug/04_results.png")

            # ── Step 8: Scroll ───────────────────────────────────────────────
            print("[scraper] Scrolling results...")
            _scroll_results(page)

            # ── Step 9: Scrape ───────────────────────────────────────────────
            cards = page.query_selector_all('div[role="article"]')
            print(f"[scraper] Scraping {len(cards)} cards...")

            for i, card in enumerate(cards):
                try:
                    if len(results) >= 100:
                        print("[scraper] Reached 100 lead limit.")
                        break
                    name = _name(card)
                    if not name:
                        continue
                    row = {
                        "Name":     name,
                        "Category": _category(card),
                        "Rating":   _rating(card),
                        "Reviews":  _reviews(card),
                        "Latest Review": _latest_review(card),
                        "Email":    _email(card),
                        "Address":  _address(card),
                        "Phone":    _phone(card),
                        "Website":  _website(card),
                    }

                    results.append(row)
                    print(f"[scraper] [{i+1}] {name} | {row['Rating']} | {row['Address']}")
                except Exception as e:
                    print(f"[scraper] Card {i} error: {e}")

        except Exception as e:
            import traceback
            print(f"[scraper] FATAL: {e}")
            traceback.print_exc()
            try:
                page.screenshot(path="debug/fatal.png")
            except Exception:
                pass
        finally:
            time.sleep(2)
            browser.close()

    # Sort by rating highest first
    results.sort(key=lambda x: float(x["Rating"]) if x.get("Rating") else 0, reverse=True)

    print(f"[scraper] Done. Total: {len(results)}")
    return results


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _scroll_results(page):
    feed = page.query_selector('div[role="feed"]')
    if not feed:
        return
    prev = 0
    stale = 0
    for i in range(15):
        cards = page.query_selector_all('div[role="article"]')
        if len(cards) >= 100:
            print("[scraper] Reached 100 card load limit during scroll.")
            break
        feed.evaluate("el => el.scrollBy(0, 2500)")
        time.sleep(1.5)
        if page.query_selector('span.HlvSq'):
            print("[scraper] End of list.")
            break
        count = len(page.query_selector_all('div[role="article"]'))
        print(f"[scraper]   Scroll {i+1}: {count} cards")
        if count == prev:
            stale += 1
            if stale >= 3:
                break
        else:
            stale = 0
        prev = count


def _safe(card, selectors):
    for sel in selectors:
        try:
            el = card.query_selector(sel)
            if el:
                t = el.inner_text().strip()
                if t:
                    return t
        except Exception:
            pass
    return ""


def _name(card):
    try:
        link = card.query_selector("a[aria-label]")
        if link:
            v = (link.get_attribute("aria-label") or "").strip()
            if v:
                return v
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


def _debug_card(card, name):
    """Print all raw row text to understand the structure."""
    try:
        rows = card.query_selector_all(".W4Efsd")
        print(f"\n[DEBUG] Card: {name}")
        for i, row in enumerate(rows):
            print(f"  W4Efsd[{i}]: {repr(row.inner_text().strip())}")
        chips = card.query_selector_all(".Io6YTe")
        for i, chip in enumerate(chips):
            print(f"  Io6YTe[{i}]: {repr(chip.inner_text().strip())}")
    except Exception as e:
        print(f"  [DEBUG ERROR] {e}")


def _parse_card_fields(card):
    address = ""
    phone   = ""

    phone_re  = re.compile(r'^(?:\+?\d[\d\s\-\(\)\.]{6,}\d)$')
    rating_re = re.compile(r'^\d+(?:\.\d+)?\(\d{1,3}(?:,\d{3})*\)$')
    price_re  = re.compile(r'[₹$€£]|^\d{2,4}\s*[-–]\s*\d{2,4}$')
    status_re = re.compile(r'^(Open|Closed|Opens|Closes)', re.I)

    try:
        rows = card.query_selector_all(".W4Efsd")
        for row in rows:
            raw = row.inner_text().strip()
            if not raw:
                continue
            segments = [s.strip() for s in raw.split('·') if s.strip()]

            for seg in segments:
                if len(seg) <= 2:
                    continue
                if status_re.match(seg):
                    continue
                if price_re.search(seg):
                    continue
                if rating_re.match(seg):
                    continue

                if phone_re.match(seg):
                    digits = re.findall(r'\d', seg)
                    if len(digits) >= 7 and not phone:
                        phone = seg
                        continue

                if not address and re.search(r'[A-Za-z]', seg) and len(seg) > 5:
                    address = seg

    except Exception:
        pass

    return address, phone


def _address(card):
    addr, _ = _parse_card_fields(card)
    return addr


def _phone(card):
    _, ph = _parse_card_fields(card)
    return ph


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
            if match:
                return match.group(0).strip()
    except Exception:
        pass
    return ""


def _email(card):
    try:
        email_re = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')

        for a in card.query_selector_all('a[href^="mailto:"]'):
            href = a.get_attribute("href") or ""
            email = href.split("mailto:")[-1].split("?")[0].strip()
            if email:
                return email

        for a in card.query_selector_all('a'):
            href = a.get_attribute("href") or ""
            match = email_re.search(href)
            if match:
                return match.group(0).strip()

        raw = card.inner_text() if hasattr(card, 'inner_text') else ""
        match = email_re.search(raw or "")
        if match:
            return match.group(0).strip()

        try:
            raw = card.content() if hasattr(card, 'content') else ""
            match = email_re.search(raw or "")
            if match:
                return match.group(0).strip()
        except Exception:
            pass
    except Exception:
        pass
    return ""


def _detail_scope(page):
    try:
        selectors = [
            '#pane',
            '[id^="pane"]',
            'div[role="main"]',
            'div.section-layout',
        ]
        for sel in selectors:
            try:
                scope = page.query_selector(sel)
                if scope:
                    return scope
            except Exception:
                pass
    except Exception:
        pass
    return page

def _website(card):
    try:
        el = card.query_selector('a[data-item-id*="authority"]')
        if el:
            href = el.get_attribute("href") or ""
            if href:
                return href

        for a in card.query_selector_all('a[href^="http"]'):
            href = a.get_attribute("href") or ""
            if not href:
                continue
            lower = href.lower()
            if 'mailto:' in lower:
                continue
            if any(block in lower for block in ["maps.google.com", "google.com/maps", "g.page", "plus.codes", "googleusercontent.com"]):
                continue
            return href

        raw = card.inner_text() if hasattr(card, 'inner_text') else ""
        if raw and 'http' in raw:
            for match in re.findall(r'https?://[^\s,\)\]\>]+', raw):
                lower = match.lower()
                if any(block in lower for block in ["maps.google.com", "google.com/maps", "g.page", "plus.codes", "googleusercontent.com"]):
                    continue
                return match
    except Exception:
        pass
    return ""