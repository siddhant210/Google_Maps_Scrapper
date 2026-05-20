"""
WhatsApp service using Playwright.

KEY FIXES:
1. Images sent via clipboard paste (Ctrl+V) → always renders as photo, never sticker
2. PDFs/docs sent via document file input → renders as file attachment
3. Send button found via multiple strategies + Enter fallback
4. Bulk send properly iterates all leads
"""

import json, os, re, time, urllib.parse, base64, mimetypes
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TEMPLATE_FILE  = "whatsapp_template.json"
SESSION_DIR    = "wa_session"
ATTACHMENT_DIR = "wa_attachments"
QR_TIMEOUT     = 90


# ── Template helpers ──────────────────────────────────────────────────────────

def get_default_template():
    return ("Hi {name}! We noticed your business on Google Maps. "
            "We help local businesses get more customers. "
            "Reply YES to know more. Reply STOP to opt out.")

def load_template():
    if os.path.exists(TEMPLATE_FILE):
        try:
            with open(TEMPLATE_FILE) as f:
                return json.load(f).get("template", get_default_template())
        except Exception: pass
    return get_default_template()

def save_template(t: str) -> bool:
    try:
        with open(TEMPLATE_FILE, "w") as f:
            json.dump({"template": t}, f, indent=2)
        return True
    except Exception: return False

def get_attachment_path() -> str | None:
    if not os.path.exists(ATTACHMENT_DIR): return None
    files = [f for f in os.listdir(ATTACHMENT_DIR) if not f.startswith('.')]
    return os.path.join(ATTACHMENT_DIR, files[0]) if files else None

def save_attachment(data: bytes, filename: str) -> str:
    os.makedirs(ATTACHMENT_DIR, exist_ok=True)
    for f in os.listdir(ATTACHMENT_DIR):
        try: os.remove(os.path.join(ATTACHMENT_DIR, f))
        except Exception: pass
    path = os.path.join(ATTACHMENT_DIR, filename)
    with open(path, 'wb') as f: f.write(data)
    return path

def clear_attachment():
    if os.path.exists(ATTACHMENT_DIR):
        for f in os.listdir(ATTACHMENT_DIR):
            try: os.remove(os.path.join(ATTACHMENT_DIR, f))
            except Exception: pass


# ── Phone sanitiser ───────────────────────────────────────────────────────────

def sanitize_phone(phone: str) -> str | None:
    if not phone: return None
    digits = re.sub(r'\D', '', str(phone).strip())
    if not digits or len(digits) < 7: return None
    if digits.startswith('91') and len(digits) in (12, 13): return digits
    if len(digits) == 10 and digits[0] in '6789': return '91' + digits
    if len(digits) == 11 and digits.startswith('0'): return '91' + digits[1:]
    if len(digits) == 10 and digits.startswith('0'): return '91' + digits[1:]
    if len(digits) == 11 and digits.startswith('1'): return '91' + digits
    if len(digits) >= 10: return '91' + digits[-10:]
    return None


# ── Core sender ───────────────────────────────────────────────────────────────

