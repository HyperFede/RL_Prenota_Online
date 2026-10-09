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
from selenium.common.exceptions import WebDriverException
from datetime import datetime
import time
import sys
import re

DEBUG_MODE = False

def debug_print(msg):
    if DEBUG_MODE:
        print(msg)
from dataObjects import Patient, Prescription, Appointment, SearchPreferences, Slot
from decisions import DecisionStore, EXPIRED, ACCEPTED, REJECTED, BOOKED
from telegram_bot import TelegramBot, TelegramError, ACCEPT, REJECT
from os import path

BASE_URL = "https://prenotasalute.regione.lombardia.it/prenotaonline/"

# Booking modes, detected automatically after login
MODE_RESCHEDULE = "modifica"  # the prescription already has an appointment: look for an earlier one
MODE_NEW = "nuova"            # no appointment yet: first-time booking

# Matches "dd/mm/yyyy - HH:MM", "dd/mm/yyyy HH:MM", "dd/mm/yyyy alle HH:MM"...
DATE_TIME_RE = re.compile(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})")

# The results list is paginated (5 per page, sorted by date): don't walk through too many pages
MAX_RESULT_PAGES = 10


def ask_yes_no(question, default=None):
    answer = input(question).strip().upper()
    if answer in ("S", "SI", "SÌ", "Y", "YES"):
        return True
    if answer in ("N", "NO"):
        return False
    return default


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
    dry_run_input = input("Eseguire in modalità dry-run (sicura, nessuna prenotazione verrà effettuata o modificata)? Y/N: ")
    dry_run = True if dry_run_input.upper() == 'Y' else False

    print("\n[Solo per una PRIMA prenotazione] Se la ricetta non ha ancora un appuntamento, il portale chiede i tuoi recapiti.")
    telefono = input("Numero di telefono (lascia vuoto se la ricetta è già prenotata): ").strip()
    email = input("Email (opzionale): ").strip()
    visita_controllo = ask_yes_no("La ricetta riporta la dicitura CONTROLLO o FOLLOW-UP? S/N (vuoto = chiedimelo se serve): ")

    print("\n[Opzionale] Notifiche Telegram")
    print("Per ricevere notifiche, crea un bot tramite @BotFather su Telegram e ottieni il tuo Chat ID.")
    print("Puoi ottenere il tuo Chat ID avviando il bot e visitando: https://api.telegram.org/bot<TUO_TOKEN>/getUpdates")
    telegram_token = input("Inserisci il Token del bot Telegram (lascia vuoto per disabilitare): ").strip()
    telegram_chat_id = input("Inserisci il Chat ID di Telegram (lascia vuoto per disabilitare): ").strip()
    telegram_timeout = 15
    if telegram_token and telegram_chat_id:
        timeout_input = input("Quanti minuti attendere la tua risposta su Telegram prima di riprendere la ricerca? (default 15): ").strip()
        telegram_timeout = int(timeout_input) if timeout_input.isdigit() else 15
    continua = ask_yes_no("Dopo aver prenotato, continuare a cercare date ancora migliori? S/N (default N): ", False)

    print("\n")

    patient = Patient(codice_fiscale, tessera_sanitaria)
    prescription = Prescription(prescription_n, patient)
    search_preferences = SearchPreferences(province, start_date, end_date, REFRESH_FREQUENCY, dry_run, telegram_token, telegram_chat_id,
                                           telefono, email, visita_controllo, telegram_timeout, continua)

    return prescription, search_preferences


