import html
import os
import re
import shutil
import tempfile
from datetime import date, datetime
from pdfminer.high_level import extract_pages, extract_text
from pdfminer.layout import LTChar, LTContainer
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import streamlit as st


# שאילתת פנקס המהנדסים והאדריכלים של משרד העבודה
# (הכתובת הישנה ב-gov.il מפנה היום לדף שגיאה)
REGISTRY_URL = "https://data.labor.gov.il/EngineersAndArchitects.aspx"
NOT_FOUND_TEXT = "לא נמצאו נתונים"

# האתר חוסם דפדפן שמזדהה כ-HeadlessChrome, לכן מזדהים כ-Chrome רגיל
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# נתיבים אפשריים לדפדפן ול-driver בשרת Linux (Streamlit Cloud / Debian)
CHROMIUM_BINARY_CANDIDATES = ["/usr/bin/chromium", "/usr/bin/chromium-browser"]
CHROMEDRIVER_CANDIDATES = ["/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"]


def find_first_existing(paths):
    """מחזיר את הנתיב הראשון שקיים בפועל, או None"""
    for path in paths:
        if os.path.exists(path):
            return path
    return None


def is_valid_israeli_id(id_number):
    """בדיקת ספרת ביקורת של תעודת זהות ישראלית"""
    if not re.fullmatch(r"\d{9}", id_number):
        return False
    total = 0
    for i, ch in enumerate(id_number):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        total += n - 9 if n > 9 else n
    return total % 10 == 0


def iter_chars(layout_obj):
    if isinstance(layout_obj, LTChar):
        yield layout_obj
    elif isinstance(layout_obj, LTContainer):
        for child in layout_obj:
            yield from iter_chars(child)


def extract_number_runs(pdf_path):
    """מחזיר את כל רצפי הספרות בקובץ לפי מיקומם בדף.
    בתעודה הספרות של מס' רישיון, ת.ז ותאריך מודבקות בטקסט הגולמי
    ('109410025084252...'), לכן מפרידים לפי המרחק בין התווים."""
    runs = []
    for page in extract_pages(pdf_path):
        digits = [c for c in iter_chars(page) if c.get_text().isdigit()]
        digits.sort(key=lambda c: (-round(c.y0), c.x0))
        current, last = "", None
        for c in digits:
            same_run = (last is not None and round(c.y0) == round(last.y0)
                        and abs(c.x0 - last.x1) < c.width * 0.5)
            if not same_run and current:
                runs.append(current)
                current = ""
            current += c.get_text()
            last = c
        if current:
            runs.append(current)
    return runs


def extract_certificate_fields(pdf_path):
    """שולף ת.ז (9 ספרות עם ספרת ביקורת תקינה) ואת כל המספרים שבתעודה"""
    runs = extract_number_runs(pdf_path)
    # גיבוי: קבצים שבהם הטקסט כן מופרד כרגיל
    runs += re.findall(r"(?<!\d)\d+(?!\d)", extract_text(pdf_path))

    id_number = next((r for r in runs if is_valid_israeli_id(r)), None)
    return id_number, set(runs)


