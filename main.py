import streamlit as st
import pdfplumber
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import time


st.set_page_config(page_title="אימות תעודות מורשה להיתר", page_icon="🔍", layout="centered")


def extract_id_from_pdf(pdf_file):
    try:
        with pdfplumber.open(pdf_file) as pdf:
            text = ""
            for page in pdf.pages:
                text += page.extract_text()
            
            id_match = re.search(r'\b\d{9}\b', text)
            if id_match:
                return id_match.group(0)
            return None
    except Exception:
        return None


def verify_license_in_gov_site(id_number):
    url = "https://dataquery.labor.gov.il/businessLicense/s/#"
    
    # הגדרות מיוחדות לשרת ענן (לינוקס) - חובה!
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new') # מוסתר
    options.add_argument('--no-sandbox') # עוקף מגבלות אבטחה של שרתים
    options.add_argument('--disable-dev-shm-usage') # מונע קריסות זיכרון בענן
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')
    
    driver = webdriver.Chrome(options=options)
    
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 15)
        
        murshe_radio = wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'מורשה להיתר')]")))
        driver.execute_script("arguments[0].click();", murshe_radio)
        time.sleep(1)


        tz_radio = wait.until(EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'תעודת זהות')]")))
        driver.execute_script("arguments[0].click();", tz_radio)
        time.sleep(1)


        search_box = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[contains(@class, 'vlocity-input')]")))
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", search_box)
        time.sleep(1)
        
        search_box.click()
        time.sleep(0.5)
        
        actions = ActionChains(driver)
        actions.send_keys_to_element(search_box, id_number)
        actions.perform()
        
        time.sleep(0.5)
        driver.execute_script(f"arguments[0].value = '{id_number}';", search_box)
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('change', { bubbles: true }));
            arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));
        """, search_box)
        
        actions.send_keys(Keys.TAB).perform()
        time.sleep(1)


        search_button = wait.until(EC.presence_of_element_located((By.XPATH, "//button[contains(., 'חפש') or contains(., 'חיפוש')]")))
        driver.execute_script("arguments[0].click();", search_button)
        
        time.sleep(5) 
        
        page_source = driver.page_source
        if "לא נמצאו נתונים" in page_source or "אין תוצאות" in page_source or "שגיאה" in page_source:
            return False
        else:
            return True
            
    except Exception:
        return False
    finally:
        driver.quit()


# --- הממשק ---
st.title("מערכת לאימות תעודות - מורשה להיתר 🔍")
st.write("העלי קובץ PDF של תעודת רישום, והמערכת תוודא אוטומטית האם היא רשומה במאגר משרד העבודה.")
st.markdown("---")


uploaded_file = st.file_uploader("בחרי קובץ תעודה (PDF)", type=["pdf"])


if uploaded_file is not None:
    st.info("סורק את הנתונים מהקובץ...")
    id_number = extract_id_from_pdf(uploaded_file)
    
    if not id_number:
        st.error("לא נמצא מספר תעודת זהות תקין בקובץ.")
    else:
        st.success(f"נמצאה תעודת זהות: {id_number}")
        
        if st.button("בדוק במאגר הממשלתי"):
            with st.spinner("מתחבר למאגר משרד העבודה ובודק... (זה עשוי לקחת כ-10 שניות)"):
                is_valid = verify_license_in_gov_site(id_number)
                
            if is_valid:
                st.success("✅ התעודה מאומתת ותקינה! הפרטים נמצאו במאגר משרד העבודה.")
                st.balloons()
            else:
                st.error("❌ התעודה לא נמצאה במאגר! ייתכן שמדובר בזיוף, שהפרטים שגויים, או שהתעודה נפסלה.")