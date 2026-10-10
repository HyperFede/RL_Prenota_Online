"""Worker ("slave"): leases one search at a time from the master and runs one search cycle with its browser.

It holds no database and no keys: the master hands over the decrypted login data for the duration of a
lease only, and every decision goes back to the master. A heartbeat keeps the lease alive; a watchdog
kills a stuck browser (the run is then reported as a transient error and retried later).
"""
import logging
import threading
import time

from rlprenota.core.deciders import Decider
from rlprenota.core.decisions import MemoryDecisionStore
from rlprenota.core.errors import ErrorKind, NoAvailability, PortalChanged, classify
from rlprenota.core.models import Patient, Prescription
from rlprenota.core.redact import redact
from rlprenota.core.runner import STOP, SearchSession, run_cycle
from rlprenota.master.validation import SearchSecrets, SearchSettings
from rlprenota.worker.client import LeaseLost, MasterUnavailable

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
RUN_TIMEOUT_SECONDS = 10 * 60      # login + all provinces, not counting the time spent waiting for the user
IDLE_POLL_SECONDS = 5


class Watchdog:
    """Calls `on_timeout` if the run takes longer than its budget; waiting for the user extends the budget."""

    def __init__(self, seconds, on_timeout, clock=time.monotonic):
        self.deadline = clock() + seconds
        self.on_timeout = on_timeout
        self.clock = clock
        self.fired = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def extend(self, seconds):
        self.deadline += seconds

    def _run(self):
        while not self._stop.wait(1):
            if self.clock() > self.deadline:
                self.fired = True
                log.warning("Watchdog: ricerca bloccata, chiudo il browser")
                self.on_timeout()
                return

    def stop(self):
        self._stop.set()


class Heartbeat:
    def __init__(self, client, lease_id, every=HEARTBEAT_SECONDS):
        self.client, self.lease_id, self.every = client, lease_id, every
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(self.every):
            try:
                self.client.heartbeat(self.lease_id)
            except LeaseLost:
                self.lost = True
                return
            except MasterUnavailable as e:
                log.warning(f"Heartbeat non riuscito: {e}")

    def stop(self):
        self._stop.set()


class RemoteDecisionStore(MemoryDecisionStore):
    """The lease's snapshot of the decisions, with every change sent back to the master."""

    def __init__(self, client, lease_id, discarded, proposals):
        self.client, self.lease_id = client, lease_id
        super().__init__(discarded, proposals)

    def remember_proposal(self, slot, message_id):
        """The master already recorded this proposal: update the local snapshot only."""
        self.proposals[slot.slot_id] = {"slot": slot.to_dict(), "message_id": message_id, "status": "pending", "ts": int(time.time())}

    def _saved_proposal(self, slot_id):
        p = self.proposals[slot_id]
        self.client.store(self.lease_id, "status", slot_id=slot_id, status=p["status"], slot=p["slot"], message_id=p.get("message_id"))

    def _saved_discard(self, slot_id):
        self.client.store(self.lease_id, "discard", slot_id=slot_id, slot=self.discarded[slot_id])

    def _removed_discard(self, slot_id):
        self.client.store(self.lease_id, "undiscard", slot_id=slot_id)


class MasterDecider(Decider):
    """Proposals go through the master (its Telegram bot); the worker waits for the owner's answer."""

    def __init__(self, client, lease_id, timeout_minutes, watchdog=None, clock=time.monotonic):
        self.client, self.lease_id, self.timeout_minutes = client, lease_id, timeout_minutes
        self.watchdog = watchdog
        self.clock = clock
        self.waited = 0.0       # time spent waiting for the user (not part of the cycle duration)

    def propose(self, session, slot):
        current = session.current_appointment.date if session.current_appointment else ""
        message_id = self.client.propose(self.lease_id, slot, current, session.prefs.dry_run, self.timeout_minutes)
        session.store.remember_proposal(slot, message_id)
        budget = self.timeout_minutes * 60
        if self.watchdog:
            self.watchdog.extend(budget + 30)
        start = self.clock()
        deadline = start + budget
        try:
            while self.clock() < deadline:
                answer = self.client.decision(self.lease_id, slot.slot_id, wait=min(25, max(1, deadline - self.clock())))
                if answer:
                    return answer
            self.client.expire(self.lease_id, slot.slot_id)
            return None
        finally:
            self.waited += self.clock() - start

    def update(self, session, slot_id, footer, keep_buttons=False):
        self.client.update(self.lease_id, slot_id, footer, keep_buttons)

    def notify(self, text):
        self.client.notify(self.lease_id, text)


