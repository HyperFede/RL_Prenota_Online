"""Hands out search runs to workers (leases) and applies the error policy.

- A lease gives one worker exclusive use of one search for one cycle; heartbeats keep it alive and an
  expired lease (crashed worker) is simply requeued.
- Each search asks for its refresh interval (min 60 s); when the workers can't keep up, every interval
  stretches fairly to `active searches x average cycle / workers`.
- Errors: transient -> exponential backoff and, after 5 in a row, pause + alerts; login rejected ->
  pause and tell the user; portal changed on 2+ searches within 30 min -> global pause + admin alert;
  throttled -> global pause for 10 minutes + admin alert.
- permit() spaces portal searches across all workers (politeness / anti-ban).
"""
import json
import random
import threading
import time
from dataclasses import dataclass

from rlprenota.master.crypto import new_token
from rlprenota.master.validation import SearchSettings

LEASE_TTL_SECONDS = 120
MAX_BACKOFF_SECONDS = 480
CIRCUIT_BREAKER_FAILURES = 5
PORTAL_CHANGE_WINDOW_SECONDS = 30 * 60
PORTAL_CHANGE_SEARCHES = 2
THROTTLE_PAUSE_SECONDS = 10 * 60
PORTAL_SEARCH_SPACING_SECONDS = 3.0


class LeaseLost(Exception):
    """The lease expired (or never existed): the worker must drop the run."""


@dataclass
class Lease:
    lease_id: str
    search_id: str
    user_id: int
    settings: SearchSettings
    secrets: object
    current_appointment: str
    discarded: dict
    proposals: dict