def get_data_from_file():
    import data_file

    patient = Patient(data_file.codice_fiscale, data_file.tessera_sanitaria)
    prescription = Prescription(data_file.prescription_n, patient)
    
    # Handle the case where the older data_file.py only had 'provincia' string instead of 'province' list
    province = getattr(data_file, 'province', [])
    if not province and hasattr(data_file, 'provincia'):
        province = [data_file.provincia]
        
    dry_run = getattr(data_file, 'dry_run', True)
    telegram_token = getattr(data_file, 'telegram_bot_token', "")
    telegram_chat_id = getattr(data_file, 'telegram_chat_id', "")
    
    search_preferences = SearchPreferences(province, data_file.start_date, data_file.end_date, data_file.refresh_frequency, dry_run, telegram_token, telegram_chat_id,
                                           telefono=str(getattr(data_file, 'telefono', "")),
                                           email=getattr(data_file, 'email', ""),
                                           visita_controllo=getattr(data_file, 'visita_controllo', None),
                                           telegram_timeout_minuti=getattr(data_file, 'telegram_timeout_minuti', 15),
                                           continua_dopo_prenotazione=getattr(data_file, 'continua_dopo_prenotazione', False))

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


def is_displayed_safe(element):
    try:
        return element.is_displayed()
    except (StaleElementReferenceException, WebDriverException):
        return False


def visible_elements(driver, css_selector):
    return [e for e in driver.find_elements(By.CSS_SELECTOR, css_selector) if is_displayed_safe(e)]


# Reads every visible availability of the current results page in a single JS call.
# Each <li class="appuntamento"> has rows made of ".appuntamento-field-title" / ".appuntamento-field-value"
# ("Data e ora", "Azienda", "Comune", "Presentarsi in") and its own "Verifica e conferma" button.
EXTRACT_SLOTS_JS = """
var items = document.querySelectorAll('ul.lista-appuntamenti > li.appuntamento');
var out = [];
for (var i = 0; i < items.length; i++) {
    var li = items[i];
    if (!li.offsetParent) { continue; }
    var fields = {};
    var titles = li.querySelectorAll('.appuntamento-field-title');
    for (var j = 0; j < titles.length; j++) {
        var value = titles[j].parentElement.querySelector('.appuntamento-field-value');
        if (value) { fields[titles[j].innerText.trim().toLowerCase()] = value.innerText.trim(); }
    }
    var btn = li.querySelector('#verifica_conferma_appuntamenti');
    out.push({index: i, fields: fields, text: li.innerText, bookable: !!(btn && btn.offsetParent)});
}
return out;
"""


def parse_slot(raw, province):
    fields = raw.get("fields") or {}
    match = DATE_TIME_RE.search(fields.get("data e ora") or raw.get("text") or "")
    if not match:
        return None
    try:
        when = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%d/%m/%Y %H:%M")
    except ValueError:
        return None
    return Slot(when, fields.get("azienda"), fields.get("presentarsi in"), fields.get("comune"), province, raw.get("index"))


def extract_slots(driver, province):
    slots = []
    for raw in driver.execute_script(EXTRACT_SLOTS_JS) or []:
        if not raw.get("bookable"):
            # e.g. "differita" availabilities without a fixed time, they can't be booked online
            debug_print(f"-> Disponibilità non prenotabile online ignorata: {raw.get('text', '')[:80]!r}")
            continue
        slot = parse_slot(raw, province)
        if slot is None:
            print(f"-> Impossibile leggere data/ora da: {raw.get('text', '')[:100].replace(chr(10), ' ')}...")
            continue
        slots.append(slot)
    return slots


def go_to_next_results_page(driver):
    links = visible_elements(driver, "ul.pagination li.pagination-next:not(.disabled) a")
    if not links:
        return False
    driver.execute_script("arguments[0].click();", links[0])
    time.sleep(1)
    return True


def read_modal_message(driver):
    texts = []
    for modal in visible_elements(driver, ".modal-dialog"):
        try:
            texts.append(modal.text.strip())
        except StaleElementReferenceException:
            pass
    return " | ".join(t.replace("\n", " ") for t in texts if t)[:400]


