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
            print("[scraper] Opening Google Maps...")
            page.goto("https://www.google.com/maps", wait_until="commit", timeout=30000)
            time.sleep(5)

            for sel in ['button[aria-label="Accept all"]', 'button[aria-label="Reject all"]', 'button[jsname="b3VHJd"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(1)
                except Exception:
                    pass

            print("[scraper] Focusing search box...")
            page.evaluate("""
                () => {
                    const box = document.querySelector('#searchboxinput');
                    if (box) { box.value = ''; box.focus(); box.click(); }
                }
            """)
            time.sleep(1)

            print(f"[scraper] Typing: '{query}'")
            page.keyboard.type(query, delay=100)
            time.sleep(3)
            page.screenshot(path="debug/01_typed.png")

            print("[scraper] Clicking search button...")
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
                        print(f"[scraper] Clicked: {sel}")
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
                        first.click()
                        clicked_search = True
                except Exception:
                    pass

            if not clicked_search:
                page.keyboard.press("Enter")

            time.sleep(4)
            page.screenshot(path="debug/02_search_submitted.png")

            print("[scraper] Killing any popups...")
            for _ in range(4):
                page.keyboard.press("Escape")
                time.sleep(0.4)

            for sel in ['button[aria-label="Close"]', 'button[jsname="twnHdb"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        time.sleep(0.3)
                except Exception:
                    pass

            time.sleep(2)

            print("[scraper] Waiting for results...")
            try:
                page.wait_for_selector('div[role="feed"]', timeout=20000)
                print("[scraper] ✓ Feed found!")
            except PlaywrightTimeout:
                page.screenshot(path="debug/04_timeout.png")
                try:
                    page.wait_for_selector('div[role="article"]', timeout=10000)
                    print("[scraper] ✓ Articles found!")
                except PlaywrightTimeout:
                    print("[scraper] No results.")
                    browser.close()
                    return []

            time.sleep(2)
            page.screenshot(path="debug/04_results.png")

            print("[scraper] Scrolling results...")
            _scroll_results(page)

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
                    print(f"[scraper] [{i+1}] {name} | {row['Rating']} | Phone: '{phone}' | Addr: '{addr[:40]}'")
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

    results.sort(key=lambda x: float(x["Rating"]) if x.get("Rating") else 0, reverse=True)
    print(f"[scraper] Done. Total: {len(results)}")
    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scroll_results(page):
    feed = page.query_selector('div[role="feed"]')
    if not feed:
        return
    prev, stale = 0, 0
    for i in range(15):
        if len(page.query_selector_all('div[role="article"]')) >= 100:
            break
        feed.evaluate("el => el.scrollBy(0, 2500)")
        time.sleep(1.5)
        if page.query_selector('span.HlvSq'):
            print("[scraper] End of list.")
            break
        count = len(page.query_selector_all('div[role="article"]'))
        print(f"[scraper]   Scroll {i+1}: {count} cards")
        stale = stale + 1 if count == prev else 0
        if stale >= 3:
            break
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


def _parse_card_fields(card) -> tuple[str, str]:
    """
    Extract address and phone from the info rows inside a Google Maps card.

    Each .W4Efsd row is a blob split by · like:
      "Clothing store · Shop No 4, Sector 3 Airoli · Open · Closes 10pm · 086938 18779"
      "Restaurant · ₹200–400 · Open now"
      "Hotel · Sector 3, Plot 12 · 022 6884 6143"

    Phone detection: a segment where ≥60% of non-space chars are digits, 
    total digits ≥ 7. This handles:
      "086938 18779"   ✓
      "022 6884 6143"  ✓
      "1800 891 0001"  ✓
      "+91 98765 43210" ✓
      "Sector 3"       ✗  (has letters, <60% digits)
    """
    address = ""
    phone   = ""

    # Patterns to skip
    skip_re   = re.compile(r'^(Open|Closed|Opens|Closes|open|closed)', re.I)
    price_re  = re.compile(r'[₹$€£]')
    rating_re = re.compile(r'^\d+\.\d+\s*\(\d+')   # "4.5(1,234)"

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
                if skip_re.match(seg):
                    continue
                if price_re.search(seg):
                    continue
                if rating_re.match(seg):
                    continue

                # Count digits vs total non-space chars
                non_space = re.sub(r'\s', '', seg)
                digits    = re.sub(r'\D', '', seg)
                digit_count = len(digits)
                total_count = len(non_space) if non_space else 1
                digit_ratio = digit_count / total_count

                # Phone: mostly digits (≥55%), at least 7 digits total
                if digit_ratio >= 0.55 and digit_count >= 7:
                    if not phone:
                        phone = seg.strip()
                    continue

                # Address: has letters, long enough, not already captured
                if not address and re.search(r'[A-Za-z]', seg) and len(seg) > 5:
                    address = seg.strip()

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
            href  = a.get_attribute("href") or ""
            email = href.split("mailto:")[-1].split("?")[0].strip()
            if email:
                return email
        for a in card.query_selector_all('a'):
            href  = a.get_attribute("href") or ""
            match = email_re.search(href)
            if match:
                return match.group(0).strip()
        raw   = card.inner_text() or ""
        match = email_re.search(raw)
        if match:
            return match.group(0).strip()
    except Exception:
        pass
    return ""


def _website(card):
    try:
        el = card.query_selector('a[data-item-id*="authority"]')
        if el:
            href = el.get_attribute("href") or ""
            if href:
                return href
        for a in card.query_selector_all('a[href^="http"]'):
            href  = a.get_attribute("href") or ""
            lower = href.lower()
            if not href or 'mailto:' in lower:
                continue
            if any(b in lower for b in ["maps.google.com", "google.com/maps", "g.page", "plus.codes", "googleusercontent.com"]):
                continue
            return href
    except Exception:
        pass
    return ""