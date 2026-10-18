import html
import os
import re
import shutil
import tempfile
from pdfminer.high_level import extract_pages, extract_text
from pdfminer.layout import LTChar, LTContainer
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import streamlit as st


# שאילתת "מורשה להיתר" של אגף אסדרת עיסוקים במשרד העבודה.
# האתר מציג רק מורשי היתר שתעודתם בתוקף (שונה מפנקס המהנדסים הכללי).
PERMIT_URL = "https://dataquery.labor.gov.il/businessLicense/s/"

# האתר חוסם דפדפן שמזדהה כ-HeadlessChrome, לכן מזדהים כ-Chrome רגיל
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# נתיבים אפשריים לדפדפן ול-driver בשרת Linux (Streamlit Cloud / Debian)
CHROMIUM_BINARY_CANDIDATES = ["/usr/bin/chromium", "/usr/bin/chromium-browser"]
CHROMEDRIVER_CANDIDATES = ["/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver"]

# שדה הזנת ת.ז באתר הוא רכיב Salesforce בתוך shadow DOM, ולכן ניגשים אליו דרך JavaScript
FIND_VISIBLE_TEXT_INPUT_JS = """
function walk(root) {
  for (const el of root.querySelectorAll('*')) {
    if (el.tagName === 'INPUT' && el.type === 'text' && el.offsetParent !== null) return el;
    if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; }
  }
  return null;
}
return walk(document);
"""

FIND_SEARCH_BUTTON_JS = """
function walk(root) {
  for (const el of root.querySelectorAll('button')) {
    if (el.innerText.trim() === 'חפש' && el.offsetParent !== null) return el;
  }
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) { const r = walk(el.shadowRoot); if (r) return r; }
  }
  return null;
}
return walk(document);
"""

# הרכיב מתעלם מהקלדה רגילה (שדה עם מסכה), לכן מציבים ערך ושולחים את האירועים שהוא מאזין להם
SET_VALUE_JS = """
const el = arguments[0];
el.focus();
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
setter.call(el, arguments[1]);
for (const name of ['input', 'change', 'blur']) {
  el.dispatchEvent(new Event(name, {bubbles: true, composed: true}));
}
"""


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
        digits.sort(key=lambda c: (-round(c.y0 / 3), c.x0))
        current, last = "", None
        for c in digits:
            same_run = (last is not None and abs(c.y0 - last.y0) < 3
                        and abs(c.x0 - last.x1) < c.width * 0.5)
            if not same_run and current:
                runs.append(current)
                current = ""
            current += c.get_text()
            last = c
        if current:
            runs.append(current)
    return runs


def normalize_id(text):
    """מחזיר ת.ז בת 9 ספרות אם הקלט תקין (כולל ת.ז ישנה בת 8 ספרות בלי 0 מוביל), אחרת None"""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 8:
        digits = "0" + digits
    return digits if is_valid_israeli_id(digits) else None


def extract_certificate_fields(pdf_path):
    """שולף ת.ז (עם ספרת ביקורת תקינה) ואת כל המספרים שבתעודה"""
    runs = extract_number_runs(pdf_path)
    # גיבוי: קבצים שבהם הטקסט כן מופרד כרגיל
    runs += re.findall(r"(?<!\d)\d+(?!\d)", extract_text(pdf_path))

    # עדיפות ל-9 ספרות, ואחר כך ת.ז ישנה בת 8 ספרות
    id_number = (next((r for r in runs if len(r) == 9 and is_valid_israeli_id(r)), None)
                 or next((normalize_id(r) for r in runs if len(r) == 8 and normalize_id(r)), None))
    if not id_number:
        # PDF עברי שמאחסן ספרות בסדר חזותי הפוך
        id_number = next((r[::-1] for r in runs if len(r) == 9 and is_valid_israeli_id(r[::-1])), None)
    return id_number, set(runs)


def certificate_number_in_pdf(cert_number, pdf_numbers):
    """מספר התעודה באתר ('01-002-0000000121') מול רצפי הספרות שב-PDF.
    משווים את הקטע האחרון, בלי אפסים מובילים."""
    last_part = cert_number.split("-")[-1].lstrip("0")
    return bool(last_part) and any(r.lstrip("0") == last_part and len(r) >= 4 for r in pdf_numbers)


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