def book_slot(driver, ignored_exceptions, slot, dry_run):
    """Open "Verifica e conferma" for exactly this slot and confirm it (unless dry_run).

    Returns (outcome, details) where outcome is "booked", "dry_run" or "failed".
    """
    # The list may have been re-rendered while waiting for the user's answer: find the slot again by identity
    current = next((s for s in extract_slots(driver, slot.provincia) if s.slot_id == slot.slot_id), None)
    if current is None:
        return "failed", "l'appuntamento non è più presente nella lista dei risultati"

    debug_print(f"-> Clicco 'Verifica e conferma' sulla disponibilità #{current.index}...")
    driver.execute_script(
        "document.querySelectorAll('ul.lista-appuntamenti > li.appuntamento')[arguments[0]]"
        ".querySelector('#verifica_conferma_appuntamenti').click();", current.index)

    try:
        summary = WebDriverWait(driver, 30, ignored_exceptions=ignored_exceptions)\
            .until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog .dati-appuntamento-summary")))
    except TimeoutException:
        message = read_modal_message(driver)
        return "failed", f"il riepilogo dell'appuntamento non si è aperto. {message}".strip()

    # Safety check: the summary must show the same date/time the user accepted
    match = DATE_TIME_RE.search(summary.text)
    shown = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%d/%m/%Y %H:%M") if match else None
    notes = [n.text.strip() for n in driver.find_elements(By.CSS_SELECTOR, ".modal-dialog .note-prepazione-descrizione") if n.text.strip()]

    def close_summary():
        for btn in visible_elements(driver, ".modal-footer > .btn-default[ng-click^='verificaPrenotazioneCtrl.annulla']"):
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(1)

    if shown != slot.when:
        close_summary()
        return "failed", f"il riepilogo mostra una data diversa ({shown}) da quella proposta ({slot.when}): annullato per sicurezza"

    if dry_run:
        close_summary()
        return "dry_run", "\n".join(notes)

    debug_print("-> Spunto la presa visione delle note...")
    driver.execute_script("var c = document.getElementById('presaVisioneNote'); if (c && !c.checked) { c.click(); }")
    try:
        conf_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='verificaPrenotazioneCtrl.conferma']")))
    except TimeoutException:
        close_summary()
        return "failed", "il pulsante 'Conferma' non è diventato attivo"
    driver.execute_script("arguments[0].click();", conf_btn)

    # The summary closes, then either the "Appuntamento prenotato" page or an error popup appears
    deadline = time.time() + 90
    popup_seen = 0
    time.sleep(2)
    while time.time() < deadline:
        try:
            done = driver.find_elements(By.XPATH, "//h4[contains(., 'Appuntamento prenotato') or contains(., 'Appuntamenti prenotati')]")
            if any(is_displayed_safe(d) for d in done):
                return "booked", "\n".join(notes)
            if visible_elements(driver, ".modal-dialog") and not visible_elements(driver, ".modal-dialog .dati-appuntamento-summary"):
                # Give the confirmation page a few seconds before calling a popup an error
                popup_seen += 1
                if popup_seen >= 5:
                    return "failed", f"il portale ha risposto: {read_modal_message(driver)}"
            else:
                popup_seen = 0
        except StaleElementReferenceException:
            pass
        time.sleep(1)
    return "failed", "nessuna conferma ricevuta dal portale entro 90 secondi (controlla sul sito se la prenotazione è avvenuta)"


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
            no_results = driver.find_elements(By.XPATH, "//h5[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'non sono state trovate disponibilit')]")
            if any(e.is_displayed() for e in no_results):
                return "ERROR"
                
            # Check B: Results
            results_containers = driver.find_elements(By.CSS_SELECTOR, ".lista-appuntamenti, .appuntamento-card")
            action_buttons = driver.find_elements(By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'prenota') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'scegli')]")
            
            visible_results = [r for r in results_containers if r.is_displayed()]
            visible_buttons = [b for b in action_buttons if b.is_displayed()]
            
            if visible_results or visible_buttons:
                # We wiped old results before searching, so any visible results are strictly new.
                return "RESULTS"
                
        except StaleElementReferenceException:
            pass # DOM is updating, try again next loop
            
        time.sleep(0.5)
    
    return "TIMEOUT"