class Scheduler:
    def __init__(self, db, searches, alerts, clock=time.time, workers=2, rng=random.random):
        self.db = db
        self.searches = searches
        self.alerts = alerts          # notify_user(user_id, text), alert_admin(key, text)
        self.clock = clock
        self.workers = max(1, workers)
        self.rng = rng
        self._permit_lock = threading.Lock()
        self._next_permit = 0.0
        self._portal_changes = []     # (time, search_id)

    # --- global state ---

    def globally_paused(self):
        if self.db.get_setting("global_pause", ""):
            return True
        return float(self.db.get_setting("pause_until", 0) or 0) > self.clock()

    def pause_global(self, reason):
        self.db.set_setting("global_pause", reason[:300])

    def resume_global(self):
        self.db.set_setting("global_pause", "")
        self.db.set_setting("pause_until", 0)
        self._portal_changes.clear()

    def permit(self):
        """Seconds the caller must wait before its next portal search."""
        with self._permit_lock:
            now = self.clock()
            slot = max(now, self._next_permit)
            self._next_permit = slot + PORTAL_SEARCH_SPACING_SECONDS * (0.9 + 0.2 * self.rng())
            return round(slot - now, 3)

    # --- leases ---

    def lease(self, worker):
        if self.globally_paused():
            return None
        now = self.clock()
        with self.db.tx() as conn:
            row = conn.execute("""SELECT s.id, s.user_id, s.settings_json, s.current_appointment FROM searches s
                                  LEFT JOIN leases l ON l.search_id = s.id
                                  WHERE s.status IN ('active', 'waiting') AND s.next_run_at <= ?
                                    AND s.secrets_enc IS NOT NULL AND (l.search_id IS NULL OR l.expires_at < ?)
                                  ORDER BY s.next_run_at LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            lease_id = new_token(16)
            conn.execute("""INSERT INTO leases(search_id, lease_id, worker, started_at, expires_at) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(search_id) DO UPDATE SET lease_id = excluded.lease_id, worker = excluded.worker,
                                started_at = excluded.started_at, expires_at = excluded.expires_at""",
                         (row["id"], lease_id, worker[:40], now, now + LEASE_TTL_SECONDS))
        store = self.searches.decisions(row["id"])
        return Lease(lease_id, row["id"], row["user_id"], SearchSettings.from_json(row["settings_json"]),
                     self.searches.secrets_for(row["id"]), row["current_appointment"], dict(store.discarded),
                     dict(store.proposals))

    def lease_row(self, lease_id):
        row = self.db.query_one("""SELECT l.*, s.user_id FROM leases l JOIN searches s ON s.id = l.search_id
                                   WHERE l.lease_id = ?""", (lease_id,))
        if row is None or row["expires_at"] < self.clock():
            raise LeaseLost()
        return row

    def heartbeat(self, lease_id, extra_seconds=LEASE_TTL_SECONDS):
        self.lease_row(lease_id)
        with self.db.tx() as conn:
            conn.execute("UPDATE leases SET expires_at = ? WHERE lease_id = ?", (self.clock() + extra_seconds, lease_id))

    def reap_expired(self):
        with self.db.tx() as conn:
            return conn.execute("DELETE FROM leases WHERE expires_at < ?", (self.clock(),)).rowcount

    # --- results ---

    def _effective_interval(self, conn, requested, duration):
        avg = float(self.db.get_setting("avg_cycle_seconds", duration) or duration)
        avg = 0.7 * avg + 0.3 * duration
        conn.execute("INSERT INTO settings(key, value) VALUES ('avg_cycle_seconds', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(avg),))
        active = conn.execute("SELECT COUNT(*) FROM searches WHERE status IN ('active', 'waiting')").fetchone()[0]
        return max(requested, active * avg / self.workers)

    def complete(self, lease_id, outcome, duration, error_kind=None, message="", selector="", step="",
                 current_appointment=None):
        """outcome: "continue" | "booked" | "error" (with error_kind from rlprenota.core.errors.ErrorKind)."""
        lease = self.lease_row(lease_id)
        search_id, user_id, now = lease["search_id"], lease["user_id"], self.clock()
        row = self.db.query_one("SELECT settings_json, consecutive_failures FROM searches WHERE id = ?", (search_id,))
        if row is None:  # deleted by the user meanwhile
            return
        settings = SearchSettings.from_json(row["settings_json"])
        notify, admin = None, None

        with self.db.tx() as conn:
            conn.execute("DELETE FROM leases WHERE lease_id = ?", (lease_id,))
            if current_appointment is not None:
                conn.execute("UPDATE searches SET current_appointment = ? WHERE id = ?", (current_appointment[:40], search_id))

            if outcome == "continue" or (outcome == "booked" and settings.continua_dopo_prenotazione):
                interval = self._effective_interval(conn, settings.refresh_seconds, duration)
                conn.execute("""UPDATE searches SET status = 'active', consecutive_failures = 0, last_run_at = ?,
                                last_result = ?, next_run_at = ?, effective_interval = ? WHERE id = ?""",
                             (now, (message or "Nessuna nuova disponibilità migliore")[:300], now + interval, interval, search_id))
            elif outcome == "booked":
                pass  # finished below, outside this transaction
            elif error_kind == "login_rejected":
                conn.execute("UPDATE searches SET status = 'error', last_run_at = ?, last_result = ? WHERE id = ?",
                             (now, message[:300], search_id))
                notify = f"⚠️ La ricerca «{settings.label}» è in pausa: {message}\nCorreggi i dati dalla pagina web e riprendila."
            elif error_kind == "throttled":
                conn.execute("UPDATE searches SET last_run_at = ?, last_result = ?, next_run_at = ? WHERE id = ?",
                             (now, "Portale sovraccarico: nuovo tentativo più tardi", now + THROTTLE_PAUSE_SECONDS, search_id))
                conn.execute("INSERT INTO settings(key, value) VALUES ('pause_until', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(now + THROTTLE_PAUSE_SECONDS),))
                admin = ("throttled", f"⏸ Il portale sta limitando le richieste ({message}). Ricerche sospese per 10 minuti.")
            else:
                failures = row["consecutive_failures"] + 1
                if failures >= CIRCUIT_BREAKER_FAILURES:
                    conn.execute("""UPDATE searches SET status = 'error', consecutive_failures = ?, last_run_at = ?,
                                    last_result = ? WHERE id = ?""",
                                 (failures, now, f"Sospesa dopo {failures} errori consecutivi", search_id))
                    notify = (f"⚠️ La ricerca «{settings.label}» è stata sospesa dopo {failures} errori consecutivi. "
                              "L'amministratore è stato avvisato; puoi riprenderla dalla pagina web.")
                    admin = (f"breaker:{search_id}", f"🔴 Ricerca {search_id} sospesa dopo {failures} errori ({error_kind}): {message[:200]}")
                else:
                    wait = min(MAX_BACKOFF_SECONDS, 60 * 2 ** (failures - 1))
                    conn.execute("""UPDATE searches SET consecutive_failures = ?, last_run_at = ?, last_result = ?,
                                    next_run_at = ? WHERE id = ?""",
                                 (failures, now, "Errore temporaneo, nuovo tentativo a breve", now + wait, search_id))
            self.db.audit(f"run_{outcome}", user_id, json.dumps({"search": search_id, "kind": error_kind,
                                                                  "duration": round(duration, 1)}), conn=conn)

        if outcome == "booked" and not settings.continua_dopo_prenotazione:
            self.searches.finish(search_id, "booked", message or "Prenotato")
        if error_kind == "portal_changed":
            self._record_portal_change(search_id, selector, step, message)
        if notify:
            self.alerts.notify_user(user_id, notify)
        if admin:
            self.alerts.alert_admin(*admin)

    def _record_portal_change(self, search_id, selector, step, message):
        now = self.clock()
        self._portal_changes = [(t, s) for t, s in self._portal_changes if now - t <= PORTAL_CHANGE_WINDOW_SECONDS]
        self._portal_changes.append((now, search_id))
        if len({s for _, s in self._portal_changes}) >= PORTAL_CHANGE_SEARCHES and not self.globally_paused():
            reason = f"Il portale sembra cambiato: '{selector}' non trovato ({step})"
            self.pause_global(reason)
            self.alerts.alert_admin("portal_changed",
                                    f"🛑 {reason}.\nTutte le ricerche sono sospese. Dettagli: {message[:300]}\n"
                                    "Verifica il portale e riprendi dalla console di amministrazione.")