def search_permit_holder(id_number):
    """מחפש ת.ז ברשימת המורשים להיתר של משרד העבודה.
    מחזיר dict: found, name, cert_number, issue_date, error, screenshot, blocked"""
    result = {"found": False, "name": None, "cert_number": None, "issue_date": None,
              "error": None, "screenshot": None, "blocked": False}
    driver = None
    step = "הפעלת הדפדפן"
    try:
        driver = build_driver()

        step = "טעינת אתר משרד העבודה"
        driver.get(PERMIT_URL)
        wait = WebDriverWait(driver, 30)
        permit_label_xpath = "//input[@value='ROO']/following-sibling::label"
        wait.until(lambda d: d.find_elements(By.XPATH, permit_label_xpath))

        step = "בחירת 'מורשה להיתר'"
        driver.find_element(By.XPATH, permit_label_xpath).click()

        step = "בחירת חיפוש לפי תעודת זהות"
        id_label = wait.until(lambda d: next(
            (el for el in d.find_elements(By.XPATH, "//input[@value='TZId']/following-sibling::label")
             if el.is_displayed()), None))
        id_label.click()

        step = "הזנת תעודת הזהות"
        field = wait.until(lambda d: d.execute_script(FIND_VISIBLE_TEXT_INPUT_JS))
        driver.execute_script(SET_VALUE_JS, field, id_number)

        step = "לחיצה על חיפוש"
        button = wait.until(lambda d: d.execute_script(FIND_SEARCH_BUTTON_JS))
        button.click()

        step = "המתנה לתוצאה"

        def body_text(d):
            return d.find_element(By.TAG_NAME, "body").text

        def result_ready(d):
            # כותרת "תוצאות חיפוש" מופיעה לפני התוצאה עצמה, לכן ממתינים לתעודה או ל-2 הודעות "לא מצאנו"
            # (אחת לכל סוג עיסוק באתר)
            t = body_text(d)
            if "תוצאות חיפוש" not in t:
                return "שגיאה:" in t
            return "מספר תעודה:" in t or t.count("לא מצאנו תוצאות") >= 2

        wait.until(result_ready)
        text = body_text(driver)

        if "שגיאה:" in text and "תוצאות חיפוש" not in text:
            result["error"] = "האתר דחה את החיפוש: " + text.split("שגיאה:", 1)[1].splitlines()[0].strip()
            return result

        step = "קריאת התוצאה"
        cert = re.search(r"מספר תעודה:\s*([\d\-]+)", text)
        cert_type = re.search(r"סוג תעודה:\s*(.+)", text)
        if not cert or not cert_type or "היתר" not in cert_type.group(1):
            return result  # "לא מצאנו תוצאות עבור החיפוש שלך"

        name = re.search(r"תעודת מורשה היתרים של\s*(.+)", text)
        issued = re.search(r"תאריך הנפקת התעודה:\s*([\d/]+)", text)
        result.update(found=True,
                      cert_number=cert.group(1),
                      name=name.group(1).strip() if name else None,
                      issue_date=issued.group(1) if issued else None)
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
  <p>בדיקה מהירה של התעודה מול רשימת המורשים להיתר של משרד העבודה</p>
</div>
<div class="steps">
  <div class="step"><b>שלב 1</b>מעלים את קובץ ה-PDF של התעודה</div>
  <div class="step"><b>שלב 2</b>המערכת קוראת את מספר ת.ז והתעודה</div>
  <div class="step"><b>שלב 3</b>הפרטים נבדקים מול הרשימה הרשמית</div>
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
    """מציג תוצאה סופית: נמצא ברשימת המורשים + מס' התעודה מה-PDF תואם"""
    if search["error"]:
        if search.get("blocked"):
            render_card("warning", "🛡️", "אתר משרד העבודה חסם את הבדיקה",
                        "האתר לא מאפשר כרגע גישה אוטומטית מהשרת שלנו. "
                        "אפשר לנסות שוב בעוד כמה דקות, או לבדוק ידנית באתר משרד העבודה.")
        else:
            render_card("warning", "🔌", "לא הצלחנו להשלים את הבדיקה",
                        "נראה שאתר משרד העבודה איטי או לא זמין כרגע. "
                        "זו לא בעיה בתעודה — כדאי לנסות שוב בעוד כמה דקות.")
        st.link_button("לבדיקה ידנית באתר משרד העבודה ↗", PERMIT_URL)
        show_technical_details(search)
        return

    if not search["found"]:
        render_card("error", "❌", "לא נמצאה תעודת מורשה להיתר בתוקף",
                    "מספר תעודת הזהות שבתעודה לא מופיע ברשימת המורשים להיתר של משרד העבודה. "
                    "הרשימה כוללת רק מורשים שתעודתם בתוקף, ולכן ייתכן גם שהתעודה פגה או שהאגרה לא שולמה. "
                    "מומלץ לא להסתמך על התעודה ולבקש הבהרה מבעליה.",
                    [("תעודת זהות", id_number)])
        return

    details = [
        ("שם במאגר", search["name"] or "-"),
        ("תעודת זהות", id_number),
        ("מספר תעודה", search["cert_number"]),
        ("תאריך הנפקה", search["issue_date"] or "-"),
    ]
    if pdf_numbers is None:
        render_card("success", "✅", "נמצא ברשימת המורשים להיתר",
                    "תעודת הזהות שהוקלדה רשומה ברשימת המורשים להיתר של משרד העבודה, והתעודה בתוקף. "
                    "שימו לב: נבדקה תעודת הזהות בלבד, ללא השוואה לקובץ תעודה.",
                    details)
    elif certificate_number_in_pdf(search["cert_number"], pdf_numbers):
        render_card("success", "✅", "התעודה אומתה בהצלחה",
                    "הפרטים בתעודה תואמים לרישום ברשימת המורשים להיתר של משרד העבודה, והתעודה בתוקף.",
                    details)
    else:
        render_card("warning", "⚠️", "נדרשת בדיקה ידנית",
                    "תעודת הזהות רשומה ברשימת המורשים להיתר, אבל מספר התעודה שבקובץ לא תואם את הרשום "
                    "במאגר. ייתכן שזו תעודה ישנה יותר או שהקובץ נערך.",
                    details)