def fill_contacts_form(driver, search_preferences):
    """Fill the contacts required by the 'Dove e quando' form when they are empty.

    For an existing appointment the portal pre-fills them; for a first booking the phone number
    and the privacy consent are mandatory, otherwise the search button stays disabled.
    """
    # The portal accepts digits only (6-15): drop spaces and an Italian +39/0039 prefix
    telefono = re.sub(r"^(\+|00)39", "", re.sub(r"[\s./-]", "", search_preferences.telefono or ""))
    telefono = re.sub(r"\D", "", telefono)
    for field_id, value in (("telefono", telefono), ("email", (search_preferences.email or "").strip())):
        for field in driver.find_elements(By.ID, field_id):
            if not is_displayed_safe(field) or (field.get_attribute("value") or "").strip():
                continue
            if not value:
                if field_id == "telefono":
                    print("ATTENZIONE: il portale richiede un numero di telefono ma non è stato configurato ('telefono').")
                continue
            debug_print(f"-> Compilo il campo {field_id}...")
            field.send_keys(value)
            driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", field)

    for consent in driver.find_elements(By.ID, "consensoPrenotazione"):
        if not driver.execute_script("return arguments[0].checked;", consent):
            debug_print("-> Spunto il consenso al trattamento dati...")
            driver.execute_script("arguments[0].click();", consent)


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

        fill_contacts_form(driver, search_preferences)

        debug_print("8. Rimuovo vecchi risultati dal DOM e cerco il pulsante di sottomissione (Cerca o Aggiorna)...")
        try:
            # Purge old results to prevent false positives
            driver.execute_script("""
                var old_results = document.querySelectorAll('.lista-appuntamenti, .appuntamento-card, .container-appuntamenti');
                for (var i = 0; i < old_results.length; i++) {
                    old_results[i].remove();
                }
            """)
        except Exception as e:
            pass

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
            visible_btns = [btn for btn in close_btns if btn.is_displayed() and 'conferma' not in (btn.get_attribute('ng-click') or '').lower()]
            
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


def detect_booking_mode(driver, timeout=45):
    """After login the portal shows the current appointment if the prescription is already booked,
    otherwise it goes straight to the booking flow. Returns (mode, error_message)."""
    deadline = time.time() + timeout
    reported_popups = set()
    while time.time() < deadline:
        try:
            if visible_elements(driver, ".dati-appuntamento-summary"):
                return MODE_RESCHEDULE, None
            if visible_elements(driver, "button[ng-click^='prenotaCompletaDatiCtrl.conferma'], select#provincia, input#telefono"):
                return MODE_NEW, None
            for modal in visible_elements(driver, ".modal-dialog"):
                text = modal.text.strip()
                titles = [t.text.strip().lower() for t in modal.find_elements(By.CSS_SELECTOR, ".modal-title")]
                if any("errore" in t or "attenzione" in t for t in titles):
                    return None, text.replace("\n", " ")[:400]
                if text not in reported_popups:
                    reported_popups.add(text)
                    print(f"-> Popup del portale: {text[:200]}")
        except StaleElementReferenceException:
            pass
        time.sleep(0.5)
    return None, "il portale non ha mostrato né un appuntamento né il modulo di prenotazione (controlla i dati inseriti)"