class Worker:
    def __init__(self, client, browsers, name="worker", sleep=time.sleep, run_timeout=RUN_TIMEOUT_SECONDS):
        self.client = client
        self.browsers = browsers
        self.name = name
        self.sleep = sleep
        self.run_timeout = run_timeout

    def run_forever(self, stop_event):
        while not stop_event.is_set():
            try:
                worked = self.run_once()
            except MasterUnavailable as e:
                log.warning(f"Master non raggiungibile: {e}")
                worked = False
            if not worked:
                stop_event.wait(IDLE_POLL_SECONDS)

    def run_once(self):
        """Lease and run one search cycle. Returns False when there was nothing to do."""
        lease = self.client.lease()
        if not lease:
            return False
        lease_id = lease["lease_id"]
        started = time.monotonic()
        heartbeat = Heartbeat(self.client, lease_id).start()
        watchdog = Watchdog(self.run_timeout, self.browsers.kill).start()
        result = {"outcome": "continue"}
        session = decider = None
        try:
            settings = SearchSettings(**lease["settings"])
            secrets = SearchSecrets.from_dict(lease["secrets"])
            prefs = settings.to_preferences(secrets)
            store = RemoteDecisionStore(self.client, lease_id, lease["discarded"], lease["proposals"])
            decider = MasterDecider(self.client, lease_id, settings.telegram_timeout, watchdog)
            session = SearchSession(self.browsers.get(), Prescription(secrets.ricetta, Patient(secrets.codice_fiscale, secrets.tessera)),
                                    prefs, store, decider, before_search=lambda: self.sleep(self.client.permit(lease_id)))
            session.open()
            outcome = run_cycle(session)
            if outcome == STOP or session.booked_slot is not None:
                # A completed booking is reported even if the watchdog fired afterwards
                slot = session.booked_slot
                result = {"outcome": "booked", "message": f"Prenotato: {slot.date_str} {slot.time_str}, {slot.sede or slot.azienda}"
                          if slot else "Prenotato"}
            elif watchdog.fired:
                raise TimeoutError("Ricerca interrotta dal watchdog: troppo lenta")
        except LeaseLost:
            log.warning("Lease perso durante la ricerca: risultato scartato")
            return True
        except NoAvailability:
            # Normal: nothing bookable online right now (e.g. right after login). Not an error.
            result = {"outcome": "continue", "message": "Al momento il portale non ha disponibilità online: continuo a controllare"}
        except Exception as e:
            kind = ErrorKind.TRANSIENT if watchdog.fired else classify(e)
            result = {"outcome": "error", "error_kind": kind.value, "message": redact(str(e))[:300]}
            if isinstance(e, PortalChanged):
                result.update(selector=e.selector, step=e.step, message=f"{e} | {e.dom_snippet[:200]}")
            log.info(f"Ricerca terminata con errore ({kind.value}): {result['message'][:200]}")
            if kind in (ErrorKind.TRANSIENT, ErrorKind.INTERNAL):
                self.browsers.kill()  # don't reuse a browser in an unknown state
        finally:
            heartbeat.stop()
            watchdog.stop()
            self.browsers.reset()

        if session is not None and session.current_appointment is not None:
            result["current_appointment"] = session.current_appointment.date
        result["duration"] = time.monotonic() - started - (decider.waited if decider else 0)
        try:
            self.client.complete(lease_id, **result)
        except LeaseLost:
            log.warning("Lease scaduto prima della fine: il master riproporrà la ricerca")
        return True
