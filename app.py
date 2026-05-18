from flask import Flask, render_template, request, send_file, jsonify
from scraper import scrape_google_maps
from whatsapp_service import load_template, save_template, send_bulk_whatsapp, sanitize_phone
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
import io
import os

app = Flask(__name__)
scraped_data = []
os.makedirs("exports", exist_ok=True)


@app.route("/")
def home():
    return render_template("index.html", data=scraped_data, error=None)


@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    global scraped_data
    if request.method == "GET":
        return render_template("index.html", data=scraped_data, error=None)

    query = request.form.get("query", "").strip()
    if not query:
        return render_template("index.html", data=[], error="Please enter a search query.")

    try:
        print(f"[app] Scraping: {query}")
        scraped_data = scrape_google_maps(query)
        print(f"[app] Got {len(scraped_data)} results")
        if not scraped_data:
            return render_template("index.html", data=[],
                error="No results found. Try a different query like 'restaurants in Bandra'.")
    except Exception as e:
        import traceback; print(traceback.format_exc())
        return render_template("index.html", data=[], error=f"Scraper error: {str(e)}")

    return render_template("index.html", data=scraped_data, error=None)


@app.route("/export")
def export():
    global scraped_data
    if not scraped_data:
        return "No data to export. Run a search first.", 400

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Leads"

    columns = ["Name", "Category", "Rating", "Reviews", "Latest Review",
               "Email", "Address", "Phone", "Website"]

    header_fill  = PatternFill("solid", fgColor="1A1A2E")
    header_font  = Font(name="Calibri", bold=True, color="00E5A0", size=11)
    header_align = Alignment(horizontal="center", vertical="center")
    alt_fill     = PatternFill("solid", fgColor="F7F9FC")
    normal_fill  = PatternFill("solid", fgColor="FFFFFF")
    cell_font    = Font(name="Calibri", size=10, color="1A1A2E")
    cell_align   = Alignment(vertical="center", wrap_text=True)
    thin         = Side(style="thin", color="E0E4EC")
    border       = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.row_dimensions[1].height = 32
    for ci, col in enumerate(columns, 1):
        c = ws.cell(row=1, column=ci, value=col)
        c.font = header_font; c.fill = header_fill
        c.alignment = header_align; c.border = border

    for ri, item in enumerate(scraped_data, 2):
        ws.row_dimensions[ri].height = 22
        fill = alt_fill if ri % 2 == 0 else normal_fill
        for ci, col in enumerate(columns, 1):
            val  = item.get(col, "") or ""
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.font = cell_font; cell.fill = fill
            cell.alignment = cell_align; cell.border = border
            if col == "Website" and val.startswith("http"):
                cell.hyperlink = val
                cell.font = Font(name="Calibri", size=10, color="0563C1", underline="single")
            if col in ("Rating", "Reviews"):
                cell.alignment = Alignment(horizontal="center", vertical="center")

    widths = {"Name": 28, "Category": 18, "Rating": 8, "Reviews": 10,
              "Latest Review": 22, "Email": 25, "Address": 45, "Phone": 16, "Website": 35}
    for ci, col in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(ci)].width = widths[col]

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}1"

    output = io.BytesIO()
    wb.save(output); output.seek(0)
    return send_file(output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="leads.xlsx")


# ── WhatsApp routes ───────────────────────────────────────────────────────────

@app.route("/whatsapp-template", methods=["GET", "POST"])
def whatsapp_template():
    if request.method == "GET":
        return jsonify({"template": load_template()})
    body = request.json or {}
    t = body.get("template", "").strip()
    if not t:
        return jsonify({"error": "Template cannot be empty"}), 400
    return jsonify({"success": True}) if save_template(t) else (jsonify({"error": "Save failed"}), 500)


@app.route("/whatsapp-lead-count")
def whatsapp_lead_count():
    global scraped_data
    total      = len(scraped_data)
    with_phone = sum(1 for l in scraped_data if sanitize_phone(l.get("Phone", "")))
    return jsonify({"total": total, "with_phone": with_phone})


@app.route("/send-whatsapp", methods=["POST"])
def send_whatsapp():
    global scraped_data
    body        = request.json or {}
    delay       = int(body.get("delay", 5))
    test_number = body.get("test_number", "").strip() or None

    # If test mode, we don't need scraped data
    if not test_number and not scraped_data:
        return jsonify({"error": "No leads found. Scrape first."}), 400

    template = load_template()
    try:
        results = send_bulk_whatsapp(
            scraped_data, template,
            delay_between=delay,
            test_number=test_number
        )
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)