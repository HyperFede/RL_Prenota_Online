from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import StaleElementReferenceException
from selenium.common.exceptions import ElementClickInterceptedException
from datetime import datetime
import time
import sys
import re

DEBUG_MODE = False

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)
from dataObjects import Patient, Prescription, Appointment, SearchPreferences
import data_file
from os import path

def ask_data():
    codice_fiscale = input("Inserisci il codice fiscale: ")

    tessera_sanitaria = input("Inserisci le ultime 5 cifre della tessera sanitaria: ")
    prescription_n = input("Inserisci il codice della ricetta: ")
    
    print ("Inserisci le province in cui vuoi la visita separate da virgola tra le seguenti: BERGAMO, BRESCIA, COMO, CREMONA, LECCO, LODI, MANTOVA, MILANO CITTA', MILANO PROVINCIA, MONZA E DELLA BRIANZA, PAVIA, SONDRIO, VARESE")
    province_input = input("").upper()
    province = [p.strip() for p in province_input.split(',')]
    
    start_date = input("Inserisci la prima data da cui vuoi la visita (gg/mm/aaaa): ")
    end_date = input("Inserisci la data entro cui vuoi la visita (gg/mm/aaaa): ")
    REFRESH_FREQUENCY = int(input("Inserisci ogni quanti secondi riavviare la ricerca se non è stata trovata una data: "))
    dry_run_input = input("Eseguire in modalità dry-run (sicura, senza alterare l'appuntamento)? Y/N: ")
    dry_run = True if dry_run_input.upper() == 'Y' else False

    print("\n")

    patient = Patient(codice_fiscale, tessera_sanitaria)
    prescription = Prescription(prescription_n, patient)
    search_preferences = SearchPreferences(province, start_date, end_date, REFRESH_FREQUENCY, dry_run)

    return prescription, search_preferences


def get_data_from_file():
    patient = Patient(data_file.codice_fiscale, data_file.tessera_sanitaria)
    prescription = Prescription(data_file.prescription_n, patient)
    
    # Handle the case where the older data_file.py only had 'provincia' string instead of 'province' list
    province = getattr(data_file, 'province', [])
    if not province and hasattr(data_file, 'provincia'):
        province = [data_file.provincia]
        
    dry_run = getattr(data_file, 'dry_run', True)
    search_preferences = SearchPreferences(province, data_file.start_date, data_file.end_date, data_file.refresh_frequency, dry_run)

    return prescription, search_preferences


def use_chrome():
    # initialize Chrome webdriver
    options = Options()
    options.add_argument("--log-level=1")
    # keep the browser open after the process has ended, so long as the quit command is not sent to the driver.
    options.add_experimental_option("detach", True)
    driver = webdriver.Chrome(options=options)
    return driver


def use_firefox():
    # initialize Firefox webdriver
    driver = webdriver.Firefox()
    return driver


def handle_initial_navigation(driver, ignored_exceptions):
    # Detect and close pop-up when the page loads
    try:
        print("Attendere la chiusura del popup iniziale...")
        # PLACEHOLDER: Please verify this selector. Assuming a generic modal close button.
        close_btn = WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".modal-content button.close, .modal-header button.close, button[aria-label='Close'], button.chiudi-modal")))
        close_btn.click()
        print("Popup chiuso.")
    except TimeoutException:
        print("Nessun popup iniziale rilevato o tempo scaduto.")
        
    # Navigate to "Gestisci prenotazione" (Manage booking) section
    # PLACEHOLDER: Please verify this selector
    try:
        print("Navigazione verso 'Gestisci prenotazione'...")
        gestisci_section = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gestisci prenotazione') or contains(@ui-sref, 'gestisci')]")))
        gestisci_section.click()
    except TimeoutException:
        print("Impossibile trovare la sezione 'Gestisci prenotazione'. Proseguo (potresti già essere nella pagina corretta).")

    # Click the "Gestisci" button
    # PLACEHOLDER: Please verify this selector
    try:
        gestisci_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gestisci')]")))
        gestisci_btn.click()
    except TimeoutException:
        print("Impossibile trovare il bottone 'Gestisci'. Proseguo.")


