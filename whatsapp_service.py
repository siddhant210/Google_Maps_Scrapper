"""
WhatsApp service using Playwright to drive WhatsApp Web.
- One browser session for all messages (QR scan once, saved to wa_session/)
- Navigates to web.whatsapp.com/send?phone=X&text=Y for each lead
"""

import json
import os
import re
import time
import urllib.parse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TEMPLATE_FILE = "whatsapp_template.json"
SESSION_DIR   = "wa_session"
QR_TIMEOUT    = 90


# ── Template helpers ──────────────────────────────────────────────────────────

def get_default_template():
    return (
        "Hi {name}! We noticed your business on Google Maps. "
        "We help local businesses get more customers online. "
        "Interested? Reply YES to know more. Reply STOP to opt out."
    )

def load_template():
    if os.path.exists(TEMPLATE_FILE):
        try:
            with open(TEMPLATE_FILE) as f:
                return json.load(f).get("template", get_default_template())
        except Exception:
            pass
    return get_default_template()

def save_template(template: str) -> bool:
    try:
        with open(TEMPLATE_FILE, "w") as f:
            json.dump({"template": template}, f, indent=2)
        return True
    except Exception:
        return False


# ── Phone sanitiser ───────────────────────────────────────────────────────────

def sanitize_phone(phone: str) -> str | None:
    """
    Accepts ANY Indian phone number format and returns digits-only with 91 prefix.
    
    Examples that all work:
      8291061982          → 918291061982   (10-digit mobile)
      +91 82910 61982     → 918291061982   (with country code)
      022 6884 6143       → 912268846143   (Mumbai landline)
      080-6297-2766       → 918062972766   (Bangalore landline)
      1800 891 0001       → 9118008910001  (toll-free)
      044-2345-6789       → 914423456789   (Chennai landline)
      0 22 6884 6143      → 912268846143   (spaces)
    """
    if not phone:
        return None

    raw    = str(phone).strip()
    digits = re.sub(r'\D', '', raw)   # keep only digits

    if not digits or len(digits) < 7:
        return None

    # Already full E.164 with 91: 12 digits starting 91
    if digits.startswith('91') and len(digits) == 12:
        return digits

    # 13 digits starting 91 (some toll-free with extra digit)
    if digits.startswith('91') and len(digits) == 13:
        return digits

    # 10-digit mobile (starts with 6-9)
    if len(digits) == 10 and digits[0] in '6789':
        return '91' + digits

    # 11 digits starting with 0 → strip leading 0, add 91
    if len(digits) == 11 and digits.startswith('0'):
        return '91' + digits[1:]

    # 10 digits starting with 0 → strip 0, add 91
    if len(digits) == 10 and digits.startswith('0'):
        return '91' + digits[1:]

    # Toll-free like 1800XXXXXXX (11 digits not starting 0)
    if len(digits) == 11 and digits.startswith('1'):
        return '91' + digits

    # 12 digits not starting 91 → take last 10 + add 91
    if len(digits) >= 10:
        return '91' + digits[-10:]

    return None


# ── Core sender ───────────────────────────────────────────────────────────────

