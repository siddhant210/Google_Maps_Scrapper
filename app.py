from flask import Flask, render_template, request, send_file
from scraper import scrape_google_maps
import pandas as pd
import os

app = Flask(__name__)

scraped_data = []

# Make sure exports folder exists
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
        print(f"[app] Scraping for: {query}")
        scraped_data = scrape_google_maps(query)
        print(f"[app] Got {len(scraped_data)} results")

        if not scraped_data:
            return render_template(
                "index.html",
                data=[],
                error="No results found. Try a different search like 'restaurants in Bandra'."
            )

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return render_template("index.html", data=[], error=f"Scraper error: {str(e)}")

    return render_template("index.html", data=scraped_data, error=None)


@app.route("/export")
def export():
    global scraped_data

    if not scraped_data:
        return "No data to export. Run a search first.", 400

    df = pd.DataFrame(scraped_data)

    file_path = "exports/leads.xlsx"
    df.to_excel(file_path, index=False)

    return send_file(file_path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)