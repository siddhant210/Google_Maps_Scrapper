"""
WhatsApp service using Playwright to drive WhatsApp Web.
Supports sending text messages + optional file attachment (image, PDF, video, etc.)
"""

import json
import os
import re
import time
import urllib.parse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TEMPLATE_FILE  = "whatsapp_template.json"
SESSION_DIR    = "wa_session"
ATTACHMENT_DIR = "wa_attachments"
QR_TIMEOUT     = 90


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

def get_attachment_path() -> str | None:
    """Return path of currently saved attachment, or None."""
    if not os.path.exists(ATTACHMENT_DIR):
        return None
    files = [f for f in os.listdir(ATTACHMENT_DIR) if not f.startswith('.')]
    if files:
        return os.path.join(ATTACHMENT_DIR, files[0])
    return None

def save_attachment(file_data: bytes, filename: str) -> str:
    """Save uploaded attachment, replacing any previous one."""
    os.makedirs(ATTACHMENT_DIR, exist_ok=True)
    # Clear old files
    for f in os.listdir(ATTACHMENT_DIR):
        try:
            os.remove(os.path.join(ATTACHMENT_DIR, f))
        except Exception:
            pass
    path = os.path.join(ATTACHMENT_DIR, filename)
    with open(path, 'wb') as f:
        f.write(file_data)
    return path

def clear_attachment():
    if os.path.exists(ATTACHMENT_DIR):
        for f in os.listdir(ATTACHMENT_DIR):
            try:
                os.remove(os.path.join(ATTACHMENT_DIR, f))
            except Exception:
                pass


# ── Phone sanitiser ───────────────────────────────────────────────────────────

def sanitize_phone(phone: str) -> str | None:
    if not phone:
        return None
    raw    = str(phone).strip()
    digits = re.sub(r'\D', '', raw)
    if not digits or len(digits) < 7:
        return None
    if digits.startswith('91') and len(digits) == 12:
        return digits
    if digits.startswith('91') and len(digits) == 13:
        return digits
    if len(digits) == 10 and digits[0] in '6789':
        return '91' + digits
    if len(digits) == 11 and digits.startswith('0'):
        return '91' + digits[1:]
    if len(digits) == 10 and digits.startswith('0'):
        return '91' + digits[1:]
    if len(digits) == 11 and digits.startswith('1'):
        return '91' + digits
    if len(digits) >= 10:
        return '91' + digits[-10:]
    return None


# ── Core sender ───────────────────────────────────────────────────────────────

def send_bulk_whatsapp(leads: list[dict], template: str,
                       delay_between: int = 5,
                       test_number: str = None,
                       attachment_path: str = None) -> dict:
    results = {"total": 0, "sent": 0, "failed": 0, "details": []}
    os.makedirs("debug", exist_ok=True)

    # Build send list
    if test_number:
        phone = sanitize_phone(test_number)
        if not phone:
            return {"total": 1, "sent": 0, "failed": 1,
                    "details": [{"name": "Test", "phone": test_number,
                                 "status": "failed", "error": f"Cannot parse: {test_number}"}]}
        send_list = [{"lead": {"Name": "Test User", "Address": "Test", "Rating": "5.0"}, "phone": phone}]
        print(f"[WhatsApp] TEST MODE → {phone}")
    else:
        send_list = []
        for lead in leads:
            raw   = lead.get("Phone", "")
            phone = sanitize_phone(raw)
            if phone:
                send_list.append({"lead": lead, "phone": phone})
                print(f"[WhatsApp] ✓ {lead.get('Name','?')} → {phone}")
            else:
                results["failed"] += 1
                results["details"].append({
                    "name": lead.get("Name", "?"), "phone": raw,
                    "status": "skipped", "error": f"Cannot parse: '{raw}'"
                })

    results["total"] = len(send_list) + results["failed"]
    if not send_list:
        return results

    # Check attachment
    attach = attachment_path or get_attachment_path()
    if attach and os.path.exists(attach):
        print(f"[WhatsApp] Attachment: {attach}")
    else:
        attach = None
        print("[WhatsApp] No attachment — text only")

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
            # ── Login ─────────────────────────────────────────────────────
            print("[WhatsApp] Opening WhatsApp Web...")
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)

            print(f"[WhatsApp] Waiting for login (scan QR if needed)...")
            logged_in = False
            for _ in range(QR_TIMEOUT):
                try:
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

            if not logged_in:
                print("[WhatsApp] Login timed out.")
                results["failed"] += len(send_list)
                return results

            print("[WhatsApp] ✓ Logged in!")
            time.sleep(2)

            # ── Send loop ─────────────────────────────────────────────────
            for i, item in enumerate(send_list):
                lead  = item["lead"]
                phone = item["phone"]
                name  = lead.get("Name", "there")

                message = (template
                    .replace("{name}",    name)
                    .replace("{address}", lead.get("Address", ""))
                    .replace("{rating}",  str(lead.get("Rating", ""))))

                print(f"\n[WhatsApp] [{i+1}/{len(send_list)}] → {name} ({phone})")

                if attach:
                    ok, err = _send_with_attachment(page, phone, message, attach, i)
                else:
                    ok, err = _send_text_only(page, phone, message, i)

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
        finally:
            time.sleep(2)
            context.close()

    print(f"\n[WhatsApp] Done — Sent: {results['sent']} | Failed: {results['failed']}")
    return results