def handle_completa_dati_modal(driver, search_preferences, ignored_exceptions):
    """'Completa dati ricetta' modal, shown only for some prescriptions on a first booking."""
    try:
        conferma = WebDriverWait(driver, 8, ignored_exceptions=ignored_exceptions)\
            .until(EC.visibility_of_element_located((By.CSS_SELECTOR, "button[ng-click^='prenotaCompletaDatiCtrl.conferma']")))
    except TimeoutException:
        return
    print("-> Il portale chiede di completare i dati della ricetta.")

    radios = driver.find_elements(By.CSS_SELECTOR, ".modal-dialog input[name='controllo']")
    if len(radios) >= 2:
        controllo = search_preferences.visita_controllo
        if controllo is None:
            controllo = ask_yes_no("La ricetta riporta la dicitura CONTROLLO o FOLLOW-UP? S/N: ", False)
        # The first radio is "Sì", the second "No"
        driver.execute_script("arguments[0].click();", radios[0] if controllo else radios[1])

    rur2 = [f for f in driver.find_elements(By.ID, "rur2") if is_displayed_safe(f)]
    if rur2:
        print("La ricetta (rossa) richiede il codice RUR riportato sulla ricetta.")
        for field_id, label in (("rur1", "Codice RUR - prima parte (S): "), ("rur2", "Codice RUR - seconda parte (Y): ")):
            for field in driver.find_elements(By.ID, field_id):
                if is_displayed_safe(field):
                    field.send_keys(input(label).strip())
                    driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", field)

    WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions).until(EC.element_to_be_clickable(conferma))
    driver.execute_script("arguments[0].click();", conferma)
    safe_wait_loading(driver, timeout=5)


def prepare_new_booking(driver, search_preferences, ignored_exceptions):
    handle_completa_dati_modal(driver, search_preferences, ignored_exceptions)
    WebDriverWait(driver, 30, ignored_exceptions=ignored_exceptions)\
        .until(lambda d: visible_elements(d, "select#provincia"))
    fill_contacts_form(driver, search_preferences)


def open_search_flow(driver, prescription, search_preferences, ignored_exceptions):
    """Log in from the home page and bring the browser to the search form.

    Returns (mode, current_appointment); current_appointment is None for a first booking."""
    driver.get(BASE_URL)
    handle_initial_navigation(driver, ignored_exceptions)
    perform_login(driver, prescription, ignored_exceptions)

    mode, error = detect_booking_mode(driver)
    if mode is None:
        raise RuntimeError(f"Accesso non riuscito: {error}")

    if mode == MODE_RESCHEDULE:
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
        return mode, current_appointment

    print("Nessun appuntamento associato alla ricetta: cerco una data per la PRIMA PRENOTAZIONE.\n")
    prepare_new_booking(driver, search_preferences, ignored_exceptions)
    return mode, None


class SearchSession:
    def __init__(self, driver, prescription, search_preferences, ignored_exceptions, store, bot):
        self.driver = driver
        self.prescription = prescription
        self.prefs = search_preferences
        self.ignored_exceptions = ignored_exceptions
        self.store = store
        self.bot = bot
        self.mode = None
        self.current_appointment = None

    def open(self, attempts=1):
        for attempt in range(1, attempts + 1):
            try:
                self.mode, self.current_appointment = open_search_flow(self.driver, self.prescription, self.prefs, self.ignored_exceptions)
                return
            except Exception as e:
                if attempt == attempts:
                    raise
                print(f"Errore durante l'accesso al portale ({str(e)[:200]}). Nuovo tentativo tra 30 secondi...")
                time.sleep(30)

    def is_better(self, slot):
        if not self.prefs.is_in_date_window(slot.when):
            return False
        if self.current_appointment is not None and slot.when >= self.current_appointment.get_datetime():
            return False
        return True

    def upper_bound(self):
        bounds = []
        if self.current_appointment is not None:
            bounds.append(self.current_appointment.get_datetime())
        if self.prefs.end_date:
            bounds.append(self.prefs.get_end_date_datetime().replace(hour=23, minute=59))
        return min(bounds) if bounds else None