def perform_login(driver, prescription, ignored_exceptions):
    # Enter patient and prescription data
    WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
        .until(EC.presence_of_element_located((By.ID, "cf"))).send_keys(prescription.codice_fiscale)
    driver.find_element(By.ID, "crs").send_keys(prescription.tessera_sanitaria)
    driver.find_element(By.ID, "codice").send_keys(prescription.prescription_n)

    # Confirm data
    element = driver.find_element(By.XPATH, "//button[@type='submit']")
    actions = ActionChains(driver)
    actions.double_click(element).perform()


def get_current_appointment(driver, ignored_exceptions, prescription):
    print("\n--- ESTRAZIONE DATI APPUNTAMENTO ATTUALE ---")
    # Get the element with all info
    appointment_data = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
        .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dati-appuntamento-summary")))
    
    raw_text = appointment_data.text
    debug_print(f"-> DEBUG: Testo grezzo del blocco appuntamento:\n{'-'*40}\n{raw_text}\n{'-'*40}")
    
    # 1. Safely extract date using Regex
    match = re.search(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})", raw_text)
    if match:
        app_date_string = f"{match.group(1)} - {match.group(2)}"
        debug_print(f"-> DEBUG: Data estratta via Regex: {app_date_string}")
    else:
        debug_print("-> [ERRORE] Regex non ha trovato un pattern Data/Ora valido nel testo grezzo!")
        app_date_string = raw_text[:30] # Fallback for debugging, will crash during strptime if invalid

    # 2. Extract address (less critical for parsing logic, but good to have)
    try:
        # We try to find the address dynamically instead of strict index
        address_blocks = appointment_data.find_elements(By.CSS_SELECTOR, "div > span")
        address = address_blocks[1].text if len(address_blocks) > 1 else raw_text.replace('\n', ' ')[:50]
    except:
        address = raw_text.replace('\n', ' ')[:50]
        
    debug_print(f"-> DEBUG: Indirizzo estratto: {address}")
    
    return Appointment(app_date_string, address, prescription)


def wait_loading(driver):
    # Wait for spinner to appear and disappear
    try:
        # wait for loading element to appear
        WebDriverWait(driver, 10)\
            .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".spinner-container")))

        # then wait for the element to disappear
        WebDriverWait(driver, 120)\
            .until_not(EC.presence_of_element_located((By.CSS_SELECTOR, ".spinner-container")))

    except TimeoutException:
        # if timeout exception was raised
        pass 

def safe_wait_loading(driver, timeout=3):
    try:
        # Wait specifically for invisibility, not absence from DOM
        WebDriverWait(driver, timeout).until(
            EC.invisibility_of_element_located((By.CSS_SELECTOR, ".spinner-container"))
        )
    except TimeoutException:
        debug_print("-> Overlay wait timed out, proceeding anyway...")