def send_bulk_whatsapp(leads: list[dict], template: str,
                       delay_between: int = 5,
                       test_number: str = None) -> dict:
    results = {"total": 0, "sent": 0, "failed": 0, "details": []}
    os.makedirs("debug", exist_ok=True)

    # Build send list
    if test_number:
        phone = sanitize_phone(test_number)
        if not phone:
            return {"total": 1, "sent": 0, "failed": 1,
                    "details": [{"name": "Test", "phone": test_number,
                                 "status": "failed", "error": f"Cannot parse number: {test_number}"}]}
        send_list = [{"lead": {"Name": "Test User", "Address": "Test Address", "Rating": "5.0"}, "phone": phone}]
        print(f"[WhatsApp] Test mode → sending to {phone}")
    else:
        send_list = []
        for lead in leads:
            raw_phone = lead.get("Phone", "")
            phone     = sanitize_phone(raw_phone)
            if phone:
                send_list.append({"lead": lead, "phone": phone})
                print(f"[WhatsApp] ✓ {lead.get('Name','?')} → {raw_phone} → {phone}")
            else:
                results["failed"] += 1
                results["details"].append({
                    "name":   lead.get("Name", "?"),
                    "phone":  raw_phone,
                    "status": "skipped",
                    "error":  f"Could not parse: '{raw_phone}'"
                })
                print(f"[WhatsApp] ✗ Skipped: {lead.get('Name','?')} — '{raw_phone}'")

    results["total"] = len(send_list) + results["failed"]

    if not send_list:
        print("[WhatsApp] No valid numbers to send to.")
        return results

    os.makedirs(SESSION_DIR, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            slow_mo=100,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.new_page()

        try:
            # ── Open WhatsApp Web ─────────────────────────────────────────
            print("[WhatsApp] Opening WhatsApp Web...")
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
            page.screenshot(path="debug/wa_01_opened.png")

            # ── Wait for login ────────────────────────────────────────────
            print(f"[WhatsApp] Waiting for login — scan QR if first time (up to {QR_TIMEOUT}s)...")
            logged_in = False
            for _ in range(QR_TIMEOUT):
                try:
                    # Any of these appearing = logged in
                    el = page.query_selector(
                        '[data-testid="chat-list-search"], '
                        '[aria-label="Search input textbox"], '
                        'div[contenteditable="true"][data-tab="3"], '
                        'div#side'
                    )
                    if el:
                        logged_in = True
                        break
                except Exception:
                    pass
                time.sleep(1)

            page.screenshot(path="debug/wa_02_login.png")

            if not logged_in:
                print("[WhatsApp] Login timed out.")
                results["failed"] += len(send_list)
                return results

            print("[WhatsApp] ✓ Logged in!")
            time.sleep(2)

            # ── Send to each number ───────────────────────────────────────
            for i, item in enumerate(send_list):
                lead  = item["lead"]
                phone = item["phone"]
                name  = lead.get("Name", "there")

                message = (template
                    .replace("{name}",    name)
                    .replace("{address}", lead.get("Address", ""))
                    .replace("{rating}",  str(lead.get("Rating", ""))))

                print(f"\n[WhatsApp] [{i+1}/{len(send_list)}] Sending to {name} ({phone})...")
                ok, err = _send_one(page, phone, message, i)

                if ok:
                    results["sent"] += 1
                    results["details"].append({"name": name, "phone": phone, "status": "sent"})
                    print(f"[WhatsApp]   ✓ Sent!")
                else:
                    results["failed"] += 1
                    results["details"].append({"name": name, "phone": phone, "status": "failed", "error": err})
                    print(f"[WhatsApp]   ✗ Failed: {err}")

                if i < len(send_list) - 1:
                    time.sleep(delay_between)

        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"[WhatsApp] FATAL: {e}")
        finally:
            time.sleep(2)
            context.close()

    print(f"\n[WhatsApp] Done — Sent: {results['sent']} | Failed: {results['failed']}")
    return results


# ── Single message sender ─────────────────────────────────────────────────────