def proposal_text(session, slot, footer):
    header = "🏥 DISPONIBILITÀ TROVATA"
    if session.current_appointment is not None:
        context = f"🔁 Appuntamento attuale: {session.current_appointment.date}"
    else:
        context = "🆕 Prima prenotazione (la ricetta non ha ancora un appuntamento)"
    dry_run = "\n🧪 DRY RUN attivo: anche se accetti, NON verrà prenotato." if session.prefs.dry_run else ""
    return f"{header}\n\n{slot.describe()}\n\n{context}{dry_run}\n\n{footer}"


def update_proposal_message(session, slot_id, footer, keep_buttons=False):
    proposal = session.store.proposals.get(slot_id)
    if not session.bot.enabled or not proposal or not proposal.get("message_id"):
        return
    slot = Slot.from_dict(proposal["slot"])
    buttons = TelegramBot.decision_buttons(slot_id) if keep_buttons else None
    try:
        session.bot.edit(proposal["message_id"], proposal_text(session, slot, footer), buttons)
    except TelegramError as e:
        debug_print(f"-> Impossibile aggiornare il messaggio Telegram: {e}")


def apply_telegram_answer(session, answer, waiting_for=None):
    """Record an answer coming from Telegram. Returns the slot_id it refers to (or None)."""
    store, bot = session.store, session.bot
    slot_id = answer.get("slot_id") or store.slot_id_for_message(answer.get("message_id"))
    if not slot_id or slot_id not in store.proposals:
        bot.answer_callback(answer.get("callback_id"), "Proposta non riconosciuta")
        return None
    if slot_id == waiting_for:
        bot.answer_callback(answer.get("callback_id"), "Ricevuto!")
        return slot_id

    # Late answer to an older proposal (expired, or from a previous run)
    status = store.proposals[slot_id].get("status")
    if status == BOOKED:
        bot.answer_callback(answer.get("callback_id"), "Questo appuntamento è già stato prenotato")
    elif answer["decision"] == REJECT:
        store.discard_id(slot_id)
        bot.answer_callback(answer.get("callback_id"), "Scartato")
        update_proposal_message(session, slot_id, "❌ Scartato: non ti verrà più proposto.")
        print(f"-> Telegram: proposta {slot_id} scartata in ritardo, non verrà più proposta.")
    else:
        # The user wants it after all: book it as soon as it shows up again (if still better than the current one)
        store.discarded.pop(slot_id, None)
        store.set_status(slot_id, ACCEPTED)
        bot.answer_callback(answer.get("callback_id"), "Ok! Lo prenoterò appena lo ritrovo disponibile")
        update_proposal_message(session, slot_id, "✅ Accettato in ritardo: verrà prenotato appena lo ritrovo disponibile.")
        print(f"-> Telegram: proposta {slot_id} accettata in ritardo, verrà prenotata appena ritrovata.")
    return slot_id


def process_telegram_answers(session, timeout=0, waiting_for=None):
    """Poll Telegram once. Returns the decision for `waiting_for` if it arrived, otherwise None."""
    try:
        answers = session.bot.poll(timeout=timeout)
    except TelegramError as e:
        print(f"Errore di comunicazione con Telegram: {e}")
        time.sleep(5)
        return None
    decision = None
    for answer in answers:
        if apply_telegram_answer(session, answer, waiting_for) == waiting_for and waiting_for:
            decision = answer["decision"]
    return decision