def build_driver():
    """יוצר Chrome WebDriver - מזהה אוטומטית אם רצים בענן (chromium מותקן) או מקומית"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_argument(f"user-agent={USER_AGENT}")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--lang=he-IL")

    chromium_binary = find_first_existing(CHROMIUM_BINARY_CANDIDATES)
    chromedriver_path = find_first_existing(CHROMEDRIVER_CANDIDATES) or shutil.which("chromedriver")

    if chromium_binary and chromedriver_path:
        # סביבת ענן: chromium ו-chromium-driver הותקנו דרך packages.txt
        chrome_options.binary_location = chromium_binary
        return webdriver.Chrome(service=Service(chromedriver_path), options=chrome_options)

    # סביבה מקומית: Selenium Manager מאתר את Chrome המותקן ומוריד driver מתאים
    return webdriver.Chrome(options=chrome_options)


def search_registry(id_number):
    """מחפש ת.ז בפנקס המהנדסים והאדריכלים.
    מחזיר dict: found, name, rows (רשימת dict לפי כותרות הטבלה), error, screenshot"""
    result = {"found": False, "name": None, "rows": [], "error": None, "screenshot": None}
    driver = None
    step = "הפעלת הדפדפן"
    try:
        driver = build_driver()

        step = "טעינת אתר הפנקס"
        driver.get(REGISTRY_URL)
        wait = WebDriverWait(driver, 20)
        wait.until(lambda d: d.find_elements(By.ID, "txtID"))

        step = "הקלדת תעודת הזהות"
        driver.find_element(By.ID, "Rdb_ID").click()
        input_field = driver.find_element(By.ID, "txtID")
        input_field.clear()
        input_field.send_keys(id_number)

        step = "לחיצה על חיפוש"
        driver.find_element(By.ID, "imgBtnSearch").click()

        step = "המתנה לתוצאה"

        def result_ready(d):
            validation = d.find_elements(By.ID, "lblValidation")
            if validation and validation[0].text.strip():
                return True
            return bool(d.find_elements(By.CSS_SELECTOR, "#tdResult table.tbl2"))

        wait.until(result_ready)

        validation = driver.find_elements(By.ID, "lblValidation")
        validation_text = validation[0].text.strip() if validation else ""
        if NOT_FOUND_TEXT in validation_text:
            return result
        if validation_text:
            result["error"] = f"האתר החזיר הודעה: {validation_text}"
            return result

        step = "קריאת טבלת התוצאות"
        title = driver.find_elements(By.CSS_SELECTOR, "#tdResult .tdTitle")
        result["name"] = title[0].text.strip() if title else None
        headers = [h.text.strip() for h in driver.find_elements(By.CSS_SELECTOR, "#tdResult .tbl2 .tdHeader")]
        for tr in driver.find_elements(By.CSS_SELECTOR, "#tdResult .tbl2 tr"):
            cells = [td.text.strip() for td in tr.find_elements(By.CSS_SELECTOR, "td.td2")]
            if cells:
                result["rows"].append(dict(zip(headers, cells)))
        result["found"] = True
        return result

    except Exception as e:
        # הודעות של Selenium כוללות stacktrace ארוך - מציגים רק את ה-msg עצמו
        error_text = (getattr(e, "msg", None) or str(e) or "").strip().splitlines()
        error_text = error_text[0] if error_text else ""
        result["error"] = f"שגיאה בשלב: {step} ({type(e).__name__}) {error_text}"
        if driver:
            try:
                result["error"] += f"\n\nכותרת הדף: {driver.title or '(ריק)'}\n\nכתובת: {driver.current_url}"
                result["screenshot"] = driver.get_screenshot_as_png()
                result["blocked"] = "לא ניתן להציג את הדף" in driver.page_source
            except Exception:
                pass
        return result
    finally:
        if driver:
            driver.quit()


def parse_date(text):
    try:
        return datetime.strptime(text, "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return None


# ---------- עיצוב ותצוגה ----------

PAGE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700;800&display=swap');

html, body, .stApp, .stMarkdown, button, input, label {
    font-family: 'Heebo', sans-serif !important;
}
.stApp { direction: rtl; }
.stMarkdown, .stAlert, [data-testid="stFileUploader"], [data-testid="stExpander"] { text-align: right; }
#MainMenu, footer { visibility: hidden; }
.block-container { max-width: 760px; padding-top: 2.5rem; }

.hero {
    background: linear-gradient(135deg, #1F3A68 0%, #2E5A9C 100%);
    color: #fff; border-radius: 18px; padding: 28px 30px; margin-bottom: 26px;
    box-shadow: 0 10px 30px rgba(31, 58, 104, 0.18);
}
.hero h1 { color: #fff; font-size: 1.85rem; font-weight: 800; margin: 0 0 6px 0; padding: 0; }
.hero p { color: #DCE6F5; font-size: 1.02rem; margin: 0; }

.steps { display: flex; gap: 10px; margin: 0 0 22px 0; flex-wrap: wrap; }
.step {
    flex: 1 1 150px; background: #F4F7FB; border: 1px solid #E1E8F2; border-radius: 12px;
    padding: 12px 14px; font-size: 0.93rem; color: #33445C;
}
.step b { display: block; color: #1F3A68; font-size: 0.8rem; margin-bottom: 2px; }

.result-card {
    border-radius: 16px; padding: 22px 24px; margin: 18px 0 12px 0; border: 1px solid;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.05);
}
.result-card .icon { font-size: 2.1rem; line-height: 1; }
.result-card h3 { margin: 8px 0 4px 0; font-size: 1.35rem; font-weight: 800; padding: 0; color: inherit; }
.result-card p { margin: 0; font-size: 1rem; }
.result-card.success { background: #EEF9F1; border-color: #BFE5CA; color: #17552B; }
.result-card.error   { background: #FDF0F0; border-color: #F2C4C4; color: #7A1E1E; }
.result-card.warning { background: #FFF8EA; border-color: #F3DDA8; color: #6B4A00; }

.details {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;
    margin-top: 16px;
}
.details div { background: rgba(255, 255, 255, 0.75); border-radius: 10px; padding: 10px 12px; }
.details span { display: block; font-size: 0.78rem; opacity: 0.75; }
.details strong { font-size: 1.02rem; }

.footnote { color: #7A8699; font-size: 0.82rem; text-align: center; margin-top: 36px; }

div.stButton > button {
    width: 100%; border-radius: 12px; padding: 0.7rem 1rem; font-size: 1.05rem; font-weight: 700;
}
</style>
"""

