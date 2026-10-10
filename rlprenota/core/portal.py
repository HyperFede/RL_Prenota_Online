"""Selenium steps on the Regione Lombardia booking portal (prenotasalute.regione.lombardia.it).

Shared by the command line tool and the NAS workers. Functions raise rlprenota.core.errors exceptions
when the portal does not look as expected, so callers can tell a portal change from a transient error.
"""
import logging
import re
import time
import unicodedata
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import TimeoutException
from selenium.common.exceptions import StaleElementReferenceException
from selenium.common.exceptions import WebDriverException

from rlprenota.core.errors import ErrorKind, LoginRejected, NoAvailability, PortalChanged, PortalError, classify
from rlprenota.core.models import Appointment, Slot

log = logging.getLogger(__name__)

BASE_URL = "https://prenotasalute.regione.lombardia.it/prenotaonline/"

# Booking modes, detected automatically after login
MODE_RESCHEDULE = "modifica"  # the prescription already has an appointment: look for an earlier one
MODE_NEW = "nuova"            # no appointment yet: first-time booking

# Matches "dd/mm/yyyy - HH:MM", "dd/mm/yyyy HH:MM", "dd/mm/yyyy alle HH:MM"...
# How the portal says "nothing bookable online right now" (popup texts, lower case)
NO_AVAILABILITY_MARKERS = ("non ci sono disponibilit", "nessuna disponibilit", "non sono state trovate disponibilit",
                           "nessun appuntamento")

DATE_TIME_RE = re.compile(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})")

# The results list is paginated (5 per page, sorted by date): don't walk through too many pages
MAX_RESULT_PAGES = 10


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
        log.info("Attendere la chiusura del popup iniziale...")
        # PLACEHOLDER: Please verify this selector. Assuming a generic modal close button.
        close_btn = WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.CSS_SELECTOR, ".modal-content button.close, .modal-header button.close, button[aria-label='Close'], button.chiudi-modal")))
        close_btn.click()
        log.info("Popup chiuso.")
    except TimeoutException:
        log.info("Nessun popup iniziale rilevato o tempo scaduto.")
        
    # Navigate to "Gestisci prenotazione" (Manage booking) section
    # PLACEHOLDER: Please verify this selector
    try:
        log.info("Navigazione verso 'Gestisci prenotazione'...")
        gestisci_section = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.XPATH, "//a[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gestisci prenotazione') or contains(@ui-sref, 'gestisci')]")))
        gestisci_section.click()
    except TimeoutException:
        log.info("Impossibile trovare la sezione 'Gestisci prenotazione'. Proseguo (potresti già essere nella pagina corretta).")

    # Click the "Gestisci" button
    # PLACEHOLDER: Please verify this selector
    try:
        gestisci_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
            .until(EC.element_to_be_clickable((By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'gestisci')]")))
        gestisci_btn.click()
    except TimeoutException:
        log.info("Impossibile trovare il bottone 'Gestisci'. Proseguo.")


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
    log.info("\n--- ESTRAZIONE DATI APPUNTAMENTO ATTUALE ---")
    # Get the element with all info
    appointment_data = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
        .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".dati-appuntamento-summary")))
    
    raw_text = appointment_data.text
    log.debug(f"-> DEBUG: Testo grezzo del blocco appuntamento:\n{'-'*40}\n{raw_text}\n{'-'*40}")
    
    # 1. Safely extract date using Regex
    match = re.search(r"(\d{2}/\d{2}/\d{4})[^\d]*(\d{2}:\d{2})", raw_text)
    if match:
        app_date_string = f"{match.group(1)} - {match.group(2)}"
        log.debug(f"-> DEBUG: Data estratta via Regex: {app_date_string}")
    else:
        log.debug("-> [ERRORE] Regex non ha trovato un pattern Data/Ora valido nel testo grezzo!")
        app_date_string = raw_text[:30] # Fallback for debugging, will crash during strptime if invalid

    # 2. Extract address (less critical for parsing logic, but good to have)
    try:
        # We try to find the address dynamically instead of strict index
        address_blocks = appointment_data.find_elements(By.CSS_SELECTOR, "div > span")
        address = address_blocks[1].text if len(address_blocks) > 1 else raw_text.replace('\n', ' ')[:50]
    except:
        address = raw_text.replace('\n', ' ')[:50]
        
    log.debug(f"-> DEBUG: Indirizzo estratto: {address}")
    
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
        log.debug("-> Overlay wait timed out, proceeding anyway...")