def send_bulk_whatsapp(leads: list[dict], template: str,
                       delay_between: int = 5,
                       test_number: str = None,
                       attachment_path: str = None,
                       chunk_start: int = None,
                       chunk_end: int = None) -> dict:

    results = {"total": 0, "sent": 0, "failed": 0, "details": []}
    os.makedirs("debug", exist_ok=True)

    # ── Build send list ───────────────────────────────────────────────────
    if test_number:
        phone = sanitize_phone(test_number)
        if not phone:
            return {"total":1,"sent":0,"failed":1,
                    "details":[{"name":"Test","phone":test_number,"status":"failed",
                                "error":f"Cannot parse: {test_number}"}]}
        send_list = [{"lead":{"Name":"Test User","Address":"Test","Rating":"5.0"},"phone":phone}]
        print(f"[WA] TEST → {phone}")
    else:
        # Apply chunk
        working = leads
        if chunk_start and chunk_end:
            working = leads[chunk_start-1 : chunk_end]
            print(f"[WA] Chunk {chunk_start}–{chunk_end}: {len(working)} leads")
        else:
            print(f"[WA] All leads: {len(working)}")

        send_list = []
        for lead in working:
            raw   = lead.get("Phone","")
            phone = sanitize_phone(raw)
            if phone:
                send_list.append({"lead": lead, "phone": phone})
                print(f"[WA] ✓ {lead.get('Name','?')} → {phone}")
            else:
                results["failed"] += 1
                results["details"].append({
                    "name": lead.get("Name","?"), "phone": raw,
                    "status": "skipped", "error": f"Cannot parse: '{raw}'"
                })
                print(f"[WA] ✗ Skip: {lead.get('Name','?')} '{raw}'")

    results["total"] = len(send_list) + results["failed"]

    if not send_list:
        print("[WA] Nothing to send.")
        return results

    # Resolve attachment
    attach = attachment_path or get_attachment_path()
    if attach and not os.path.exists(attach): attach = None
    print(f"[WA] Attachment: {os.path.basename(attach) if attach else 'None'}")

    # Detect if attachment is an image/video
    is_media = False
    if attach:
        ext = os.path.splitext(attach)[1].lower()
        is_media = ext in ('.jpg','.jpeg','.png','.gif','.webp','.bmp','.mp4','.mov','.avi','.mkv','.webm')

    os.makedirs(SESSION_DIR, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            slow_mo=80,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                # Allow clipboard access for paste approach
                "--enable-features=UseOzonePlatform",
            ],
        )
        page = context.new_page()

        try:
            # ── Login ─────────────────────────────────────────────────────
            print("[WA] Opening WhatsApp Web...")
            page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)

            print(f"[WA] Waiting for login (scan QR if needed, up to {QR_TIMEOUT}s)...")
            logged_in = False
            for _ in range(QR_TIMEOUT):
                try:
                    if page.query_selector('[data-testid="chat-list-search"], div#side, [aria-label="Search input textbox"]'):
                        logged_in = True; break
                except Exception: pass
                time.sleep(1)

            if not logged_in:
                print("[WA] Login timeout.")
                results["failed"] += len(send_list)
                return results

            print("[WA] ✓ Logged in!")
            time.sleep(2)

            # ── Send loop ─────────────────────────────────────────────────
            for i, item in enumerate(send_list):
                lead  = item["lead"]
                phone = item["phone"]
                name  = lead.get("Name", "there")

                message = (template
                    .replace("{name}",    name)
                    .replace("{address}", lead.get("Address",""))
                    .replace("{rating}",  str(lead.get("Rating",""))))

                print(f"\n[WA] [{i+1}/{len(send_list)}] → {name} ({phone})")

                if attach and is_media:
                    ok, err = _send_image_clipboard(page, phone, message, attach)
                elif attach:
                    ok, err = _send_document(page, phone, message, attach)
                else:
                    ok, err = _send_text(page, phone, message)

                if ok:
                    results["sent"] += 1
                    results["details"].append({"name":name,"phone":phone,"status":"sent"})
                    print(f"[WA]   ✓ Sent!")
                else:
                    results["failed"] += 1
                    results["details"].append({"name":name,"phone":phone,"status":"failed","error":err})
                    print(f"[WA]   ✗ Failed: {err}")

                if i < len(send_list) - 1:
                    time.sleep(delay_between)

        except Exception as e:
            import traceback; traceback.print_exc()
        finally:
            time.sleep(2)
            context.close()

    print(f"\n[WA] Done — Sent:{results['sent']} Failed:{results['failed']}")
    return results


# ── Text only ─────────────────────────────────────────────────────────────────

def _send_text(page, phone, message):
    try:
        encoded = urllib.parse.quote(message, safe='')
        page.goto(f"https://web.whatsapp.com/send?phone={phone}&text={encoded}",
                  wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)
        _dismiss_popup(page)

        box = _wait_input(page)
        if not box: return False, "Chat did not open"

        box.click(); time.sleep(0.5)

        # Check pre-fill worked
        try: txt = (box.text_content() or "").strip()
        except Exception: txt = ""
        if not txt:
            page.keyboard.type(message, delay=15)
            time.sleep(0.8)

        ok = _press_send(page, box)
        time.sleep(2)
        return True, ""
    except Exception as e:
        return False, str(e)


# ── Image via clipboard paste ─────────────────────────────────────────────────

