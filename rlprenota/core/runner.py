"""One search session on the portal: scan provinces, ask the user, book. Shared by the CLI and the NAS workers."""
import calendar
import logging
import time

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException

from rlprenota.core import portal
from rlprenota.core.decisions import ACCEPT, BOOKED, EXPIRED, REJECT
from rlprenota.core.errors import LoginRejected, NoAvailability, PortalChanged, PortalError, PortalHiccup
from rlprenota.core.filters import Verdict
from rlprenota.core.models import ANTICIPA, ENTRAMBE, POSTICIPA

log = logging.getLogger(__name__)
POSTPONE_RESULT_PAGES = 30
SLOW_SEARCH_SECONDS = 45     # after the first 15 s

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
        self.booked_slot = None              # set when this session books something

    def open(self, attempts=1, retry_delay=30):
        for attempt in range(1, attempts + 1):
            try:
                self.mode, self.current_appointment = portal.open_search_flow(
                    self.driver, self.prescription, self.prefs, self.ignored_exceptions, self.ask)
                if self.direction == POSTICIPA and self.current_appointment is None:
                    raise LoginRejected("la ricetta non ha un appuntamento prenotato da posticipare: disattiva "
                                        "«Posticipa» per prenotare la prima data utile")
                return
            except (PortalChanged, PortalHiccup) as e:
                # Usually the portal ending the session or loading slowly: start again with a clean page
                if attempt == attempts:
                    raise
                log.info(f"Accesso non riuscito ({str(e)[:150]}): riprovo da capo...")
                if self.driver is not None:
                    # Best effort: the next attempt reloads the page anyway
                    try:
                        self.driver.delete_all_cookies()
                    except Exception as cleanup_error:
                        log.debug(f"Pulizia dei cookie non riuscita: {type(cleanup_error).__name__}")
                time.sleep(retry_delay)
            except PortalError:
                raise  # wrong data or "no availability": retrying now won't help
            except Exception as e:
                if attempt == attempts:
                    raise
                log.info(f"Errore durante l'accesso al portale ({str(e)[:200]}). Nuovo tentativo tra {retry_delay} secondi...")
                time.sleep(retry_delay)

    def provinces(self):
        return self.filter.provinces_to_search()

    @property
    def direction(self):
        return getattr(self.prefs, "direzione", ANTICIPA) or ANTICIPA

    @property
    def postponing(self):
        """Slots after the booked appointment are wanted (postpone or hybrid mode)."""
        return self.direction in (POSTICIPA, ENTRAMBE) and self.current_appointment is not None

    def postpone_limit(self):
        """Latest acceptable slot when postponing: the end date, or one month after the booked appointment."""
        if self.filter.end_date:
            return self.prefs.get_end_date_datetime().replace(hour=23, minute=59)
        return one_month_after(self.current_appointment.get_datetime()).replace(hour=23, minute=59)

    def is_better(self, slot):
        verdict = self.filter.accepts(slot)
        if not verdict.ok or self.current_appointment is None:
            return verdict
        booked = self.current_appointment.get_datetime()
        if slot.when == booked:
            return Verdict(False, "è l'appuntamento attuale")
        if slot.when < booked:
            if self.direction == POSTICIPA:
                return Verdict(False, "non successivo all'appuntamento attuale")
            return verdict
        if self.direction == ANTICIPA:
            return Verdict(False, "non precedente all'appuntamento attuale")
        if slot.when > self.postpone_limit():
            return Verdict(False, "oltre il limite per posticipare")
        return verdict

    def upper_bound(self):
        if self.postponing:
            return self.postpone_limit()
        bounds = []
        if self.current_appointment is not None:
            bounds.append(self.current_appointment.get_datetime())
        if self.filter.end_date:
            bounds.append(self.prefs.get_end_date_datetime().replace(hour=23, minute=59))
        return min(bounds) if bounds else None

    def portal_hints(self):
        hints = self.filter.portal_hints()
        # Only postponing: ask the portal for results from the booked day (hybrid needs the earlier ones too)
        hints.start_from = (self.current_appointment.get_datetime().date()
                            if self.postponing and self.direction == POSTICIPA else None)
        return hints

    def max_result_pages(self):
        # Later slots are further down the list (the portal may ignore the start date; hybrid doesn't set it)
        return POSTPONE_RESULT_PAGES if self.postponing else portal.MAX_RESULT_PAGES