def handle_confirmation_form(driver, ignored_exceptions):
    try:
        debug_print("\n--- INIZIO GESTIONE FORM DI CONFERMA (CONSENSO E DATI) ---")
        
        # 3. Explicit Overlays/Spinners Removal Wait:
        debug_print("-> Attesa scomparsa di eventuali overlay/spinner...")
        safe_wait_loading(driver, timeout=3)

        # 4. Debugging Breakpoints before Consent and Confirmation
        if DEBUG_MODE:
            input("Breakpoint 1: Form caricato. Premi INVIO per procedere con l'interazione della privacy e dei campi...")

        # 2. Form Validation Event Dispatching for Phone, Email, Date
        debug_print("-> Cerco i campi di input per forzare la validazione (onBlur/onChange)...")
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='tel'], input[type='date'], input.form-control")
            for inp in inputs:
                if inp.is_displayed():
                    val = inp.get_attribute("value")
                    debug_print(f"-> Trovato input (type={inp.get_attribute('type')}, id={inp.get_attribute('id')}). Valore attuale: '{val}'. Dispaccio eventi...")
                    driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", inp)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", inp)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", inp)
                    time.sleep(0.2)
        except Exception as e:
            debug_print(f"-> Impossibile elaborare i campi di testo: {e}")

        # 1. Strict and Robust Checkbox Interaction
        debug_print("-> Cerco la checkbox per il consenso privacy...")
        try:
            checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
            
            if len(checkboxes) == 0:
                debug_print("-> Nessun tag <input type='checkbox'> trovato. Cerco elementi custom (.checkmark, .ui-chkbox-box)...")
                checkmarks = driver.find_elements(By.CSS_SELECTOR, ".checkmark, .ui-chkbox-box")
                for check in checkmarks:
                    debug_print("-> Clicco custom checkmark via Javascript...")
                    driver.execute_script("arguments[0].click();", check)
                    time.sleep(0.5)
            else:
                for checkbox in checkboxes:
                    is_checked = driver.execute_script("return arguments[0].checked;", checkbox)
                    if not is_checked:
                        debug_print(f"-> Clicco la checkbox privacy (id={checkbox.get_attribute('id')}) via Javascript...")
                        driver.execute_script("arguments[0].click();", checkbox)
                        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", checkbox)
                        time.sleep(0.5)
                    
                    final_state = driver.execute_script("return arguments[0].checked;", checkbox)
                    debug_print(f"-> Stato finale della checkbox: {'Selezionata' if final_state else 'NON Selezionata'}")
                    
        except Exception as e:
            debug_print(f"-> Errore durante l'interazione con la checkbox: {e}")

        if DEBUG_MODE:
            input("Breakpoint 2: Checkbox e campi elaborati. Premi INVIO per tentare il click su 'Conferma'...")

        debug_print("-> Attesa scomparsa di eventuali overlay/spinner prima della conferma...")
        safe_wait_loading(driver, timeout=3)

        debug_print("-> Cerco il pulsante 'Conferma'...")
        try:
            # We look for the conferma button robustly
            conferma_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_element_located((By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'conferma') or contains(@ng-click, 'conferma') or @btn-riprenota]")))
            
            debug_print("-> Attendo che 'Conferma' sia cliccabile...")
            WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions).until(EC.element_to_be_clickable(conferma_btn))
            
            debug_print("-> Clicco su 'Conferma' (via Javascript)...")
            driver.execute_script("arguments[0].click();", conferma_btn)
            debug_print("-> Pulsante 'Conferma' cliccato con successo.")
        except TimeoutException:
             debug_print("-> Fallback: cerco selettore originale per la conferma...")
             conferma_btn = WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='riprenotaRicettaCtrl.conferma']")))
             driver.execute_script("arguments[0].click();", conferma_btn)
             debug_print("-> Pulsante 'Conferma' (fallback) cliccato con successo.")

        debug_print("--- FINE GESTIONE FORM DI CONFERMA ---\n")
        safe_wait_loading(driver, timeout=3)
        
    except Exception as e:
        print(f"\nERRORE IMPREVISTO durante la gestione della conferma: {e}")