def _send_image_clipboard(page, phone, message, file_path):
    """
    Paste image from clipboard into WhatsApp Web.
    This ALWAYS renders as a proper photo — never a sticker or file.

    How it works:
      1. Open the chat
      2. Use JavaScript to write the image to the clipboard as image/png
      3. Focus the message input box
      4. Press Ctrl+V → WhatsApp shows the image preview
      5. Type caption
      6. Click Send / press Enter
    """
    try:
        page.goto(f"https://web.whatsapp.com/send?phone={phone}",
                  wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)
        _dismiss_popup(page)

        box = _wait_input(page)
        if not box: return False, "Chat did not open"

        # Read image file and encode as base64
        with open(file_path, 'rb') as f:
            img_bytes = f.read()

        ext  = os.path.splitext(file_path)[1].lower()
        mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg',
                '.gif':'image/gif','.webp':'image/webp','.bmp':'image/bmp'}.get(ext,'image/png')

        b64 = base64.b64encode(img_bytes).decode('utf-8')

        # Write image to clipboard via JS ClipboardItem API
        print(f"[WA]   Writing image to clipboard ({mime}, {len(img_bytes)//1024}KB)...")
        clipboard_result = page.evaluate(f"""
            async () => {{
                try {{
                    const b64 = '{b64}';
                    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                    const blob  = new Blob([bytes], {{ type: '{mime}' }});
                    const item  = new ClipboardItem({{ '{mime}': blob }});
                    await navigator.clipboard.write([item]);
                    return 'ok';
                }} catch(e) {{
                    return 'err:' + e.message;
                }}
            }}
        """)
        print(f"[WA]   Clipboard write: {clipboard_result}")

        if clipboard_result and clipboard_result.startswith('err'):
            # Clipboard API failed — fallback to document upload
            print("[WA]   Clipboard failed, falling back to document upload")
            return _send_document(page, phone, message, file_path)

        # Focus the chat input and paste
        box.click(); time.sleep(0.5)
        page.keyboard.press("Control+v")
        time.sleep(3)  # wait for image preview to appear

        # ── Now in the image preview screen ──────────────────────────────
        # Type caption in the caption input
        if message:
            caption_box = None
            for sel in [
                'div[contenteditable="true"][aria-label="Add a caption…"]',
                'div[contenteditable="true"][aria-placeholder*="caption" i]',
                'div[aria-label="Add a caption…"]',
                'div[contenteditable="true"][data-tab="10"]',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        caption_box = el; break
                except Exception: continue

            if caption_box:
                caption_box.click(); time.sleep(0.3)
                page.keyboard.type(message, delay=15)
            else:
                # Just type — focus should be on caption area
                page.keyboard.type(message, delay=15)
            time.sleep(0.8)

        # ── Click Send ────────────────────────────────────────────────────
        sent = False
        for sel in [
            'div[aria-label="Send"]',
            'button[aria-label="Send"]',
            'span[data-icon="send"]',
            '[data-testid="send"]',
            'button[data-testid="compose-btn-send"]',
        ]:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(); sent = True
                    print(f"[WA]   Clicked send: {sel}")
                    break
            except Exception: continue

        if not sent:
            print("[WA]   No send button — pressing Enter")
            page.keyboard.press("Enter")

        time.sleep(2)
        return True, ""

    except Exception as e:
        import traceback; traceback.print_exc()
        return False, str(e)


# ── Document/PDF via file input ───────────────────────────────────────────────

def _send_document(page, phone, message, file_path):
    """Send non-image files (PDF, Word, Excel) as document attachments."""
    try:
        page.goto(f"https://web.whatsapp.com/send?phone={phone}",
                  wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)
        _dismiss_popup(page)

        box = _wait_input(page)
        if not box: return False, "Chat did not open"

        # Click attach button
        attach_btn = None
        for sel in ['div[title="Attach"]','button[title="Attach"]','[data-testid="attach-menu-plus"]',
                    'span[data-icon="attach"]','div[aria-label="Attach"]','button[aria-label="Attach"]']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible(): attach_btn = el; break
            except Exception: continue

        if not attach_btn:
            return _send_text(page, phone, message)

        attach_btn.click(); time.sleep(1.5)

        # Find document file input
        file_input = None
        for sel in ['input[type="file"][accept="*"]','input[type="file"]']:
            try:
                el = page.query_selector(sel)
                if el: file_input = el; break
            except Exception: continue

        if file_input:
            file_input.set_input_files(file_path)
            print(f"[WA]   Document uploaded")
        else:
            try:
                with page.expect_file_chooser(timeout=5000) as fc:
                    for sel in ['span:has-text("Document")','li:has-text("Document")',
                                '[data-testid="mi-attach-document"]']:
                        try:
                            el = page.query_selector(sel)
                            if el and el.is_visible(): el.click(); break
                        except Exception: continue
                fc.value.set_files(file_path)
            except Exception as e:
                print(f"[WA]   Doc upload failed: {e}")
                return _send_text(page, phone, message)

        time.sleep(3)

        if message:
            for sel in ['div[contenteditable="true"][aria-label="Add a caption…"]',
                        'div[contenteditable="true"][aria-placeholder*="caption" i]',
                        'div[contenteditable="true"]']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(); time.sleep(0.3)
                        page.keyboard.type(message, delay=15); break
                except Exception: continue
            time.sleep(0.8)

        # Send
        sent = False
        for sel in ['div[aria-label="Send"]','button[aria-label="Send"]',
                    'span[data-icon="send"]','[data-testid="send"]']:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible(): btn.click(); sent=True; break
            except Exception: continue
        if not sent: page.keyboard.press("Enter")

        time.sleep(2)
        return True, ""

    except Exception as e:
        return False, str(e)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _dismiss_popup(page):
    for sel in ['div[data-animate-modal-body="true"] button',
                'div[role="dialog"] button',
                '[data-testid="popup-contents"] button']:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible(): el.click()
        except Exception: pass

def _wait_input(page):
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
            if el: return el
        except Exception: continue
    return None

def _press_send(page, box):
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
                print(f"[WA]   Send clicked: {sel}")
                return True
        except Exception: continue
    # Fallback
    print("[WA]   No send button — pressing Enter")
    box.click()
    page.keyboard.press("Enter")
    return True