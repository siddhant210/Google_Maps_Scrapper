"""
WhatsApp service using Playwright.

KEY FIXES:
1. Images sent via Photos & Videos attach input → always renders as photo, never sticker
2. PDFs/docs sent via document file input → renders as file attachment
3. Send button found via multiple strategies + Enter fallback
4. Bulk send properly iterates all leads
"""

import json, os, re, time, urllib.parse, base64, mimetypes, io, csv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    import openpyxl
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False
    print("[WA] WARNING: openpyxl not installed. Run: pip install openpyxl")

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


# ── Parse leads from uploaded Excel / CSV ────────────────────────────────────

def extract_leads_from_file(data: bytes, filename: str) -> list[dict]:
    """
    Extract leads (Name + Phone) from an uploaded Excel (.xlsx/.xls) or CSV file.
    Returns list of dicts with at least 'Name' and 'Phone' keys.
    """
    ext  = os.path.splitext(filename)[1].lower()
    rows = []

    try:
        if ext in ('.xlsx', '.xls'):
            if not OPENPYXL_OK:
                raise ImportError("openpyxl is not installed. Run: pip install openpyxl")
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            ws = wb.active
            headers = None
            for row in ws.iter_rows(values_only=True):
                vals = [str(c).strip() if c is not None else '' for c in row]
                if not any(vals):
                    continue
                if headers is None:
                    headers = [v.lower().strip() for v in vals]
                    continue
                entry = {}
                for i, h in enumerate(headers):
                    entry[h] = vals[i] if i < len(vals) else ''
                rows.append(entry)
            print(f"[extract_leads] xlsx: {len(rows)} data rows, headers={headers}")

        elif ext == '.csv':
            # Try utf-8 first, fall back to latin-1
            for enc in ('utf-8-sig', 'utf-8', 'latin-1'):
                try:
                    text   = data.decode(enc)
                    reader = csv.DictReader(io.StringIO(text))
                    rows   = [{k.lower().strip(): str(v).strip() for k, v in row.items()} for row in reader]
                    print(f"[extract_leads] csv ({enc}): {len(rows)} rows")
                    break
                except Exception:
                    continue
        else:
            raise ValueError(f"Unsupported file type: {ext}")

    except Exception as e:
        print(f"[extract_leads] Parse error: {e}")
        raise  # re-raise so Flask route can return the real error message

    phone_keys = ['phone','mobile','contact','number','tel','cell','whatsapp','ph',
                  'phonenumber','phone number','mobile number','contact number',
                  'phone no','mobile no','contact no']
    name_keys  = ['name','business','company','firm','title','business name','shop',
                  'customer name','client name']

    leads = []
    for row in rows:
        phone = ''
        for k in phone_keys:
            if k in row and row[k]:
                phone = row[k]; break
        if not phone:
            # grab any value where ≥60% chars are digits and total digits ≥ 7
            for k, v in row.items():
                v = v.strip()
                digits = re.sub(r'\D', '', v)
                non_space = re.sub(r'\s', '', v)
                if len(digits) >= 7 and (len(non_space) == 0 or len(digits)/len(non_space) >= 0.55):
                    phone = v; break

        name = ''
        for k in name_keys:
            if k in row and row[k]:
                name = row[k]; break
        if not name:
            # first non-phone, non-empty value
            name = next((v for k, v in row.items() if v and k not in phone_keys
                         and not re.fullmatch(r'[\d\s\+\-\(\)\.]+', v)), 'Lead')

        if phone:
            leads.append({
                'Name':    name.strip(),
                'Phone':   phone.strip(),
                'Address': row.get('address', row.get('addr', row.get('location', ''))),
                'Rating':  row.get('rating', ''),
            })

    print(f"[extract_leads] {filename}: {len(rows)} rows → {len(leads)} leads with phone")
    return leads


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

    attach   = attachment_path or get_attachment_path()
    if attach and not os.path.exists(attach): attach = None
    print(f"[WA] Attachment: {os.path.basename(attach) if attach else 'None'}")

    is_media = False
    if attach:
        ext      = os.path.splitext(attach)[1].lower()
        is_media = ext in ('.jpg','.jpeg','.png','.gif','.webp','.bmp',
                           '.mp4','.mov','.avi','.mkv','.webm')

    os.makedirs(SESSION_DIR, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=SESSION_DIR,
            headless=False,
            slow_mo=80,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        page = context.new_page()

        try:
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
                    ok, err = _send_image_via_attach(page, phone, message, attach)
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


# ── Image via Photos & Videos attach input (FIXED — never sends as sticker) ───

def _send_image_via_attach(page, phone, message, file_path):
    """
    Send image using WhatsApp's Photos & Videos file input.
    Using the image/* accept input means WhatsApp ALWAYS renders it as a
    normal photo in the chat — never as a sticker or document.

    Flow:
      1. Open chat
      2. Click the attach (paperclip) button
      3. Set the file on the image/video file input directly
      4. Wait for preview
      5. Type caption
      6. Click Send
    """
    try:
        page.goto(f"https://web.whatsapp.com/send?phone={phone}",
                  wait_until="domcontentloaded", timeout=25000)
        time.sleep(5)
        _dismiss_popup(page)

        box = _wait_input(page)
        if not box: return False, "Chat did not open"

        # ── Step 1: Click the paperclip / attach button ───────────────────
        attach_btn = None
        for sel in [
            'div[title="Attach"]',
            'button[title="Attach"]',
            '[data-testid="attach-menu-plus"]',
            'span[data-icon="attach"]',
            'div[aria-label="Attach"]',
            'button[aria-label="Attach"]',
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    attach_btn = el; break
            except Exception: continue

        if not attach_btn:
            return False, "Attach button not found"

        attach_btn.click()
        time.sleep(1.5)

        # ── Step 2: Use the Photos & Videos file input ────────────────────
        # This is the key fix: WhatsApp has a separate <input accept="image/*,video/...">
        # for photos/videos. Files set on THIS input are always sent as media, not stickers.
        uploaded = False

        for sel in [
            'input[type="file"][accept="image/*,video/mp4,video/3gpp,video/quicktime"]',
            'input[type="file"][accept*="image/"][accept*="video"]',
            'input[type="file"][accept*="image/"]',
            'input[accept*="image"]',
        ]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.set_input_files(file_path)
                    print(f"[WA]   ✓ Image file input: {sel}")
                    uploaded = True; break
            except Exception: continue

        # Fallback: try clicking "Photos & Videos" menu item with file chooser
        if not uploaded:
            for sel in [
                'span:has-text("Photos & Videos")',
                'li:has-text("Photos")',
                '[data-testid="mi-attach-image"]',
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        with page.expect_file_chooser(timeout=4000) as fc:
                            el.click()
                        fc.value.set_files(file_path)
                        print(f"[WA]   ✓ Photos menu item: {sel}")
                        uploaded = True; break
                except Exception: continue

        if not uploaded:
            print("[WA]   Image input not found — falling back to document send")
            return _send_document(page, phone, message, file_path)

        time.sleep(3)  # wait for preview to render

        # ── Step 3: Type caption ──────────────────────────────────────────
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
                page.keyboard.type(message, delay=15)
            time.sleep(0.8)

        # ── Step 4: Click Send ────────────────────────────────────────────
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
    print("[WA]   No send button — pressing Enter")
    box.click()
    page.keyboard.press("Enter")
    return True