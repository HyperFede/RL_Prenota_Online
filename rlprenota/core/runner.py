"""One search session on the portal: scan provinces, ask the user, book. Shared by the CLI and the NAS workers."""
import logging
import time

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from rlprenota.core import portal
from rlprenota.core.decisions import ACCEPT, BOOKED, EXPIRED, REJECT
from rlprenota.core.errors import PortalError
from rlprenota.core.filters import Verdict

log = logging.getLogger(__name__)

DEFAULT_IGNORED = (NoSuchElementException, StaleElementReferenceException)

# Outcomes of run_cycle
CONTINUE = "continue"
STOP = "stop"


class SearchSession:
    def __init__(self, driver, prescription, search_preferences, store, decider, search_filter=None,
                 ignored_exceptions=DEFAULT_IGNORED, ask=None, before_search=None):
        self.driver = driver
        self.prescription = prescription
        self.prefs = search_preferences
        self.store = store
        self.decider = decider
        self.filter = search_filter or search_preferences.build_filter()
        self.ignored_exceptions = ignored_exceptions
        self.ask = ask                       # terminal questions (CLI only)
        self.before_search = before_search   # e.g. the global rate limiter on the NAS
        self.mode = None
        self.current_appointment = None

    def open(self, attempts=1, retry_delay=30):
        for attempt in range(1, attempts + 1):
            try:
                self.mode, self.current_appointment = portal.open_search_flow(
                    self.driver, self.prescription, self.prefs, self.ignored_exceptions, self.ask)
                return
            except PortalError:
                raise  # retrying won't help: wrong data or a changed portal
            except Exception as e:
                if attempt == attempts:
                    raise
                log.info(f"Errore durante l'accesso al portale ({str(e)[:200]}). Nuovo tentativo tra {retry_delay} secondi...")
                time.sleep(retry_delay)

    def provinces(self):
        return self.filter.provinces_to_search()

    def is_better(self, slot):
        verdict = self.filter.accepts(slot)
        if not verdict.ok:
            return verdict
        if self.current_appointment is not None and slot.when >= self.current_appointment.get_datetime():
            return Verdict(False, "non precedente all'appuntamento attuale")
        return verdict

    def upper_bound(self):
        bounds = []
        if self.current_appointment is not None:
            bounds.append(self.current_appointment.get_datetime())
        if self.filter.end_date:
            bounds.append(self.prefs.get_end_date_datetime().replace(hour=23, minute=59))
        return min(bounds) if bounds else None


def find_candidate(session, province, skip):
    """Return the earliest slot (in the results of `province`) worth proposing or booking, or None."""
    bound = session.upper_bound()
    for page in range(portal.MAX_RESULT_PAGES):
        slots = portal.extract_slots(session.driver, province)
        best = None
        for slot in slots:
            if slot.slot_id in skip:
                continue
            verdict = session.is_better(slot)
            if not verdict.ok:
                log.debug(f"-> {slot.date_str} {slot.time_str} {slot.comune}: {verdict.reason}")
                continue
            if session.store.is_discarded(slot):
                log.info(f"-> {slot.date_str} {slot.time_str} presso {slot.sede or slot.azienda}: scartato da te in precedenza, lo ignoro.")
                continue
            if not (session.store.is_pre_accepted(slot) or session.store.should_propose(slot)):
                log.debug(f"-> {slot.date_str} {slot.time_str}: già proposto in questa sessione senza risposta.")
                continue
            if best is None or slot.when < best.when:
                best = slot
        if best:
            return best
        # Results are sorted by date: once a page starts after the limit, the next ones are even later
        if not slots or (bound and min(s.when for s in slots) >= bound):
            return None
        if not portal.go_to_next_results_page(session.driver):
            return None
        log.debug(f"-> Passo alla pagina {page + 2} dei risultati...")
    return None