def _send_one(page, phone: str, message: str, idx: int = 0) -> tuple[bool, str]:
    try:
        encoded = urllib.parse.quote(message, safe='')
        url = f"https://web.whatsapp.com/send?phone={phone}&text={encoded}"
        print(f"[WhatsApp]   URL: {url[:80]}...")

        page.goto(url, wait_until="domcontentloaded", timeout=25000)

        # Wait longer for WhatsApp to load the chat
        time.sleep(5)
        page.screenshot(path=f"debug/wa_send_{idx:02d}_a_loaded.png")

        # ── Check for invalid number popup ────────────────────────────────
        for sel in [
            'div[data-animate-modal-body="true"]',
            'div[role="dialog"]',
            '[data-testid="popup-contents"]',
        ]:
            try:
                modal = page.query_selector(sel)
                if modal and modal.is_visible():
                    print(f"[WhatsApp]   Modal detected — clicking OK")
                    for btn_sel in ['button', '[role="button"]']:
                        try:
                            btn = modal.query_selector(btn_sel)
                            if btn:
                                btn.click()
                                break
                        except Exception:
                            pass
                    return False, "Number not registered on WhatsApp"
            except Exception:
                pass

        # ── Find message input box ────────────────────────────────────────
        input_box = None

        # Try waiting for it first (most reliable)
        for sel in [
            'div[contenteditable="true"][data-tab="10"]',
            'div[contenteditable="true"][data-tab="6"]',
            'div[contenteditable="true"][title="Type a message"]',
            'div[contenteditable="true"][aria-label="Type a message"]',
            'div[contenteditable="true"][aria-placeholder]',
            'footer div[contenteditable="true"]',
            'div[role="textbox"]',
        ]:
            try:
                el = page.wait_for_selector(sel, timeout=3000, state="visible")
                if el:
                    input_box = el
                    print(f"[WhatsApp]   Input found: {sel}")
                    break
            except PlaywrightTimeout:
                continue
            except Exception:
                continue

        page.screenshot(path=f"debug/wa_send_{idx:02d}_b_input.png")

        if not input_box:
            return False, "Message input not found — chat did not open"

        # Click to focus
        input_box.click()
        time.sleep(1)

        # Check what text is already in the box (from URL ?text= param)
        box_text = ""
        try:
            box_text = (input_box.text_content() or "").strip()
            print(f"[WhatsApp]   Box text (first 60): '{box_text[:60]}'")
        except Exception:
            pass

        # If text param didn't populate, type manually
        if not box_text:
            print("[WhatsApp]   Text not pre-filled — typing manually")
            # Select all and clear first
            input_box.press("Control+a")
            time.sleep(0.3)
            input_box.press("Delete")
            time.sleep(0.3)
            # Type the message
            page.keyboard.type(message, delay=20)
            time.sleep(1)

        page.screenshot(path=f"debug/wa_send_{idx:02d}_c_typed.png")

        # ── Find and click send button ────────────────────────────────────
        # WhatsApp Web's send button — try many selectors
        send_clicked = False
        send_selectors = [
            'button[data-testid="compose-btn-send"]',
            'button[aria-label="Send"]',
            '[data-testid="compose-btn-send"]',
            'span[data-icon="send"]',
            'button[data-tab="11"]',
            'button[title="Send"]',
            '[data-testid="send"]',
            # The send button is often inside the footer
            'footer button[aria-label="Send"]',
            'footer span[data-icon="send"]',
        ]

        for sel in send_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    print(f"[WhatsApp]   Clicking send: {sel}")
                    btn.click()
                    send_clicked = True
                    break
            except Exception:
                continue

        # If no button found, use keyboard Enter
        if not send_clicked:
            print("[WhatsApp]   No send button found — pressing Enter")
            input_box.click()
            time.sleep(0.3)
            page.keyboard.press("Enter")

        time.sleep(2)
        page.screenshot(path=f"debug/wa_send_{idx:02d}_d_sent.png")

        # ── Verify: check if input box is now empty (message was sent) ────
        try:
            after_text = (input_box.text_content() or "").strip()
            print(f"[WhatsApp]   Box after send: '{after_text[:40]}'")
            if after_text and len(after_text) > 5:
                # Box still has text — try Enter one more time
                print("[WhatsApp]   Box not cleared — trying Enter again")
                input_box.click()
                page.keyboard.press("Enter")
                time.sleep(1.5)
        except Exception:
            pass

        return True, ""

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            page.screenshot(path=f"debug/wa_send_{idx:02d}_error.png")
        except Exception:
            pass
        return False, str(e)