def main():
    st.set_page_config(page_title="אימות תעודת מורשה להיתר", page_icon="📜", layout="centered")
    st.markdown(PAGE_CSS, unsafe_allow_html=True)
    st.markdown(HERO_HTML, unsafe_allow_html=True)

    uploaded_file = st.file_uploader("קובץ התעודה (PDF)", type=["pdf"],
                                     help="יש להעלות את קובץ ה-PDF המקורי של התעודה, לא צילום מסך.")

    with st.expander("אפשר גם להקליד מספר תעודת זהות ידנית"):
        manual_id = st.text_input("מספר תעודת זהות", max_chars=9, placeholder="9 ספרות, כולל ספרת ביקורת",
                                  help="אם מעלים קובץ וגם מקלידים מספר, ייבדק המספר שהוקלד.")

    if not st.button("🔍 בדיקת התעודה", type="primary", width="stretch",
                     disabled=uploaded_file is None and not manual_id.strip()):
        st.markdown('<div class="footnote">⏱️ הבדיקה לוקחת בדרך כלל כ-10 שניות · '
                    "מתבצעת מול רשימה ציבורית של משרד העבודה · הקובץ לא נשמר במערכת</div>",
                    unsafe_allow_html=True)
        return

    with st.status("בודקים את התעודה... התהליך עשוי לקחת כ-10 שניות ⏱️", expanded=True) as status:
        id_number, pdf_numbers, read_error = None, set(), None
        manual_typed = bool(manual_id.strip())
        if uploaded_file is not None:
            st.write("📄 קוראים את פרטי התעודה מהקובץ...")
            # שמירה לקובץ זמני ייחודי - בטוח גם כשכמה משתמשים מריצים במקביל
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(uploaded_file.getbuffer())
                temp_path = tmp.name
            try:
                id_number, pdf_numbers = extract_certificate_fields(temp_path)
            except Exception as e:
                read_error = f"{type(e).__name__}: {e}"
            finally:
                os.remove(temp_path)
        else:
            pdf_numbers = None  # בדיקה לפי ת.ז בלבד

        if manual_typed:
            # ת.ז שהוקלדה ידנית גוברת על מה שנקרא מהקובץ
            id_number = normalize_id(manual_id)

        if not id_number:
            status.update(label="לא הצלחנו לקרוא את התעודה", state="error", expanded=False)
        else:
            st.write(f"🔢 נמצאה תעודת זהות: **{id_number}**")
            st.write("🌐 בודקים מול רשימת המורשים להיתר, זה עשוי לקחת כ-10 שניות...")
            search = search_permit_holder(id_number)
            if search["error"]:
                status.update(label="הבדיקה לא הושלמה", state="error", expanded=False)
            else:
                status.update(label="הבדיקה הושלמה", state="complete", expanded=False)

    if not id_number and manual_typed:
        render_card("warning", "🔢", "מספר תעודת הזהות שהוקלד לא תקין",
                    "יש לבדוק שהוקלדו 9 ספרות מלאות, כולל ספרת הביקורת בסוף.")
        return

    if not id_number:
        render_card("warning", "📄", "לא מצאנו מספר תעודת זהות בקובץ",
                    "כדאי לוודא שזה קובץ ה-PDF המקורי שהתקבל ממשרד העבודה. "
                    "קובץ סרוק או צילום מסך לא מכיל טקסט שאפשר לקרוא. "
                    "אפשר להקליד את מספר תעודת הזהות ידנית בחלונית שמעל הכפתור.")
        with st.expander("פרטים טכניים (למי שמתחזק את המערכת)"):
            if read_error:
                st.code(f"שגיאה בקריאת ה-PDF: {read_error}", language=None)
            else:
                found = sorted(pdf_numbers or [], key=len, reverse=True)
                st.code("מספרים שנמצאו בקובץ: " + (", ".join(found) if found else "(אין טקסט בקובץ - כנראה סרוק)"),
                        language=None)
        return

    show_verdict(search, id_number, pdf_numbers)


if __name__ == "__main__":
    main()