def ask_decision(session, slot):
    """Ask the user whether to book this slot. Returns ACCEPT, REJECT or None (no answer)."""
    store, bot = session.store, session.bot
    print('\a')  # Audio beep
    print(f"\n!!! TROVATA DISPONIBILITA' MIGLIORE IN {slot.provincia} !!!")
    print(slot.describe())

    if bot.enabled:
        timeout_minutes = session.prefs.telegram_timeout_minuti
        footer = f"Vuoi prenotarlo? Premi un pulsante, metti 👍/👎 o rispondi 'si'/'no' (attendo {timeout_minutes} minuti)."
        try:
            message_id = bot.send(proposal_text(session, slot, footer), TelegramBot.decision_buttons(slot.slot_id))
        except TelegramError as e:
            print(f"Impossibile inviare la proposta su Telegram ({e}): chiedo sul terminale.")
        else:
            store.record_proposal(slot, message_id)
            print(f"-> Proposta inviata su Telegram, attendo la risposta per {timeout_minutes} minuti...")
            deadline = time.time() + timeout_minutes * 60
            while time.time() < deadline:
                remaining = int(deadline - time.time())
                decision = process_telegram_answers(session, timeout=max(1, min(25, remaining)), waiting_for=slot.slot_id)
                if decision:
                    return decision
            return None

    store.record_proposal(slot)
    answer = input("Prenotare questo appuntamento? [S] sì / [N] scarta per sempre / [Invio] salta per ora: ").strip().upper()
    if answer in ("S", "SI", "SÌ", "Y", "YES"):
        return ACCEPT
    if answer in ("N", "NO"):
        return REJECT
    return None


def find_candidate(session, province, skip):
    """Return the earliest slot (in the results of `province`) worth proposing or booking, or None."""
    bound = session.upper_bound()
    for page in range(MAX_RESULT_PAGES):
        slots = extract_slots(session.driver, province)
        best = None
        for slot in slots:
            if slot.slot_id in skip or not session.is_better(slot):
                continue
            if session.store.is_discarded(slot):
                print(f"-> {slot.date_str} {slot.time_str} presso {slot.sede or slot.azienda}: scartato da te in precedenza, lo ignoro.")
                continue
            if not (session.store.is_pre_accepted(slot) or session.store.should_propose(slot)):
                debug_print(f"-> {slot.date_str} {slot.time_str}: già proposto in questa sessione senza risposta.")
                continue
            if best is None or slot.when < best.when:
                best = slot
        if best:
            return best
        # Results are sorted by date: once a page starts after the limit, the next ones are even later
        if not slots or (bound and min(s.when for s in slots) >= bound):
            return None
        if not go_to_next_results_page(session.driver):
            return None
        debug_print(f"-> Passo alla pagina {page + 2} dei risultati...")
    return None


def process_results(session, province):
    """Handle the results shown for a province. Returns "NEXT", "RESET" or "STOP"."""
    store, bot = session.store, session.bot
    handled = set()
    while True:
        slot = find_candidate(session, province, handled)
        if slot is None:
            print(f"-> Nessuna nuova disponibilità migliore in {province}.")
            return "NEXT"
        handled.add(slot.slot_id)

        if store.is_pre_accepted(slot):
            print(f"\n!!! Ritrovato l'appuntamento che avevi accettato: {slot.date_str} {slot.time_str} ({province}) !!!")
            decision = ACCEPT
        else:
            decision = ask_decision(session, slot)

        if decision == REJECT:
            store.discard(slot)
            print("-> Appuntamento scartato: non verrà più proposto. Continuo la ricerca.")
            update_proposal_message(session, slot.slot_id, "❌ Scartato: non ti verrà più proposto. La ricerca continua.")
            continue
        if decision is None:
            store.set_status(slot.slot_id, EXPIRED)
            print("-> Nessuna risposta: continuo la ricerca (non lo riproporrò in questa sessione).")
            minutes = session.prefs.telegram_timeout_minuti
            update_proposal_message(session, slot.slot_id,
                                    f"⌛ Nessuna risposta entro {minutes} minuti: la ricerca è ripresa.\n"
                                    "Puoi ancora rispondere: se premi Prenota, lo prenoterò appena lo ritrovo disponibile.",
                                    keep_buttons=True)
            continue

        print("-> Appuntamento accettato. Procedo con la prenotazione...")
        update_proposal_message(session, slot.slot_id, "✅ Accettato: prenotazione in corso...")
        try:
            outcome, details = book_slot(session.driver, session.ignored_exceptions, slot, session.prefs.dry_run)
        except Exception as e:
            outcome, details = "failed", f"errore imprevisto: {str(e)[:200]}"

        if outcome == "dry_run":
            store.set_status(slot.slot_id, EXPIRED)
            print("\n[DRY RUN] Riepilogo verificato, l'appuntamento NON è stato prenotato. Riprendo la ricerca...")
            update_proposal_message(session, slot.slot_id, "🧪 DRY RUN: riepilogo verificato sul portale ma NON prenotato. La ricerca continua.")
            return "NEXT"

        if outcome == "failed":
            store.set_status(slot.slot_id, EXPIRED)
            print(f"\nPrenotazione NON riuscita: {details}. Riprendo la ricerca...")
            update_proposal_message(session, slot.slot_id, f"⚠️ Prenotazione non riuscita: {details}\nLa ricerca continua.")
            return "RESET"

        store.set_status(slot.slot_id, BOOKED)
        if session.current_appointment is not None:
            session.current_appointment.change_app(slot.appointment_date_str, slot.address)
        print("\n!!! APPUNTAMENTO PRENOTATO CON SUCCESSO !!!")
        print(slot.describe())
        if details:
            print("Note importanti:\n" + details)
        notes = f"\n\n📝 Note:\n{details[:1500]}" if details else ""
        update_proposal_message(session, slot.slot_id, f"🎉 PRENOTATO CON SUCCESSO!{notes}")

        if session.prefs.continua_dopo_prenotazione:
            bot.notify("🔎 Continuo a cercare una data ancora migliore di quella appena prenotata.")
            return "RESET"
        return "STOP"