def get_first_availability(driver, ignored_exceptions):
    try:
        # Get appointment list
        app_list = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".lista-appuntamenti, .appuntamento-card, button[ng-click*='prenota'], button[ng-click*='scegli']")))

        # Get all list items representing appointments
        list_items = driver.find_elements(By.CSS_SELECTOR, ".lista-appuntamenti li, .appuntamento-card")
        
        if not list_items:
            wrapper = driver.find_elements(By.CSS_SELECTOR, ".lista-appuntamenti, .container-appuntamenti")
            if wrapper:
                list_items = wrapper
                
        for item in list_items:
            try:
                item_text = item.text
                if not item_text.strip():
                    continue
                    
                # Regex to flexibly catch DD/MM/YYYY followed by HH:MM, ignoring separators like "-", "alle", etc.
                match = re.search(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})", item_text)
                
                if match:
                    date_str = match.group(1)
                    time_str = match.group(2)
                    clean_datetime_str = f"{date_str} {time_str}"
                    try:
                        availability = datetime.strptime(clean_datetime_str, "%d/%m/%Y %H:%M")
                        return availability, item_text  # Successfully found the first parseable availability
                    except ValueError as e:
                        print(f"-> Could not parse date from text '{clean_datetime_str}': {e}")
                        continue
                else:
                    # If regex fails, print a warning and check the next item
                    print(f"-> Could not parse date pattern from text: {item_text[:100].replace(chr(10), ' ')}...")
                    continue
            except StaleElementReferenceException:
                pass
                
        print("-> Errore: la lista è apparsa ma non ho estratto date valide.")
        return None, None
        
    except TimeoutException:
        return None, None


def get_new_appointment_info(driver, ignored_exceptions):
    print("\n--- ESTRAZIONE DATI NUOVO APPUNTAMENTO ---")
    appointment_data = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
        .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dati-appuntamento-summary")))
        
    raw_text = appointment_data.text
    debug_print(f"-> DEBUG: Testo grezzo del blocco NUOVO appuntamento:\n{'-'*40}\n{raw_text}\n{'-'*40}")
    
    # Safely extract date using Regex
    match = re.search(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})", raw_text)
    if match:
        new_date = f"{match.group(1)} - {match.group(2)}"
        debug_print(f"-> DEBUG: Nuova Data estratta via Regex: {new_date}")
    else:
        debug_print("-> [ERRORE] Regex non ha trovato un pattern Data/Ora valido nel testo grezzo!")
        new_date = "Data Non Valida"

    try:
        # Avoid strict XPath, use generic CSS or fallback to full text
        new_address = raw_text.replace('\n', ' ')[:100]
    except:
        new_address = "Indirizzo Sconosciuto"
        
    debug_print(f"-> DEBUG: Nuovo indirizzo estratto: {new_address}")
        
    alert = driver.find_elements(By.CSS_SELECTOR, ".note-prepazione-descrizione > p")
    return new_address, new_date, alert


