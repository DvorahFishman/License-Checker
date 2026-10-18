def verify_license_in_gov_site(id_number):
    url = "https://dataquery.labor.gov.il/businessLicense/s/#"
    
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless=new') 
    
    options.add_argument('--no-sandbox') 
    options.add_argument('--disable-dev-shm-usage') 
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
        # התיקון הקריטי לפי תמונת המסך שלך:
        if "לא מצאנו תוצאות עבור החיפוש שלך" in page_source:
            return False
        else:
            return True
            
    except Exception:
        return False
    finally:
        time.sleep(3) # השהייה קצרה שתספיקי לראות לפני שנסגר
        driver.quit()
