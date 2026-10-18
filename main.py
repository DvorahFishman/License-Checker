import os
import re
import time
from pdfminer.high_level import extract_text
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import streamlit as st




def extract_id_from_pdf(pdf_path):
    """שולף תעודת זהות בת 9 ספרות מקובץ ה-PDF"""
    try:
        text = extract_text(pdf_path)
        ids = re.findall(r'\b\d{9}\b', text)
        if ids:
            return ids[0]
        return None
    except Exception as e:
        st.error(f"שגיאה בקריאת קובץ ה-PDF: {e}")
        return None




def verify_license_in_gov_site(pdf_path):
    """מבצע אימות של תעודת הזהות מול אתר הממשלה באמצעות Selenium בערך Headless"""
    id_number = extract_id_from_pdf(pdf_path)
    if not id_number:
        return "❌ שגיאה: לא נמצאה תעודת זהות בת 9 ספרות בקובץ ה-PDF."


    st.info(f"🔍 חולצה תעודת זהות: **{id_number}**. מתחיל אימות מול האתר...")


    # הגדרות הרצה שקטה (Headless) עבור Selenium
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")


    driver = None
    try:
        driver = webdriver.Chrome(options=chrome_options)
        url = "https://www.gov.il/he/service/licensed-architects-and-engineers-registry"
        driver.get(url)


        wait = WebDriverWait(driver, 15)


        # 1. בחירת "מורשה להיתר"
        license_type_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//label[contains(., 'מורשה להיתר')]"))
        )
        license_type_button.click()
        time.sleep(1)


        # 2. בחירת "תעודת זהות"
        id_radio_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//label[contains(., 'תעודת זהות')]"))
        )