def one_month_after(when):
    """Same day of the next month (the last day of it when that day doesn't exist, e.g. 31/01 -> 28/02)."""
    year, month = (when.year + 1, 1) if when.month == 12 else (when.year, when.month + 1)
    day = min(when.day, calendar.monthrange(year, month)[1])
    return when.replace(year=year, month=month, day=day)


def find_candidate(session, province, skip):
    """Return the earliest slot (in the results of `province`) worth proposing or booking, or None."""
    bound = session.upper_bound()
    for page in range(session.max_result_pages()):
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
        session.booked_slot = slot
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
    hints = session.portal_hints()
    provinces = list(session.provinces())
    if not provinces:
        # e.g. facilities saved without their province: never report "nothing new" for a search that checks nothing
        raise LoginRejected("nessuna provincia da cercare: indica la provincia di ogni struttura e salva la ricerca")
    index, retried, fresh_login, skipped_in_a_row = 0, False, False, 0
    shown_province = None    # whose results the page shows (None: none of this cycle's)
    while index < len(provinces):
        prov = provinces[index]
        log.info(f"\n>>> Ricerca nella provincia: {prov} <<<")
        if session.before_search:
            session.before_search()

        # The previous province's list must be gone before the results are read (same province: may be identical)
        previous_results = portal.results_fingerprint(driver) if shown_province not in (None, prov) else ""
        try:
            searched = portal.search_in_province(driver, ignored_exceptions, prov, session.prefs, hints)
        except (PortalChanged, PortalHiccup) as e:
            # Most of the time the portal ended the session or reloaded the page: log in again and retry once.
            if retried:
                raise    # still wrong right after a fresh login: report it
            log.info(f"-> {prov}: pagina inattesa ({str(e)[:120]}), accedo di nuovo e riprovo...")
            retried = True
            session.open(attempts=3)
            shown_province = None
            fresh_login = True
            continue
        except NoAvailability as e:
            # The portal says there's nothing online right now and drops the search form: a normal result.
            log.info(f"-> {prov}: il portale non ha disponibilità online al momento ({e}).")
            if fresh_login and not retried and skipped_in_a_row:
                return CONTINUE   # refused again right after a fresh login: the portal has nothing now, retry later
            if retried:
                index, retried = index + 1, False   # refused twice: skip this province
                skipped_in_a_row += 1
            else:
                retried = True                      # log in again to get the form back, then retry this province
            try:
                session.open(attempts=3)
                shown_province = None
            except NoAvailability:
                return CONTINUE
            fresh_login = True
            continue
        fresh_login, retried, skipped_in_a_row = False, False, 0
        index += 1

        if not searched:
            log.info(f"Ricerca in {prov} interrotta a causa di un errore temporaneo nel form.")
            portal.cleanup_ui_for_next_search(driver, ignored_exceptions)
            continue

        outcome = portal.check_search_outcome(driver, prov, stale=previous_results)
        if outcome == "TIMEOUT":
            # The portal sometimes takes 30 s or more (often the first search after logging in)
            log.info(f"L'interfaccia in {prov} sta impiegando più tempo del previsto, attendo fino a {SLOW_SEARCH_SECONDS} secondi...")
            outcome = portal.check_search_outcome(driver, prov, timeout=SLOW_SEARCH_SECONDS, stale=previous_results)

        if outcome == "RESULTS":
            shown_province = prov
            log.info(f"\n-> Trovati appuntamenti in {prov}! Elaborazione...")
            result = process_results(session, prov)
            if result == "STOP":
                return STOP
            if result == "RESET":
                # The page is no longer on the search results (booking page or error): log in again
                log.info("-> Riparto dalla pagina iniziale del portale...")
                session.open(attempts=3)
                shown_province = None
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
        try:
            if session.mode is None:
                session.open()
            if run_cycle(session) == STOP:
                return
        except NoAvailability as e:
            log.info(f"Al momento il portale non ha disponibilità online ({e}). Riprovo più tardi.")
            session.mode = None
        log.info(f"\n   FINE CICLO #{iteration}: pausa di {session.prefs.refresh_frequency} secondi prima di ricominciare...")
        session.decider.idle(session, session.prefs.refresh_frequency)
        iteration += 1