def handle_confirmation_form(driver, ignored_exceptions):
    try:
        log.debug("\n--- INIZIO GESTIONE FORM DI CONFERMA (CONSENSO E DATI) ---")
        
        # 3. Explicit Overlays/Spinners Removal Wait:
        log.debug("-> Attesa scomparsa di eventuali overlay/spinner...")
        safe_wait_loading(driver, timeout=3)

        # 4. Debugging Breakpoints before Consent and Confirmation
        # 2. Form Validation Event Dispatching for Phone, Email, Date
        log.debug("-> Cerco i campi di input per forzare la validazione (onBlur/onChange)...")
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[type='tel'], input[type='date'], input.form-control")
            for inp in inputs:
                if inp.is_displayed():
                    val = inp.get_attribute("value")
                    log.debug(f"-> Trovato input (type={inp.get_attribute('type')}, id={inp.get_attribute('id')}). Valore attuale: '{val}'. Dispaccio eventi...")
                    driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", inp)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", inp)
                    driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", inp)
                    time.sleep(0.2)
        except Exception as e:
            log.debug(f"-> Impossibile elaborare i campi di testo: {e}")

        # 1. Strict and Robust Checkbox Interaction
        log.debug("-> Cerco la checkbox per il consenso privacy...")
        try:
            checkboxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
            
            if len(checkboxes) == 0:
                log.debug("-> Nessun tag <input type='checkbox'> trovato. Cerco elementi custom (.checkmark, .ui-chkbox-box)...")
                checkmarks = driver.find_elements(By.CSS_SELECTOR, ".checkmark, .ui-chkbox-box")
                for check in checkmarks:
                    log.debug("-> Clicco custom checkmark via Javascript...")
                    driver.execute_script("arguments[0].click();", check)
                    time.sleep(0.5)
            else:
                for checkbox in checkboxes:
                    is_checked = driver.execute_script("return arguments[0].checked;", checkbox)
                    if not is_checked:
                        log.debug(f"-> Clicco la checkbox privacy (id={checkbox.get_attribute('id')}) via Javascript...")
                        driver.execute_script("arguments[0].click();", checkbox)
                        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", checkbox)
                        time.sleep(0.5)
                    
                    final_state = driver.execute_script("return arguments[0].checked;", checkbox)
                    log.debug(f"-> Stato finale della checkbox: {'Selezionata' if final_state else 'NON Selezionata'}")
                    
        except Exception as e:
            log.debug(f"-> Errore durante l'interazione con la checkbox: {e}")

        log.debug("-> Attesa scomparsa di eventuali overlay/spinner prima della conferma...")
        safe_wait_loading(driver, timeout=3)

        log.debug("-> Cerco il pulsante 'Conferma'...")
        try:
            # We look for the conferma button robustly
            conferma_btn = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_element_located((By.XPATH, "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'conferma') or contains(@ng-click, 'conferma') or @btn-riprenota]")))
            
            log.debug("-> Attendo che 'Conferma' sia cliccabile...")
            WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions).until(EC.element_to_be_clickable(conferma_btn))
            
            log.debug("-> Clicco su 'Conferma' (via Javascript)...")
            driver.execute_script("arguments[0].click();", conferma_btn)
            log.debug("-> Pulsante 'Conferma' cliccato con successo.")
        except TimeoutException:
             log.debug("-> Fallback: cerco selettore originale per la conferma...")
             conferma_btn = WebDriverWait(driver, 10, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_element_located((By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='riprenotaRicettaCtrl.conferma']")))
             driver.execute_script("arguments[0].click();", conferma_btn)
             log.debug("-> Pulsante 'Conferma' (fallback) cliccato con successo.")

        log.debug("--- FINE GESTIONE FORM DI CONFERMA ---\n")
        safe_wait_loading(driver, timeout=3)
        
    except Exception as e:
        log.info(f"\nERRORE IMPREVISTO durante la gestione della conferma: {e}")


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
            log.debug(f"-> Disponibilità non prenotabile online ignorata: {raw.get('text', '')[:80]!r}")
            continue
        slot = parse_slot(raw, province)
        if slot is None:
            log.info(f"-> Impossibile leggere data/ora da: {raw.get('text', '')[:100].replace(chr(10), ' ')}...")
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

    log.debug(f"-> Clicco 'Verifica e conferma' sulla disponibilità #{current.index}...")
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

    log.debug("-> Spunto la presa visione delle note...")
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
    log.info(f"-> Analizzo l'esito della ricerca per {current_province} (Attesa max: {timeout}s)...")
    
    while time.time() - start_time < timeout:
        try:
            # Check A: Error Pop-up
            error_texts = driver.find_elements(By.XPATH, "//*[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'nessuna disponibilit') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'nessun appuntamento') or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'non ci sono disponibilit') or contains(@class, 'modal-title') and contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'attenzione')]")
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
                    log.info("ATTENZIONE: il portale richiede un numero di telefono ma non è stato configurato ('telefono').")
                continue
            log.debug(f"-> Compilo il campo {field_id}...")
            field.send_keys(value)
            driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", field)

    for consent in driver.find_elements(By.ID, "consensoPrenotazione"):
        if not driver.execute_script("return arguments[0].checked;", consent):
            log.debug("-> Spunto il consenso al trattamento dati...")
            driver.execute_script("arguments[0].click();", consent)


def normalize_province(name):
    # "Milano Città", "MILANO CITTA'" and "milano  citta" all become "MILANO CITTA"
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split())


def match_province_option(options, province_name):
    wanted = normalize_province(province_name)
    if not wanted:
        return None
    exact = [o for o in options if normalize_province(o.text) == wanted]
    if exact:
        return exact[0]
    # e.g. "MONZA" -> "MONZA E DELLA BRIANZA", only if unambiguous
    partial = [o for o in options if normalize_province(o.text).startswith(wanted)]
    return partial[0] if len(partial) == 1 else None


def search_in_province(driver, ignored_exceptions, province_name, search_preferences, hints=None):
    """Fill the search form for one province and submit it.

    Returns False on a transient problem (the province is skipped this cycle); raises PortalChanged when
    the form is not what we expect and LoginRejected when the province does not exist on the portal."""
    try:
        log.debug("\n--- INIZIO INTERAZIONE FORM RICERCA ---")
        log.debug("1. Attendendo la scomparsa di eventuali caricamenti (spinner)...")
        safe_wait_loading(driver, timeout=3)
        
        # Determine current UI state
        try:
            edit_btn = driver.find_element(By.CSS_SELECTOR, "button[id='modifica-ricerca-info-testata']")
            if edit_btn.is_displayed():
                is_expanded = edit_btn.get_attribute("aria-expanded")
                if is_expanded == "false" or not is_expanded:
                    log.debug("3. (Pagina Risultati) Il menu di ricerca è chiuso. Clicco sul pulsante modifica ricerca per aprirlo...")
                    driver.execute_script("arguments[0].click();", edit_btn)
                    time.sleep(1) # Wait for dropdown animation to finish
                    safe_wait_loading(driver, timeout=3)
                else:
                    log.debug("3. (Pagina Risultati) Il menu di ricerca è GIA' aperto. Non clicco il bottone per evitare di chiuderlo.")
        except NoSuchElementException:
            pass # Form is already visible (Main page or Error popup closed)

        log.debug("4. Attendo che il selettore della provincia sia visibile e cliccabile...")
        try:
            provincia_selects = WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions)\
                .until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "select[id='provincia']")))
            provincia_select = next((s for s in provincia_selects if s.is_displayed()), None)
            if not provincia_select:
                raise TimeoutException()
        except TimeoutException:
            provincia_select = None
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            if len(iframes) > 0:
                log.debug("-> Dropdown non trovato nel DOM principale. Passo al primo iframe...")
                driver.switch_to.frame(iframes[0])
                provincia_selects = driver.find_elements(By.CSS_SELECTOR, "select[id='provincia']")
                provincia_select = next((s for s in provincia_selects if s.is_displayed()), None)
            if provincia_select is None:
                raise_missing(driver, "select#provincia", "modulo di ricerca")

        log.debug(f"5. Seleziono la provincia: {province_name}...")
        element = Select(provincia_select)
        available = [o.text.strip() for o in element.options if o.text.strip()]
        if not available:
            raise PortalChanged("select#provincia option", "elenco province vuoto", page_snippet(driver))
        option = match_province_option(element.options, province_name)
        if option is None:
            raise LoginRejected(f"Provincia '{province_name}' non disponibile sul portale. Valori disponibili: {', '.join(available)}")
        element.select_by_visible_text(option.text)
        
        log.debug("-> Eseguo evento Javascript 'change' sul dropdown...")
        driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", provincia_select)

        if hints is not None:
            apply_portal_hints(driver, hints)

        fill_contacts_form(driver, search_preferences)

        log.debug("8. Rimuovo vecchi risultati dal DOM e cerco il pulsante di sottomissione (Cerca o Aggiorna)...")
        try:
            # Purge old results to prevent false positives
            driver.execute_script("""
                var old_results = document.querySelectorAll('.lista-appuntamenti, .appuntamento-card, .container-appuntamenti');
                for (var i = 0; i < old_results.length; i++) {
                    old_results[i].remove();
                }
            """)
        except WebDriverException:
            pass

        try:
            # First try the modal update button
            confirm_btns = driver.find_elements(By.CSS_SELECTOR, ".modal-footer > .btn-primary[ng-click^='doveQuandoModalCtrl.aggiorna']")
            submit_btn = next((b for b in confirm_btns if b.is_displayed()), None)
            if submit_btn:
                log.debug("-> Trovato pulsante 'Aggiorna' (Modale). Clicco...")
                driver.execute_script("arguments[0].click();", submit_btn)
            else:
                raise NoSuchElementException()
        except NoSuchElementException:
            # Fallback to main page .submit
            log.debug("-> Scroll verso il pulsante 'Cerca' (Main Page)...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            submit_btns = driver.find_elements(By.CSS_SELECTOR, ".submit")
            submit_btn = next((b for b in submit_btns if b.is_displayed()), None)
            if submit_btn is None:
                raise_missing(driver, ".submit", "pulsante di ricerca")
            log.debug("-> Trovato pulsante 'Cerca'. Clicco...")
            driver.execute_script("arguments[0].click();", submit_btn)
        
        log.debug("9. Torno al contesto principale e attendo l'avvio della ricerca...")
        driver.switch_to.default_content()
        safe_wait_loading(driver, timeout=3)
        
        log.debug("--- FINE INTERAZIONE FORM RICERCA (SUCCESSO) ---\n")
        return True # success
        
    except PortalError:
        driver.switch_to.default_content()
        raise
    except Exception as e:
        driver.switch_to.default_content()
        if classify(e) is not ErrorKind.TRANSIENT:
            raise
        log.info(f"Problema temporaneo durante la ricerca in {province_name}: {str(e)[:200]}")
        return False


WEEKDAY_CHECKBOXES = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _set_checkbox(driver, element_id, wanted):
    for box in driver.find_elements(By.ID, element_id):
        if is_displayed_safe(box) or box.find_elements(By.XPATH, "./ancestor::label"):
            if bool(driver.execute_script("return arguments[0].checked;", box)) != wanted:
                driver.execute_script("arguments[0].click();", box)
            return True
    return False


def apply_portal_hints(driver, hints):
    """Use the portal's own weekday / morning-afternoon filters, so it returns fewer results.

    Best effort: our own filter is applied anyway, so a missing checkbox only costs performance."""
    try:
        for index, element_id in enumerate(WEEKDAY_CHECKBOXES):
            _set_checkbox(driver, element_id, index in hints.weekdays)
        _set_checkbox(driver, "mattina", hints.morning)
        _set_checkbox(driver, "pomeriggio", hints.afternoon)
    except WebDriverException as e:
        log.debug(f"-> Filtri giorni/fasce del portale non applicati: {str(e)[:100]}")


def no_availability_message(driver):
    """Text of a visible portal popup saying there is nothing bookable online, or None."""
    for modal in visible_elements(driver, ".modal-dialog"):
        try:
            text = " ".join(modal.text.split())
        except StaleElementReferenceException:
            continue
        if any(marker in text.lower() for marker in NO_AVAILABILITY_MARKERS):
            return text[:300]
    return None


def raise_missing(driver, selector, step):
    """An expected element is missing: a "no availability" popup explains it, otherwise the portal changed."""
    message = no_availability_message(driver)
    if message:
        raise NoAvailability(message)
    raise PortalChanged(selector, step, page_snippet(driver))


def page_snippet(driver, limit=1500):
    """Redacted excerpt of the visible page, for portal-change diagnostics."""
    try:
        return (driver.execute_script("return document.body ? document.body.innerText : '';") or "")[:limit]
    except WebDriverException:
        return ""

def cleanup_ui_for_next_search(driver, ignored_exceptions):
    log.debug("\n-> [PULIZIA UI] Avvio chiusura di tutti i popup e menu...")
    
    # 1. Close all Error Modals
    popups_closed = 0
    max_attempts = 5
    while popups_closed < max_attempts:
        try:
            close_btns = driver.find_elements(By.CSS_SELECTOR, ".modal-dialog button.close, .modal-dialog button[ng-click*='chiudi'], .modal-dialog .btn-default, .modal-dialog .btn-primary")
            visible_btns = [btn for btn in close_btns if btn.is_displayed() and 'conferma' not in (btn.get_attribute('ng-click') or '').lower()]
            
            if not visible_btns:
                break
                
            log.debug(f"-> Chiusura popup #{popups_closed + 1} in corso...")
            driver.execute_script("arguments[0].click();", visible_btns[0])
            popups_closed += 1
            time.sleep(0.5)
        except Exception as e:
            log.debug(f"-> Errore durante la chiusura del popup: {str(e)[:100]}")
            break
            
    if popups_closed > 0:
        log.debug("-> Attendo la completa invisibilità dei popup dal DOM...")
        try:
            WebDriverWait(driver, 5, ignored_exceptions=ignored_exceptions).until(
                EC.invisibility_of_element_located((By.CSS_SELECTOR, ".modal-dialog"))
            )
            log.debug("-> Popup completamente invisibili.")
        except TimeoutException:
            log.debug("-> Timeout attesa scomparsa popup. Procedo comunque.")

    # 2. Close Search Menu if it's open
    try:
        edit_btn = driver.find_element(By.CSS_SELECTOR, "button[id='modifica-ricerca-info-testata']")
        if edit_btn.is_displayed():
            is_expanded = edit_btn.get_attribute("aria-expanded")
            if is_expanded == "true":
                log.debug("-> [PULIZIA UI] Il menu di ricerca è rimasto aperto. Lo chiudo...")
                driver.execute_script("arguments[0].click();", edit_btn)
                time.sleep(1) # wait for collapse animation
    except NoSuchElementException:
        pass
    except Exception as e:
        log.debug(f"-> Errore chiusura menu ricerca: {e}")

    log.debug("-> Pulizia UI completata. Attendo 3 secondi prima della prossima provincia...")
    time.sleep(3)


def detect_booking_mode(driver, timeout=45):
    """After login the portal shows the current appointment if the prescription is already booked,
    otherwise it goes straight to the booking flow. Returns the mode, raises LoginRejected on a portal
    error message and PortalChanged when neither page shows up."""
    deadline = time.time() + timeout
    reported_popups = set()
    while time.time() < deadline:
        try:
            if visible_elements(driver, ".dati-appuntamento-summary"):
                return MODE_RESCHEDULE
            if visible_elements(driver, "button[ng-click^='prenotaCompletaDatiCtrl.conferma'], select#provincia, input#telefono"):
                return MODE_NEW
            for modal in visible_elements(driver, ".modal-dialog"):
                text = modal.text.strip()
                titles = [t.text.strip().lower() for t in modal.find_elements(By.CSS_SELECTOR, ".modal-title")]
                if any(marker in text.lower() for marker in NO_AVAILABILITY_MARKERS):
                    raise NoAvailability(" ".join(text.split())[:300])
                if any("errore" in t or "attenzione" in t for t in titles):
                    raise LoginRejected(text.replace("\n", " ")[:400])
                if text not in reported_popups:
                    reported_popups.add(text)
                    log.info(f"-> Popup del portale: {text[:200]}")
        except StaleElementReferenceException:
            pass
        time.sleep(0.5)
    raise PortalChanged(".dati-appuntamento-summary | select#provincia", "dopo il login", page_snippet(driver))


def handle_completa_dati_modal(driver, search_preferences, ignored_exceptions, ask=None):
    """'Completa dati ricetta' modal, shown only for some prescriptions on a first booking.

    `ask(question) -> str` lets the command line ask the user; without it the missing data is reported
    as LoginRejected, so the user can complete the search settings."""
    try:
        conferma = WebDriverWait(driver, 8, ignored_exceptions=ignored_exceptions)\
            .until(EC.visibility_of_element_located((By.CSS_SELECTOR, "button[ng-click^='prenotaCompletaDatiCtrl.conferma']")))
    except TimeoutException:
        return
    log.info("-> Il portale chiede di completare i dati della ricetta.")

    radios = driver.find_elements(By.CSS_SELECTOR, ".modal-dialog input[name='controllo']")
    if len(radios) >= 2:
        controllo = search_preferences.visita_controllo
        if controllo is None:
            if ask is None:
                raise LoginRejected("Il portale chiede se la ricetta riporta la dicitura CONTROLLO o FOLLOW-UP: indicalo nelle impostazioni della ricerca.")
            controllo = ask("La ricetta riporta la dicitura CONTROLLO o FOLLOW-UP? S/N: ").strip().upper() in ("S", "SI", "SÌ", "Y")
        # The first radio is "Sì", the second "No"
        driver.execute_script("arguments[0].click();", radios[0] if controllo else radios[1])

    rur2 = [f for f in driver.find_elements(By.ID, "rur2") if is_displayed_safe(f)]
    if rur2:
        log.info("La ricetta (rossa) richiede il codice RUR riportato sulla ricetta.")
        if ask is None:
            raise LoginRejected("La ricetta rossa richiede il codice RUR: questa ricerca va avviata dal programma sul computer.")
        for field_id, label in (("rur1", "Codice RUR - prima parte (S): "), ("rur2", "Codice RUR - seconda parte (Y): ")):
            for field in driver.find_elements(By.ID, field_id):
                if is_displayed_safe(field):
                    field.send_keys(ask(label).strip())
                    driver.execute_script("arguments[0].dispatchEvent(new Event('blur', { bubbles: true }));", field)

    WebDriverWait(driver, 20, ignored_exceptions=ignored_exceptions).until(EC.element_to_be_clickable(conferma))
    driver.execute_script("arguments[0].click();", conferma)
    safe_wait_loading(driver, timeout=5)


def prepare_new_booking(driver, search_preferences, ignored_exceptions, ask=None):
    handle_completa_dati_modal(driver, search_preferences, ignored_exceptions, ask)
    try:
        WebDriverWait(driver, 30, ignored_exceptions=ignored_exceptions)\
            .until(lambda d: visible_elements(d, "select#provincia"))
    except TimeoutException:
        raise PortalChanged("select#provincia", "modulo 'Dove e quando' della prima prenotazione", page_snippet(driver)) from None
    fill_contacts_form(driver, search_preferences)


def open_search_flow(driver, prescription, search_preferences, ignored_exceptions, ask=None):
    """Log in from the home page and bring the browser to the search form.

    Returns (mode, current_appointment); current_appointment is None for a first booking."""
    driver.get(BASE_URL)
    handle_initial_navigation(driver, ignored_exceptions)
    perform_login(driver, prescription, ignored_exceptions)

    mode = detect_booking_mode(driver)

    if mode == MODE_RESCHEDULE:
        current_appointment = get_current_appointment(driver, ignored_exceptions, prescription)
        log.info("L'appuntamento attuale è fissato per il giorno %s presso %s", current_appointment.date, current_appointment.address)

        # Click on edit appointment
        log.info("\n-> Clicco sul pulsante per modificare l'appuntamento (btn-riprenota)...")
        buttons = driver.find_elements(By.CSS_SELECTOR, "button[btn-riprenota='']")
        if not buttons:
            raise PortalChanged("button[btn-riprenota]", "modifica appuntamento", page_snippet(driver))
        driver.execute_script("arguments[0].click();", buttons[0])

        # Handle the confirmation modal (with privacy checkbox and inputs)
        handle_confirmation_form(driver, ignored_exceptions)
        return mode, current_appointment

    log.info("Nessun appuntamento associato alla ricetta: cerco una data per la PRIMA PRENOTAZIONE.\n")
    prepare_new_booking(driver, search_preferences, ignored_exceptions, ask)
    return mode, None