def check_search_outcome(driver, current_province, timeout=15):
    start_time = time.time()
    print(f"-> Analizzo l'esito della ricerca per {current_province} (Attesa max: {timeout}s)...")
    
    # Pre-process province name to ignore common generic suffixes for flexible string matching
    search_term = current_province.lower().replace(" citta'", "").replace(" provincia", "")
    
    while time.time() - start_time < timeout:
        try:
            # Check A: Error Pop-up
            error_texts = driver.find_elements(By.XPATH, "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'nessuna disponibilit') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'nessun appuntamento') or contains(@class, 'modal-title') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'attenzione')]")
            if any(e.is_displayed() for e in error_texts):
                return "ERROR"
                
            # Check B: Results
            results_containers = driver.find_elements(By.CSS_SELECTOR, ".lista-appuntamenti, .appuntamento-card")
            action_buttons = driver.find_elements(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prenota') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'scegli')]")
            
            visible_results = [r for r in results_containers if r.is_displayed()]
            visible_buttons = [b for b in action_buttons if b.is_displayed()]
            
            if visible_results or visible_buttons:
                result_text = ""
                if visible_results:
                    result_text = visible_results[0].text.lower()
                else:
                    result_text = driver.find_element(By.TAG_NAME, "body").text.lower()
                
                # Anti-Stale Data Check
                if search_term in result_text:
                    return "RESULTS"
                else:
                    # Results are visible but do not match the current province. It's stale data.
                    pass 
                
        except StaleElementReferenceException:
            pass # DOM is updating, try again next loop
            
        time.sleep(0.5)
    
    return "TIMEOUT"


def search_in_province(driver, ignored_exceptions, province_name, search_preferences):
    try:
        debug_print("\n--- INIZIO INTERAZIONE FORM RICERCA ---")
        debug_print("1. Attendendo la scomparsa di eventuali caricamenti (spinner)...")
        safe_wait_loading(driver, timeout=3)
        
        # Determine current UI state
        try:
            edit_btn = driver.find_element(By.CSS_SELECTOR, "button[id='modifica-ricerca-info-testata']")
            if edit_btn.is_displayed():
                is_expanded = edit_btn.get_attribute("aria-expanded")
                if is_expanded == "false" or not is_expanded:
                    debug_print("3. (Pagina Risultati) Il menu di ricerca è chiuso. Clicco sul pulsante modifica ricerca per aprirlo...")
                    driver.execute_script("arguments[0].click();", edit_btn)
                    time.sleep(1) # Wait for dropdown animation to finish
                    safe_wait_loading(driver, timeout=3)
                else:
                    debug_print("3. (Pagina Risultati) Il menu di ricerca è GIA' aperto. Non clicco il bottone per evitare di chiuderlo.")
        except NoSuchElementException:
            pass # Form is already visible (Main page or Error popup closed)

        debug_print("4. Attendo che il selettore della provincia sia visibile e cliccabile...")
        try:
            provincia_selects = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "select[id='provincia']")))
            provincia_select = next((s for s in provincia_selects if s.is_displayed()), None)
            if not provincia_select:
                raise TimeoutException()
        except TimeoutException:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            if len(iframes) > 0:
                debug_print("-> Dropdown non trovato nel DOM principale. Passo al primo iframe...")
                driver.switch_to.frame(iframes[0])
                provincia_selects = WebDriverWait(driver, 5, ignored_exceptions=ignored_exceptions)\
                    .until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "select[id='provincia']")))
                provincia_select = next((s for s in provincia_selects if s.is_displayed()), None)

        debug_print(f"5. Seleziono la provincia: {province_name}...")
        element = Select(provincia_select)
        element.select_by_visible_text(province_name)
        
        debug_print("-> Eseguo evento Javascript 'change' sul dropdown...")
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", provincia_select)

        debug_print("8. Cerco il pulsante di sottomissione (Cerca o Aggiorna)...")
        try:
            # First try the modal update button
            confirm_btns = driver.find_elements(By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='doveQuandoModalCtrl.aggiorna']")
            submit_btn = next((b for b in confirm_btns if b.is_displayed()), None)
            if submit_btn:
                debug_print("-> Trovato pulsante 'Aggiorna' (Modale). Clicco...")
                driver.execute_script("arguments[0].click();", submit_btn)
            else:
                raise NoSuchElementException()
        except NoSuchElementException:
            # Fallback to main page .submit
            debug_print("-> Scroll verso il pulsante 'Cerca' (Main Page)...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            submit_btns = WebDriverWait(driver, 5, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".submit")))
            submit_btn = next((b for b in submit_btns if b.is_displayed()), None)
            debug_print("-> Trovato pulsante 'Cerca'. Clicco...")
            driver.execute_script("arguments[0].click();", submit_btn)
        
        debug_print("9. Torno al contesto principale e attendo l'avvio della ricerca...")
        driver.switch_to.default_content()
        safe_wait_loading(driver, timeout=3)
        
        debug_print("--- FINE INTERAZIONE FORM RICERCA (SUCCESSO) ---\n")
        return True # success
        
    except Exception as e:
        print(f"\nERRORE IMPREVISTO durante l'inserimento dei dati di ricerca in {province_name}: {str(e)[:200]}")
        driver.switch_to.default_content()
        return False

def cleanup_ui_for_next_search(driver, ignored_exceptions):
    debug_print("\n-> [PULIZIA UI] Avvio chiusura di tutti i popup e menu...")
    
    # 1. Close all Error Modals
    popups_closed = 0
    max_attempts = 5
    while popups_closed < max_attempts:
        try:
            close_btns = driver.find_elements(By.CSS_SELECTOR, ".modal-dialog button.close, .modal-dialog button[ng-click*='chiudi'], .modal-dialog .btn-default, .modal-dialog .btn-primary")
            visible_btns = [btn for btn in close_btns if btn.is_displayed()]
            
            if not visible_btns:
                break
                
            debug_print(f"-> Chiusura popup #{popups_closed + 1} in corso...")
            driver.execute_script("arguments[0].click();", visible_btns[0])
            popups_closed += 1
            time.sleep(0.5)
        except Exception as e:
            debug_print(f"-> Errore durante la chiusura del popup: {str(e)[:100]}")
            break
            
    if popups_closed > 0:
        debug_print("-> Attendo la completa invisibilità dei popup dal DOM...")
        try:
            WebDriverWait(driver, 5, ignored_exceptions=ignored_exceptions).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog"))
            )
            debug_print("-> Popup completamente invisibili.")
        except TimeoutException:
            debug_print("-> Timeout attesa scomparsa popup. Procedo comunque.")

    # 2. Close Search Menu if it's open
    try:
        edit_btn = driver.find_element(By.CSS_SELECTOR, "button[id='modifica-ricerca-info-testata']")
        if edit_btn.is_displayed():
            is_expanded = edit_btn.get_attribute("aria-expanded")
            if is_expanded == "true":
                debug_print("-> [PULIZIA UI] Il menu di ricerca è rimasto aperto. Lo chiudo...")
                driver.execute_script("arguments[0].click();", edit_btn)
                time.sleep(1) # wait for collapse animation
    except NoSuchElementException:
        pass
    except Exception as e:
        debug_print(f"-> Errore chiusura menu ricerca: {e}")

    debug_print("-> Pulizia UI completata. Attendo 3 secondi prima della prossima provincia...")
    time.sleep(3)


