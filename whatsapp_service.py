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
QR_TIMEOUT    = 90   # seconds to wait for QR scan


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
    Convert any Indian phone format to E.164 digits (no +).
    
    Handles all these formats:
      8291061982          → 918291061982
      +91 82910 61982     → 918291061982
      091-82910-61982     → 918291061982
      022 6884 6143       → 912268846143  (landline, 11 digits with 0)
      080 6297 2766       → 918062972766  (landline)
      1800 891 0001       → 918008910001  (toll-free, keep as-is with 91 prefix)
      +918291061982       → 918291061982
    """
    if not phone:
        return None

    # Strip everything except digits and leading +
    raw = phone.strip()
    has_plus = raw.startswith('+')
    digits = re.sub(r'\D', '', raw)

    if not digits:
        return None

    # Already has country code +91 → 12 digits starting with 91
    if has_plus and digits.startswith('91') and len(digits) == 12:
        return digits

    # 12 digits starting with 91 (no plus)
    if digits.startswith('91') and len(digits) == 12:
        return digits

    # 10 digits — mobile number, add 91
    if len(digits) == 10 and digits[0] in '6789':
        return '91' + digits

    # 11 digits starting with 0 — strip leading 0, add 91
    if len(digits) == 11 and digits.startswith('0'):
        return '91' + digits[1:]

    # 10 digits starting with 0 (some formats) — strip 0, add 91
    if len(digits) == 10 and digits.startswith('0'):
        return '91' + digits[1:]

    # Toll-free / other 10+ digit numbers
    if len(digits) >= 10:
        # If it doesn't start with 91, add it
        if not digits.startswith('91'):
            return '91' + digits[-10:]  # take last 10 digits
        return digits

    return None


# ── Core sender ───────────────────────────────────────────────────────────────

def send_bulk_whatsapp(leads: list[dict], template: str,
                       delay_between: int = 5,
                       test_number: str = None) -> dict:
    """
    Send WhatsApp messages.
    If test_number is provided, sends ONLY to that number (ignores leads).
    Otherwise sends to all leads with valid phone numbers.
    """
    results = {"total": 0, "sent": 0, "failed": 0, "details": []}

    # Build send list
    if test_number:
        phone = sanitize_phone(test_number)
        if not phone:
            return {"total": 1, "sent": 0, "failed": 1,
                    "details": [{"name": "Test", "phone": test_number,
                                 "status": "failed", "error": "Invalid test number format"}]}
        send_list = [{"lead": {"Name": "Test", "Address": "", "Rating": ""}, "phone": phone}]
    else:
        send_list = []
        for lead in leads:
            phone = sanitize_phone(lead.get("Phone", ""))
            if phone:
                send_list.append({"lead": lead, "phone": phone})
            else:
                results["failed"] += 1
                results["details"].append({
                    "name":   lead.get("Name", "?"),
                    "phone":  lead.get("Phone", ""),
                    "status": "skipped",
                    "error":  f"Could not parse: '{lead.get('Phone', '')}'"
                })
                print(f"[WhatsApp] Skipped (bad phone): {lead.get('Name')} — '{lead.get('Phone', '')}'")

    results["total"] = len(send_list) + results["failed"]

    if not send_list:
        print("[WhatsApp] No valid phone numbers found in leads.")
        return results

    os.makedirs(SESSION_DIR, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            slow_mo=150,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        page = context.new_page()

        try:
            # Open WhatsApp Web
            print("[WhatsApp] Opening WhatsApp Web...")
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30000)

            # Wait for login
            print(f"[WhatsApp] Waiting for login (up to {QR_TIMEOUT}s — scan QR if first time)...")
            try:
                page.wait_for_selector(
                    '[data-testid="chat-list-search"], '
                    'div[contenteditable="true"][data-tab="3"], '
                    'div[aria-label="Search input textbox"]',
                    timeout=QR_TIMEOUT * 1000
                )
                print("[WhatsApp] ✓ Logged in!")
            except PlaywrightTimeout:
                results["failed"] += len(send_list)
                results["details"].append({"status": "failed", "error": "WhatsApp login timed out"})
                return results

            time.sleep(3)

            # Send to each
            for i, item in enumerate(send_list):
                lead  = item["lead"]
                phone = item["phone"]
                name  = lead.get("Name", "there")

                message = (template
                    .replace("{name}",    name)
                    .replace("{address}", lead.get("Address", ""))
                    .replace("{rating}",  str(lead.get("Rating", ""))))

                print(f"[WhatsApp] [{i+1}/{len(send_list)}] → {name} ({phone})")
                ok, err = _send_one(page, phone, message)

                if ok:
                    results["sent"] += 1
                    results["details"].append({"name": name, "phone": phone, "status": "sent"})
                    print(f"[WhatsApp]   ✓ Sent")
                else:
                    results["failed"] += 1
                    results["details"].append({"name": name, "phone": phone, "status": "failed", "error": err})
                    print(f"[WhatsApp]   ✗ Failed: {err}")

                if i < len(send_list) - 1:
                    time.sleep(delay_between)

        except Exception as e:
            import traceback; traceback.print_exc()
        finally:
            time.sleep(2)
            context.close()

    print(f"[WhatsApp] Done — Sent: {results['sent']} | Failed: {results['failed']}")
    return results


def _send_one(page, phone: str, message: str) -> tuple[bool, str]:
    try:
        encoded = urllib.parse.quote(message)
        url = f"https://web.whatsapp.com/send?phone={phone}&text={encoded}"
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # Wait for message input
        try:
            input_box = page.wait_for_selector(
                'div[contenteditable="true"][data-tab="10"], '
                'div[contenteditable="true"][data-tab="6"], '
                'footer div[contenteditable="true"]',
                timeout=15000
            )
        except PlaywrightTimeout:
            # Check for invalid number popup
            for btn_sel in [
                'div[data-animate-modal-body="true"] button',
                'button[data-animate-modal-popup="true"]',
                'div[role="dialog"] button',
            ]:
                try:
                    btn = page.query_selector(btn_sel)
                    if btn and btn.is_visible():
                        btn.click()
                        break
                except Exception:
                    pass
            return False, "Chat did not open — number may not be on WhatsApp"

        time.sleep(1.5)

        # Click send button
        send_btn = page.query_selector(
            'button[data-tab="11"], '
            'span[data-icon="send"], '
            '[data-testid="send"], '
            'button[aria-label="Send"]'
        )
        if send_btn and send_btn.is_visible():
            send_btn.click()
        else:
            input_box.click()
            page.keyboard.press("Enter")

        time.sleep(1.5)
        return True, ""

    except Exception as e:
        return False, str(e)