HERO_HTML = """
<div class="hero">
  <h1>📜 אימות תעודת מורשה להיתר</h1>
  <p>בדיקה מהירה של תעודה מול פנקס המהנדסים והאדריכלים של משרד העבודה</p>
</div>
<div class="steps">
  <div class="step"><b>שלב 1</b>מעלים את קובץ ה-PDF של התעודה</div>
  <div class="step"><b>שלב 2</b>המערכת קוראת את מספר ת.ז והרישיון</div>
  <div class="step"><b>שלב 3</b>הפרטים נבדקים מול הפנקס הרשמי</div>
</div>
"""


def render_card(kind, icon, title, text, details=None):
    """כרטיס תוצאה מעוצב. details: רשימת (כותרת, ערך)"""
    details_html = ""
    if details:
        cells = "".join(
            f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
            for label, value in details
        )
        details_html = f'<div class="details">{cells}</div>'
    st.markdown(
        f'<div class="result-card {kind}"><div class="icon">{icon}</div>'
        f"<h3>{html.escape(title)}</h3><p>{html.escape(text)}</p>{details_html}</div>",
        unsafe_allow_html=True,
    )


def show_technical_details(search):
    with st.expander("פרטים טכניים (למי שמתחזק את המערכת)"):
        st.code(search["error"], language=None)
        if search["screenshot"]:
            st.image(search["screenshot"], caption="מה שהדפדפן ראה ברגע התקלה")


