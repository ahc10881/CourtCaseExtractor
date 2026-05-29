from flask import (
    Flask,
    render_template,
    request,
    send_file,
    flash,
    redirect,
    url_for
)

import os
import re
import subprocess
import requests

from datetime import datetime

from reportlab.lib.utils import simpleSplit
from reportlab.lib.pagesizes import legal
from reportlab.pdfgen import canvas
from reportlab.lib import colors


app = Flask(__name__)
app.secret_key = "secret-key"

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)


# ============================================================
# PDF SLIP GENERATION
# ============================================================

def generate_slips(slips_data, output):

    page_width, page_height = legal
    c = canvas.Canvas(output, pagesize=legal)

    cols = 2
    rows = 5

    slip_width = page_width / cols
    slip_height = page_height / rows

    font_bold = "Times-Bold"
    font_normal = "Times-Roman"

    slip_index = 0

    for entry in slips_data:

        row = (slip_index // cols) % rows
        col = slip_index % cols

        if slip_index > 0 and slip_index % (cols * rows) == 0:
            c.showPage()

        x = col * slip_width
        y = page_height - (row + 1) * slip_height

        c.setStrokeColor(colors.black)
        c.rect(x + 5, y + 5, slip_width - 10, slip_height - 10)

        tx = x + slip_width / 2
        ty = y + slip_height - 50

        c.setFont(font_normal, 18)
        c.drawCentredString(tx, ty, "CRLA 2006–2010")

        ty -= 40

        c.setFont(font_bold, 24)
        c.drawCentredString(tx, ty, f"Court No.: {entry['court']}")

        ty -= 40

        c.drawCentredString(tx, ty, f"Sl. No.: {entry['sl_no']}")

        ty -= 40

        c.setFont(font_normal, 16)
        c.drawCentredString(tx, ty, f"Date: {entry['date']}")

        bottom_text = entry["main_case"]

        if entry["with_cases"]:
            bottom_text += " + " + " + ".join(entry["with_cases"])

        c.setFont(font_normal, 6)

        lines = simpleSplit(
            bottom_text,
            font_normal,
            6,
            slip_width - 20
        )

        for i, line in enumerate(lines):
            c.drawString(x + 10, y + 10 + i * 8, line)

        slip_index += 1

    c.save()


# ============================================================
# PDF TEXT EXTRACTION
# ============================================================

def extract_text(pdf_path):

    txt_path = pdf_path.replace(".pdf", "_temp.txt")

    subprocess.run(
        ["pdftotext", "-layout", pdf_path, txt_path],
        check=True
    )

    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    os.remove(txt_path)

    return text


# ============================================================
# REMOVE DUPLICATE JUDGES
# ============================================================

def dedupe_judges(jlist):

    seen = set()
    cleaned = []

    for j in jlist:

        key = re.sub(r"\s+", " ", j.strip().lower())

        if key not in seen:
            seen.add(key)
            cleaned.append(j)

    return cleaned


# ============================================================
# PARSE COURTWISE DATA
# ============================================================

def extract_courtwise(text):

    date_re = re.compile(r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})")

    court_re = re.compile(
        r"(?i)Court\s*No\.?\s*[:-]?\s*(\d+)"
    )

    judge_re = re.compile(
        r"(?i)^\s*hon[’'`]?ble.*"
    )

    line_re = re.compile(
        r"^\s*(\d+)\s+(.*?)\s+((?:CRLA|JAPL|CRLAD|JAPLD)/\d+/(2006|2007|2008|2009|2010))"
    )

    with_case_re = re.compile(
        r"(?:CRLA|CRLR|GOVA|CRLAD|JAPL|C372)/\d+/\d{4}"
    )

    sr_re = re.compile(r"^\s*(\d+)\s+")

    with_token_re = re.compile(r"\bWITH\b", re.I)

    courts = {}

    current_court = None
    current_date = None

    lines = text.split("\n")

    i = 0

    while i < len(lines):

        line = lines[i].strip()

        d = date_re.search(line)

        if d:
            current_date = d.group(1)

        c = court_re.search(line)

        if c:

            current_court = f"Court {c.group(1)}"

            courts.setdefault(current_court, {
                "date": current_date,
                "judge": [],
                "cases": []
            })

            j = i + 1

            while j < len(lines):

                nxt = lines[j].strip()

                if judge_re.match(nxt):

                    courts[current_court]["judge"].append(nxt)

                    courts[current_court]["judge"] = dedupe_judges(
                        courts[current_court]["judge"]
                    )

                    j += 1

                else:
                    break

        if not current_court:
            i += 1
            continue

        m = line_re.match(line)

        if not m:
            i += 1
            continue

        sr = m.group(1)
        listing = m.group(2).strip()
        main_case = m.group(3)

        with_list = []

        seen = set()

        pending = False

        j = i + 1

        while j < len(lines):

            nxt = lines[j].strip()

            if sr_re.match(nxt):
                break

            if with_token_re.search(nxt):
                pending = True

            if pending:

                found = with_case_re.findall(nxt)

                for f in found:

                    case = re.search(
                        r"(CRLA|CRLR|GOVA|CRLAD|JAPL|C372)/\d+/\d{4}",
                        nxt
                    )

                    if case:

                        case_no = case.group(0)

                        if case_no not in seen and case_no != main_case:

                            seen.add(case_no)
                            with_list.append(case_no)

                if "WITH" not in nxt.upper() and not with_case_re.search(nxt):
                    pending = False

            j += 1

        courts[current_court]["cases"].append({
            "sr_no": sr,
            "listing": listing,
            "main_case": main_case,
            "with_cases": with_list
        })

        i = j

    return courts


