"""Command-line version: runs one search on this computer with a visible browser.

The NAS service (rlprenota.master / rlprenota.worker) runs the same search logic for several users.
"""
import logging
import sys
from os import path

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from rlprenota.core import portal
from rlprenota.core.deciders import TelegramDecider, TerminalDecider
from rlprenota.core.decisions import DecisionStore
from rlprenota.core.errors import PortalError
from rlprenota.core.models import Patient, Prescription, SearchPreferences
from rlprenota.core.redact import RedactingFilter
from rlprenota.core.runner import SearchSession, search_loop
from rlprenota.telegram import TelegramBot

log = logging.getLogger("rlprenota")


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
                                           continua_dopo_prenotazione=getattr(data_file, 'continua_dopo_prenotazione', False),
                                           location_mode=getattr(data_file, 'modalita_luogo', "provinces"),
                                           home_comune=getattr(data_file, 'comune_di_partenza', ""),
                                           max_km=getattr(data_file, 'distanza_massima_km', None),
                                           facilities=getattr(data_file, 'strutture', []),
                                           weekdays=getattr(data_file, 'giorni_settimana', None),
                                           time_from=getattr(data_file, 'orario_da', None),
                                           time_to=getattr(data_file, 'orario_a', None))

    return prescription, search_preferences


def setup_logging(debug=False):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger("rlprenota")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False


def main():
    setup_logging("--debug" in sys.argv)
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

    try:
        search_filter = search_preferences.build_filter()
    except ValueError as e:
        sys.exit(f"Impostazioni di ricerca non valide: {e}")

    bot = TelegramBot(search_preferences.telegram_token, search_preferences.telegram_chat_id)
    decider = TelegramDecider(bot, search_preferences.telegram_timeout_minuti) if bot.enabled else TerminalDecider()
    store = DecisionStore(prescription.prescription_n)
    if store.discarded:
        print(f"-> {len(store.discarded)} appuntamenti scartati in precedenza verranno ignorati.")

    # Ask which browser to use
    driver = portal.use_chrome() if input("Scrivi 1 per usare Chrome oppure 2 per Firefox: ") == "1" else portal.use_firefox()
    driver.set_window_size(1400,1000)

    ignored_exceptions = (NoSuchElementException, StaleElementReferenceException)
    session = SearchSession(driver, prescription, search_preferences, store, decider, search_filter,
                            ignored_exceptions, ask=input)

    try:
        session.open()
        where = ", ".join(session.provinces())
        if session.mode == portal.MODE_RESCHEDULE:
            decider.notify(f"🔎 Ricerca avviata: cerco una data precedente al {session.current_appointment.date} in {where}.")
        else:
            decider.notify(f"🔎 Ricerca avviata per la PRIMA prenotazione in {where}.")

        # Start search loop over multiple provinces
        search_loop(session)
        
        # Close the browser and end script
        print("Grazie per aver combattuto insieme contro la sanità privata <3")
        driver.quit()
        sys.exit()

    except PortalError as e:
        print(f"Ricerca interrotta: {e}")
        decider.notify(f"❌ Ricerca interrotta: {str(e)[:300]}")
        driver.quit()
    except Exception as e:
        print(f"Si è verificato un errore: {e}")
        decider.notify(f"❌ Ricerca interrotta per un errore: {str(e)[:300]}")
        driver.quit()

if __name__ == "__main__":
    main()