# ── Text-only send ────────────────────────────────────────────────────────────

def _send_text_only(page, phone: str, message: str, idx: int = 0) -> tuple[bool, str]:
    try:
        encoded = urllib.parse.quote(message, safe='')
        url     = f"https://web.whatsapp.com/send?phone={phone}&text={encoded}"
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)

        # Dismiss any popups
        _dismiss_popup(page)

        # Find input box
        input_box = _find_input(page)
        if not input_box:
            return False, "Input box not found"

        input_box.click()
        time.sleep(0.5)

        # Verify text is in box, type if not
        box_text = ""
        try:
            box_text = (input_box.text_content() or "").strip()
        except Exception:
            pass

        if not box_text:
            page.keyboard.type(message, delay=20)
            time.sleep(0.8)

        # Send
        _click_send(page, input_box)
        time.sleep(2)
        return True, ""

    except Exception as e:
        return False, str(e)


# ── Send with attachment ──────────────────────────────────────────────────────

def _send_with_attachment(page, phone: str, message: str,
                           attachment_path: str, idx: int = 0) -> tuple[bool, str]:
    """
    Send a file attachment + caption text via WhatsApp Web.

    Flow:
      1. Navigate to the chat
      2. Click the paperclip/attach button
      3. Use file chooser to upload the file
      4. Type message as caption
      5. Click Send
    """
    try:
        # Open chat (no pre-filled text — we'll add caption after attaching)
        url = f"https://web.whatsapp.com/send?phone={phone}"
        page.goto(url, wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)

        _dismiss_popup(page)
        page.screenshot(path=f"debug/wa_{idx:02d}_a_chat.png")

        # Verify chat opened
        input_box = _find_input(page)
        if not input_box:
            return False, "Chat did not open"

        # ── Click the Attach / Paperclip button ───────────────────────────
        attach_btn = None
        attach_selectors = [
            'div[title="Attach"]',
            'button[title="Attach"]',
            'span[data-icon="attach"]',
            '[data-testid="attach-menu-plus"]',
            'div[aria-label="Attach"]',
            'button[aria-label="Attach"]',
            'span[data-icon="plus"]',
            '[data-testid="attach"]',
        ]
        for sel in attach_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    attach_btn = el
                    print(f"[WhatsApp]   Attach btn: {sel}")
                    break
            except Exception:
                continue

        if not attach_btn:
            print("[WhatsApp]   No attach button — sending text only")
            return _send_text_only(page, phone, message, idx)

        attach_btn.click()
        time.sleep(1.5)
        page.screenshot(path=f"debug/wa_{idx:02d}_b_attach_menu.png")

        # ── Choose file type from the popup menu ──────────────────────────
        # After clicking paperclip, a menu appears with Photos, Documents, etc.
        # We click "Document" for PDFs, or "Photos & Videos" for images
        ext = os.path.splitext(attachment_path)[1].lower()
        is_image = ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')

        file_input = None

        if is_image:
            # Try "Photos & Videos" option
            for sel in [
                'li span:has-text("Photos")',
                'span:has-text("Photos & Videos")',
                '[data-testid="mi-attach-image"]',
                'input[accept*="image"]',
                'li[title="Photos & Videos"]',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        time.sleep(1)
                        break
                except Exception:
                    continue

        # For all file types, also try finding the hidden file input directly
        # WhatsApp Web has hidden <input type="file"> elements
        for sel in [
            'input[type="file"][accept*="*"]',
            'input[type="file"]',
            'input[accept="*"]',
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    file_input = el
                    break
            except Exception:
                continue

        # ── Upload the file via file chooser ──────────────────────────────
        uploaded = False

        if file_input:
            try:
                file_input.set_input_files(attachment_path)
                uploaded = True
                print(f"[WhatsApp]   File set via input: {attachment_path}")
            except Exception as e:
                print(f"[WhatsApp]   set_input_files failed: {e}")

        if not uploaded:
            # Use Playwright's file chooser interception
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    # Try clicking "Document" menu item
                    for sel in [
                        'li span:has-text("Document")',
                        'span:has-text("Document")',
                        '[data-testid="mi-attach-document"]',
                        'li[title="Document"]',
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible():
                                el.click()
                                break
                        except Exception:
                            continue
                file_chooser = fc_info.value
                file_chooser.set_files(attachment_path)
                uploaded = True
                print(f"[WhatsApp]   File chosen via chooser: {attachment_path}")
            except Exception as e:
                print(f"[WhatsApp]   File chooser failed: {e}")

        if not uploaded:
            print("[WhatsApp]   Upload failed — sending text only")
            page.keyboard.press("Escape")
            time.sleep(1)
            return _send_text_only(page, phone, message, idx)

        time.sleep(3)  # wait for file preview to appear
        page.screenshot(path=f"debug/wa_{idx:02d}_c_preview.png")

        # ── Type caption in the caption input ─────────────────────────────
        caption_selectors = [
            'div[contenteditable="true"][data-tab="10"]',
            'div[contenteditable="true"][aria-label="Add a caption…"]',
            'div[contenteditable="true"][aria-placeholder*="caption"]',
            'div[contenteditable="true"][aria-label*="caption"]',
            'div[contenteditable="true"][data-lexical-editor]',
            # Sometimes the main input box becomes the caption box
            'div[contenteditable="true"]',
        ]
        caption_box = None
        for sel in caption_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    caption_box = el
                    print(f"[WhatsApp]   Caption box: {sel}")
                    break
            except Exception:
                continue

        if caption_box and message:
            caption_box.click()
            time.sleep(0.5)
            page.keyboard.type(message, delay=20)
            time.sleep(0.8)
        elif message:
            # Just type — focus should be on caption
            page.keyboard.type(message, delay=20)
            time.sleep(0.8)

        page.screenshot(path=f"debug/wa_{idx:02d}_d_caption.png")

        # ── Click Send ────────────────────────────────────────────────────
        send_selectors = [
            'div[aria-label="Send"]',
            'button[aria-label="Send"]',
            'span[data-icon="send"]',
            '[data-testid="send"]',
            'button[data-testid="compose-btn-send"]',
        ]
        sent = False
        for sel in send_selectors:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    sent = True
                    print(f"[WhatsApp]   Send clicked: {sel}")
                    break
            except Exception:
                continue

        if not sent:
            page.keyboard.press("Enter")
            print("[WhatsApp]   Pressed Enter to send")

        time.sleep(2)
        page.screenshot(path=f"debug/wa_{idx:02d}_e_sent.png")
        return True, ""

    except Exception as e:
        import traceback; traceback.print_exc()
        try:
            page.screenshot(path=f"debug/wa_{idx:02d}_error.png")
        except Exception:
            pass
        return False, str(e)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _dismiss_popup(page):
    for sel in [
        'div[data-animate-modal-body="true"] button',
        'div[role="dialog"] button',
        '[data-testid="popup-contents"] button',
    ]:
        try:
            modal = page.query_selector(sel)
            if modal and modal.is_visible():
                modal.click()
        except Exception:
            pass


def _find_input(page):
    for sel in [
        'div[contenteditable="true"][data-tab="10"]',
        'div[contenteditable="true"][data-tab="6"]',
        'div[contenteditable="true"][title="Type a message"]',
        'div[contenteditable="true"][aria-label="Type a message"]',
        'footer div[contenteditable="true"]',
        'div[role="textbox"]',
    ]:
        try:
            el = page.wait_for_selector(sel, timeout=4000, state="visible")
            if el:
                return el
        except PlaywrightTimeout:
            continue
        except Exception:
            continue
    return None


def _click_send(page, input_box):
    for sel in [
        'button[data-testid="compose-btn-send"]',
        'button[aria-label="Send"]',
        'span[data-icon="send"]',
        '[data-testid="send"]',
        'button[data-tab="11"]',
        'footer button[aria-label="Send"]',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                return
        except Exception:
            continue
    # Fallback
    input_box.click()
    page.keyboard.press("Enter")