def process_results(session, province):
    """Handle the results shown for a province. Returns "NEXT", "RESET" or "STOP"."""
    store, decider = session.store, session.decider
    handled = set()
    while True:
        slot = find_candidate(session, province, handled)
        if slot is None:
            log.info(f"-> Nessuna nuova disponibilità migliore in {province}.")
            return "NEXT"
        handled.add(slot.slot_id)

        if store.is_pre_accepted(slot):
            log.info(f"\n!!! Ritrovato l'appuntamento che avevi accettato: {slot.date_str} {slot.time_str} ({province}) !!!")
            decision = ACCEPT
        else:
            log.info(f"\n!!! TROVATA DISPONIBILITA' MIGLIORE IN {slot.provincia} !!!\n{slot.describe()}")
            decision = decider.propose(session, slot)

        if decision == REJECT:
            store.discard(slot)
            log.info("-> Appuntamento scartato: non verrà più proposto. Continuo la ricerca.")
            decider.update(session, slot.slot_id, "❌ Scartato: non ti verrà più proposto. La ricerca continua.")
            continue
        if decision is None:
            store.set_status(slot.slot_id, EXPIRED)
            log.info("-> Nessuna risposta: continuo la ricerca (non lo riproporrò in questa sessione).")
            minutes = session.prefs.telegram_timeout_minuti
            decider.update(session, slot.slot_id,
                           f"⌛ Nessuna risposta entro {minutes} minuti: la ricerca è ripresa.\n"
                           "Puoi ancora rispondere: se premi Prenota, lo prenoterò appena lo ritrovo disponibile.",
                           keep_buttons=True)
            continue

        log.info("-> Appuntamento accettato. Procedo con la prenotazione...")
        decider.update(session, slot.slot_id, "✅ Accettato: prenotazione in corso...")
        portal_error = None
        try:
            outcome, details = portal.book_slot(session.driver, session.ignored_exceptions, slot, session.prefs.dry_run)
        except Exception as e:
            outcome, details = "failed", f"errore imprevisto: {str(e)[:200]}"
            portal_error = e if isinstance(e, PortalError) else None

        if outcome == "dry_run":
            store.set_status(slot.slot_id, EXPIRED)
            log.info("\n[DRY RUN] Riepilogo verificato, l'appuntamento NON è stato prenotato. Riprendo la ricerca...")
            decider.update(session, slot.slot_id, "🧪 DRY RUN: riepilogo verificato sul portale ma NON prenotato. La ricerca continua.")
            return "NEXT"

        if outcome == "failed":
            store.set_status(slot.slot_id, EXPIRED)
            log.info(f"\nPrenotazione NON riuscita: {details}. Riprendo la ricerca...")
            decider.update(session, slot.slot_id, f"⚠️ Prenotazione non riuscita: {details}\nLa ricerca continua.")
            if portal_error is not None:
                raise portal_error
            return "RESET"

        store.set_status(slot.slot_id, BOOKED)
        if session.current_appointment is not None:
            session.current_appointment.change_app(slot.appointment_date_str, slot.address)
        log.info(f"\n!!! APPUNTAMENTO PRENOTATO CON SUCCESSO !!!\n{slot.describe()}")
        if details:
            log.info("Note importanti:\n" + details)
        notes = f"\n\n📝 Note:\n{details[:1500]}" if details else ""
        decider.update(session, slot.slot_id, f"🎉 PRENOTATO CON SUCCESSO!{notes}")

        if session.prefs.continua_dopo_prenotazione:
            decider.notify("🔎 Continuo a cercare una data ancora migliore di quella appena prenotata.")
            return "RESET"
        return "STOP"


def run_cycle(session):
    """Search every province once. The session must already be open. Returns CONTINUE or STOP.

    PortalError subclasses propagate: the caller decides about retries and alerts."""
    driver, ignored_exceptions = session.driver, session.ignored_exceptions
    hints = session.filter.portal_hints()
    for prov in session.provinces():
        log.info(f"\n>>> Ricerca nella provincia: {prov} <<<")
        if session.before_search:
            session.before_search()

        if not portal.search_in_province(driver, ignored_exceptions, prov, session.prefs, hints):
            log.info(f"Ricerca in {prov} interrotta a causa di un errore temporaneo nel form.")
            portal.cleanup_ui_for_next_search(driver, ignored_exceptions)
            continue

        outcome = portal.check_search_outcome(driver, prov)
        if outcome == "TIMEOUT":
            log.info(f"L'interfaccia in {prov} sta impiegando più tempo del previsto, riprovo per altri 15 secondi...")
            outcome = portal.check_search_outcome(driver, prov, timeout=15)

        if outcome == "RESULTS":
            log.info(f"\n-> Trovati appuntamenti in {prov}! Elaborazione...")
            result = process_results(session, prov)
            if result == "STOP":
                return STOP
            if result == "RESET":
                # The page is no longer on the search results (booking page or error): log in again
                log.info("-> Riparto dalla pagina iniziale del portale...")
                session.open(attempts=3)
                continue
        elif outcome == "ERROR":
            log.info(f"\n-> Nessun appuntamento utile trovato in {prov}.")
        else:
            log.info(f"\n-> La pagina in {prov} non ha caricato risultati validi. Salto la provincia.")
        portal.cleanup_ui_for_next_search(driver, ignored_exceptions)
    return CONTINUE


def search_loop(session):
    """Command-line loop: search, wait, repeat until something is booked."""
    iteration = 1
    while True:
        log.info(f"\n=============================================\n   INIZIO CICLO DI RICERCA GLOBALE #{iteration}\n"
                 f"=============================================")
        if run_cycle(session) == STOP:
            return
        log.info(f"\n   FINE CICLO #{iteration}: pausa di {session.prefs.refresh_frequency} secondi prima di ricominciare...")
        session.decider.idle(session, session.prefs.refresh_frequency)
        iteration += 1