# ============================================================
# SUMMARY
# ============================================================

def prepare_summary(courts):

    year_count = {
        "2006": 0,
        "2007": 0,
        "2008": 0,
        "2009": 0,
        "2010": 0
    }

    court_count = {}

    for court, block in courts.items():

        total_cases = len(block["cases"])

        if total_cases == 0:
            continue

        court_count[court] = total_cases

        for case in block["cases"]:

            m = re.search(
                r"/(2006|2007|2008|2009|2010)$",
                case["main_case"]
            )

            if m:
                year_count[m.group(1)] += 1

    return year_count, court_count


# ============================================================
# HOME
# ============================================================

@app.route("/", methods=["GET", "POST"])
def home():

    if request.method == "POST":

        pdf_file = request.files.get("pdf")
        pdf_url = request.form.get("pdf_url", "").strip()

        pdf_path = None

        # ====================================================
        # FILE UPLOAD
        # ====================================================

        if pdf_file and pdf_file.filename:

            filename = pdf_file.filename

            if not filename.lower().endswith(".pdf"):

                flash("Only PDF files allowed")
                return redirect(url_for("home"))

            pdf_path = os.path.join(
                UPLOAD_FOLDER,
                filename
            )

            pdf_file.save(pdf_path)

        # ====================================================
        # URL DOWNLOAD
        # ====================================================

        elif pdf_url:

            if not pdf_url.lower().endswith(".pdf"):

                flash("URL must point to PDF file")
                return redirect(url_for("home"))

            filename = f"downloaded_{int(datetime.now().timestamp())}.pdf"

            pdf_path = os.path.join(
                UPLOAD_FOLDER,
                filename
            )

            try:

                response = requests.get(
                    pdf_url,
                    timeout=30
                )

                with open(pdf_path, "wb") as f:
                    f.write(response.content)

            except Exception as e:

                flash(f"Error downloading PDF: {e}")
                return redirect(url_for("home"))

        else:

            flash("Upload PDF or enter URL")
            return redirect(url_for("home"))

        # ====================================================
        # PROCESS PDF
        # ====================================================

        try:

            text = extract_text(pdf_path)

            courts = extract_courtwise(text)

            slips_data = []

            for court, block in courts.items():

                for case in block["cases"]:

                    slips_data.append({
                        "court": court.replace("Court ", ""),
                        "sl_no": case["sr_no"],
                        "date": block["date"],
                        "main_case": case["main_case"],
                        "with_cases": case["with_cases"]
                    })

            courtlist_date = datetime.now().strftime("%d-%m-%Y")

            output_name = f"SLIPS_{courtlist_date}.pdf"

            output_path = os.path.join(
                OUTPUT_FOLDER,
                output_name
            )

            generate_slips(
                slips_data,
                output_path
            )

            year_count, court_count = prepare_summary(courts)

            return render_template(
                "result.html",
                courts=courts,
                year_count=year_count,
                court_count=court_count,
                pdf_name=output_name
            )

        except Exception as e:

            flash(str(e))
            return redirect(url_for("home"))

    return render_template("index.html")


# ============================================================
# DOWNLOAD
# ============================================================

@app.route("/download/<filename>")
def download(filename):

    path = os.path.join(
        OUTPUT_FOLDER,
        filename
    )

    return send_file(
        path,
        as_attachment=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    app.run(debug=True)