def show_verdict(search, id_number, pdf_numbers):
    """מציג תוצאה סופית: נמצא + מס' התעודה מה-PDF תואם + בתוקף"""
    if search["error"]:
        if search.get("blocked"):
            render_card("warning", "🛡️", "אתר משרד העבודה חסם את הבדיקה",
                        "האתר לא מאפשר כרגע גישה אוטומטית מהשרת שלנו. "
                        "אפשר לנסות שוב בעוד כמה דקות, או לבדוק ידנית באתר הפנקס.")
        else:
            render_card("warning", "🔌", "לא הצלחנו להשלים את הבדיקה",
                        "נראה שאתר משרד העבודה איטי או לא זמין כרגע. "
                        "זו לא בעיה בתעודה — כדאי לנסות שוב בעוד כמה דקות.")
        st.link_button("לבדיקה ידנית באתר הפנקס ↗", REGISTRY_URL)
        show_technical_details(search)
        return

    if not search["found"]:
        render_card("error", "❌", "התעודה לא נמצאה בפנקס",
                    "מספר תעודת הזהות שבתעודה לא רשום בפנקס המהנדסים והאדריכלים. "
                    "מומלץ לא להסתמך על התעודה ולבקש הבהרה מבעליה.",
                    [("תעודת זהות", id_number)])
        return

    name = search["name"] or "-"
    matching = [r for r in search["rows"] if r.get("מספר תעודה") in pdf_numbers]

    if not matching:
        render_card("warning", "⚠️", "נדרשת בדיקה ידנית",
                    "תעודת הזהות רשומה בפנקס, אבל מספר הרישיון שבתעודה לא תואם את הרשום במאגר. "
                    "ייתכן שהתעודה שייכת לאדם אחר או שנערכה.",
                    [("שם במאגר", name), ("תעודת זהות", id_number)])
    else:
        expiry_dates = [parse_date(r.get("תאריך תוקף")) for r in matching]
        valid = any(d is None or d >= date.today() for d in expiry_dates)
        row = matching[0]
        details = [
            ("שם במאגר", name),
            ("תעודת זהות", id_number),
            ("מספר רישיון", row.get("מספר תעודה", "-")),
            ("ענף", row.get("ענף", "-")),
            ("בתוקף עד", row.get("תאריך תוקף", "-")),
        ]
        if valid:
            render_card("success", "✅", "התעודה אומתה בהצלחה",
                        "הפרטים בתעודה תואמים לרישום בפנקס המהנדסים והאדריכלים, והרישיון בתוקף.",
                        details)
        else:
            render_card("error", "⏳", "תוקף הרישיון הסתיים",
                        "הפרטים רשומים בפנקס, אבל תוקף הרישיון הסתיים. "
                        "ייתכן שתשלום האגרה עוד לא עודכן — כדאי לבדוק שוב בעוד כחודש.",
                        details)

    if search["rows"]:
        with st.expander("כל הרישומים שנמצאו בפנקס"):
            st.table(search["rows"])


def main():
    st.set_page_config(page_title="אימות תעודת מורשה להיתר", page_icon="📜", layout="centered")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    st.markdown(HERO_HTML, unsafe_allow_html=True)

    uploaded_file = st.file_uploader("קובץ התעודה (PDF)", type=["pdf"],
                                     help="יש להעלות את קובץ ה-PDF המקורי של התעודה, לא צילום מסך.")

    if not st.button("🔍 בדיקת התעודה", type="primary", width="stretch",
                     disabled=uploaded_file is None):
        st.markdown('<div class="footnote">⏱️ הבדיקה לוקחת בדרך כלל כ-10 שניות · '
                    "מתבצעת מול מאגר ציבורי של משרד העבודה · הקובץ לא נשמר במערכת</div>",
                    unsafe_allow_html=True)
        return

    with st.status("בודקים את התעודה... התהליך עשוי לקחת כ-10 שניות ⏱️", expanded=True) as status:
        st.write("📄 קוראים את פרטי התעודה מהקובץ...")
        # שמירה לקובץ זמני ייחודי - בטוח גם כשכמה משתמשים מריצים במקביל
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(uploaded_file.getbuffer())
            temp_path = tmp.name
        try:
            id_number, pdf_numbers = extract_certificate_fields(temp_path)
        except Exception:
            id_number, pdf_numbers = None, set()
        finally:
            os.remove(temp_path)

        if not id_number:
            status.update(label="לא הצלחנו לקרוא את התעודה", state="error", expanded=False)
        else:
            st.write(f"🔢 נמצאה תעודת זהות: **{id_number}**")
            st.write("🌐 בודקים מול פנקס המהנדסים והאדריכלים, זה עשוי לקחת כ-10 שניות...")
            search = search_registry(id_number)
            if search["error"]:
                status.update(label="הבדיקה לא הושלמה", state="error", expanded=False)
            else:
                status.update(label="הבדיקה הושלמה", state="complete", expanded=False)

    if not id_number:
        render_card("warning", "📄", "לא מצאנו מספר תעודת זהות בקובץ",
                    "כדאי לוודא שזה קובץ ה-PDF המקורי שהתקבל ממשרד העבודה. "
                    "קובץ סרוק או צילום מסך לא מכיל טקסט שאפשר לקרוא.")
        return

    show_verdict(search, id_number, pdf_numbers)


if __name__ == "__main__":
    main()