def reset_to_search_form(driver, prescription, ignored_exceptions):
    debug_print("\n-> [HARD RESET] Ricarico la pagina per eliminare i dati sporchi dal DOM...")
    driver.refresh()
    time.sleep(3)
    
    try:
        cf_inputs = driver.find_elements(By.ID, "cf")
        if cf_inputs and cf_inputs[0].is_displayed():
            debug_print("-> Richiesto nuovo login dopo il refresh...")
            perform_login(driver, prescription, ignored_exceptions)
            time.sleep(2)
    except:
        pass
        
    try:
        debug_print("-> Rientro nella gestione appuntamento: Clicco sul pulsante 'Modifica/Riprenota'...")
        element = WebDriverWait(driver, 15, ignored_exceptions=ignored_exceptions).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "button[btn-riprenota='']"))
        )
        driver.execute_script("arguments[0].click();", element)
        
        # We need to handle the privacy confirmation form again
        handle_confirmation_form(driver, ignored_exceptions)
    except Exception as e:
        debug_print(f"-> Errore durante il rientro nel form di ricerca: {e}")


def search_loop(driver, search_preferences, current_appointment, prescription, ignored_exceptions):
    iteration = 1
    while True:
        print(f"\n=============================================")
        print(f"   INIZIO CICLO DI RICERCA GLOBALE #{iteration}")
        print(f"=============================================")
        
        for prov in search_preferences.province:
            print(f"\n>>> Ricerca nella provincia: {prov} <<<")
            
            success = search_in_province(driver, ignored_exceptions, prov, search_preferences)
            
            if not success:
                print(f"Ricerca in {prov} interrotta a causa di un errore nel form.")
                cleanup_ui_for_next_search(driver, ignored_exceptions)
                continue
            
            outcome = check_search_outcome(driver, prov)
            
            if outcome == "TIMEOUT":
                print(f"\n=============================================")
                print(f"WARNING: L'interfaccia in {prov} sta impiegando più tempo del previsto a caricare.")
                print(f"Tento un ulteriore wait/retry automatico di 15 secondi...")
                print(f"=============================================")
                outcome = check_search_outcome(driver, prov, timeout=15)
            
            if outcome == "ERROR":
                print(f"\n-> [BRANCH A - NO DISPONIBILITÀ] Nessun appuntamento utile trovato in {prov}.")
                cleanup_ui_for_next_search(driver, ignored_exceptions)
                continue
                
            elif outcome == "RESULTS":
                print(f"\n-> [BRANCH B - DISPONIBILITÀ TROVATA] Trovati appuntamenti in {prov}! Elaborazione...")
                first_availability, raw_text = get_first_availability(driver, ignored_exceptions)
                
                if first_availability is None:
                    print(f"-> Errore: la lista è apparsa ma non ho potuto estrarre la data.")
                    cleanup_ui_for_next_search(driver, ignored_exceptions)
                    continue
                   
                current_dt = current_appointment.get_datetime()
                print(f"DEBUG: Found slot with raw text: {raw_text[:150].replace(chr(10), ' ')}...")
                print(f"DEBUG: Parsed new date: {first_availability.strftime('%d/%m/%Y %H:%M')} | Current date: {current_dt.strftime('%d/%m/%Y %H:%M')}")
                
                if first_availability < current_dt:
                    print('\a') # Audio beep
                    print(f"\n!!! TROVATA DISPONIBILITA' MIGLIORE IN {prov} !!!")
                    print(f"Data trovata: {first_availability.strftime('%d/%m/%Y %H:%M')}")
                    
                    # --- UI CLEANUP ---
                    debug_print("-> Rimozione overlay/banner cookie via JS per pulire lo schermo...")
                    try:
                        driver.execute_script("""
                            var elements = document.querySelectorAll('.cc-window, .cookie-banner, header, nav, .navbar');
                            for (var i = 0; i < elements.length; i++) {
                                elements[i].style.display = 'none';
                            }
                            window.scrollTo(0, 0);
                        """)
                    except Exception as e:
                        debug_print(f"-> Errore durante la pulizia della UI: {e}")
                        
                    # --- INTERACTIVE PAUSE ---
                    user_choice = input("\nBetter appointment found! Do you want to proceed manually? (Y/N): ")
                    
                    if user_choice.upper() in ['N', 'NO']:
                        print(f"-> Scelta utente: N. Ignoro l'appuntamento in {prov} e resetto il form per la successiva.")
                        reset_to_search_form(driver, prescription, ignored_exceptions)
                        continue
                        
                    print("-> Scelta utente: Y. Procedo con la conferma automatizzata...")
                    try:
                        driver.find_element(By.CSS_SELECTOR, ".ui-disponibilita-action-buttons > button[id='verifica_conferma_appuntamenti']").click()
                    except ElementClickInterceptedException:
                        btn = driver.find_element(By.CSS_SELECTOR, ".ui-disponibilita-action-buttons > button[id='verifica_conferma_appuntamenti']")
                        driver.execute_script("arguments[0].click();", btn)

                    new_address, new_date, alert = get_new_appointment_info(driver, ignored_exceptions)
                    
                    print(f"\n=============================================")
                    print(f"NUOVO APPUNTAMENTO DISPONIBILE:")
                    print(f"Data: {new_date}")
                    print(f"Struttura: {new_address}")
                    if alert:
                        print("Note importanti:")
                        for a in alert:
                            print("-", a.text)
                    print(f"=============================================")

                    if search_preferences.dry_run:
                        print("\n[DRY RUN] Modalità sicura attiva. L'appuntamento NON verrà modificato.")
                        print("Riprendo la ricerca...")
                        try:
                            annulla_btn = driver.find_element(By.CSS_SELECTOR, ".modal-footer > .btn-default[ng-click^='verificaPrenotazioneCtrl.annulla']")
                            driver.execute_script("arguments[0].click();", annulla_btn)
                            time.sleep(1)
                        except:
                            pass
                    else:
                        risposta = input("\nVuoi confermare il nuovo appuntamento? Y/N: ")

                        if risposta.upper() == "Y":
                            try:
                                checkmark = driver.find_element(By.CSS_SELECTOR, ".checkmark")
                                driver.execute_script("arguments[0].click();", checkmark)
                                
                                conf_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
                                    .until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='verificaPrenotazioneCtrl.conferma']")))
                                driver.execute_script("arguments[0].click();", conf_btn)
                                
                                current_appointment.change_app(new_date, new_address)
                                print("\n!!! APPUNTAMENTO AGGIORNATO CON SUCCESSO !!!")
                            except Exception as e:
                                print(f"Errore durante la conferma: {e}")
                                
                        risposta_continua = input("Vuoi continuare la ricerca per trovare date ancora migliori? Y/N: ")

                        if risposta_continua.upper() == "N":
                            return
                        else:
                            try:
                                annulla_btn = driver.find_element(By.CSS_SELECTOR, ".modal-footer > .btn-default[ng-click^='verificaPrenotazioneCtrl.annulla']")
                                driver.execute_script("arguments[0].click();", annulla_btn)
                                time.sleep(1)
                            except:
                                pass
                else:
                    print(f"REJECTED: The found date is NOT strictly earlier than the current date.")
                
                cleanup_ui_for_next_search(driver, ignored_exceptions)
                continue
            
            else:
                print(f"\n-> [TIMEOUT/UNKNOWN] La UI in {prov} non ha caricato risultati validi dopo i retry. Salto la provincia e pulisco l'interfaccia.")
                cleanup_ui_for_next_search(driver, ignored_exceptions)
                continue
        
        print(f"\n=============================================")
        print(f"   FINE CICLO #{iteration}")
        print(f"   Pausa di {search_preferences.refresh_frequency} secondi prima di ricominciare...")
        print(f"=============================================")
        time.sleep(search_preferences.refresh_frequency)
        iteration += 1