def wait_between_cycles(session, seconds):
    if not session.bot.enabled:
        time.sleep(seconds)
        return
    # Keep listening to Telegram, so late answers are recorded while waiting
    deadline = time.time() + seconds
    while time.time() < deadline:
        process_telegram_answers(session, timeout=max(1, min(25, int(deadline - time.time()))))


def search_loop(session):
    driver, ignored_exceptions = session.driver, session.ignored_exceptions
    search_preferences = session.prefs
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
                result = process_results(session, prov)

                if result == "STOP":
                    return
                if result == "RESET":
                    # The page is no longer on the search results (booking page or error): log in again
                    print("-> Riparto dalla pagina iniziale del portale...")
                    session.open(attempts=3)
                    continue

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
        wait_between_cycles(session, search_preferences.refresh_frequency)
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

    bot = TelegramBot(search_preferences.telegram_token, search_preferences.telegram_chat_id)
    store = DecisionStore(prescription.prescription_n)
    if store.discarded:
        print(f"-> {len(store.discarded)} appuntamenti scartati in precedenza verranno ignorati.")

    # Ask which browser to use
    driver = use_chrome() if input("Scrivi 1 per usare Chrome oppure 2 per Firefox: ") == "1" else use_firefox()
    driver.set_window_size(1400,1000)

    ignored_exceptions = (NoSuchElementException, StaleElementReferenceException)
    session = SearchSession(driver, prescription, search_preferences, ignored_exceptions, store, bot)

    try:
        session.open()

        if session.mode == MODE_RESCHEDULE:
            bot.notify(f"🔎 Ricerca avviata: cerco una data precedente al {session.current_appointment.date} "
                       f"in {', '.join(search_preferences.province)}.")
        else:
            bot.notify(f"🔎 Ricerca avviata per la PRIMA prenotazione in {', '.join(search_preferences.province)}.")

        # Start search loop over multiple provinces
        search_loop(session)
        
        # Close the browser and end script
        print("Grazie per aver combattuto insieme contro la sanità privata <3")
        driver.quit()
        sys.exit()

    except Exception as e:
        print(f"Si è verificato un errore: {e}")
        bot.notify(f"❌ Ricerca interrotta per un errore: {str(e)[:300]}")
        driver.quit()

if __name__ == "__main__":
    main()