def main():
    print("ciao :) \n")

    # Ask user information that will be used during search
    if path.isfile("data_file.py"):
        if input("Scrivi 1 per inserire i dati a mano oppure 2 per utilizzare quelli nel file 'data_file.py': ") == "2":
            prescription, search_preferences = get_data_from_file()
        else:
            prescription, search_preferences = ask_data()  
    else:
        prescription, search_preferences = ask_data()
    
    if search_preferences.dry_run:
        print("\n*** ESECUZIONE IN MODALITA' DRY RUN (NESSUNA MODIFICA VERRA' APPORTATA) ***\n")

    # Ask which browser to use
    driver = use_chrome() if input("Scrivi 1 per usare Chrome oppure 2 per Firefox: ") == "1" else use_firefox()
    driver.set_window_size(1400,1000)

    # Open link Prenota Online
    driver.get("https://prenotasalute.regione.lombardia.it/prenotaonline/")
         
    try:
        ignored_exceptions = (NoSuchElementException, StaleElementReferenceException)
        
        # New UI Flow 
        handle_initial_navigation(driver, ignored_exceptions)

        # Login
        perform_login(driver, prescription, ignored_exceptions)

        # Get current appointment date
        current_appointment = get_current_appointment(driver, ignored_exceptions, prescription)
        print("L'appuntamento attuale è fissato per il giorno", current_appointment.date)
        print("presso", current_appointment.address)
        print("\n")

        # Click on edit appointment 
        print("\n-> Clicco sul pulsante per modificare l'appuntamento (btn-riprenota)...")
        element = driver.find_element(By.CSS_SELECTOR, "button[btn-riprenota='']")
        driver.execute_script("arguments[0].click();", element)

        # Handle the confirmation modal (with privacy checkbox and inputs)
        handle_confirmation_form(driver, ignored_exceptions)

        # Start search loop over multiple provinces
        search_loop(driver, search_preferences, current_appointment, prescription, ignored_exceptions)
        
        # Close the browser and end script
        print("Grazie per aver combattuto insieme contro la sanità privata <3")
        driver.quit()
        sys.exit()

    except Exception as e:
        print(f"Si è verificato un errore: {e}")
        driver.quit()

if __name__ == "__main__":
    main()