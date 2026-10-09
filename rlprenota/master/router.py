"""Routes slot proposals between workers and their owners on Telegram.

A worker asks (propose), then waits (wait_decision). The owner's answer arrives through the single bot
(BotGateway.decision_handler -> handle_answer) and is delivered to the waiting worker. Answers that come
after the worker stopped waiting are applied to the database directly (late reject = discard, late
accept = book when found again). Only the owner's Telegram chat can answer for a search.
"""
import json
import logging
import threading
import time
from types import SimpleNamespace

from rlprenota.core.deciders import proposal_text
from rlprenota.core.decisions import ACCEPTED, BOOKED, REJECT
from rlprenota.core.models import Slot
from rlprenota.master.validation import SearchSettings
from rlprenota.telegram import TelegramError

log = logging.getLogger(__name__)
ADMIN_ALERT_DEDUP_SECONDS = 3600


class ProposalRouter:
    def __init__(self, db, auth, searches, gateway, clock=time.time):
        self.db = db
        self.auth = auth
        self.searches = searches
        self.gateway = gateway
        self.clock = clock
        self._cond = threading.Condition()
        self._waiting = {}        # (search_id, slot_id) -> None until answered
        self._answers = {}
        self._last_alert = {}

    # --- helpers ---

    def _owner(self, search_id):
        row = self.db.query_one("SELECT user_id, settings_json, current_appointment FROM searches WHERE id = ?", (search_id,))
        return row

    def _text(self, search_id, slot, footer, current_appointment=None, dry_run=None):
        row = self._owner(search_id)
        settings = SearchSettings.from_json(row["settings_json"])
        current = row["current_appointment"] if current_appointment is None else current_appointment
        context = SimpleNamespace(current_appointment=SimpleNamespace(date=current) if current else None,
                                  prefs=SimpleNamespace(dry_run=settings.dry_run if dry_run is None else dry_run))
        return f"🔎 {settings.label}\n" + proposal_text(context, slot, footer)

    @staticmethod
    def _buttons(search_id, slot_id):
        return [[{"text": "✅ Prenota", "callback_data": f"a:{search_id}:{slot_id}"},
                 {"text": "❌ Scarta", "callback_data": f"r:{search_id}:{slot_id}"}]]

    # --- worker side ---

    def propose(self, search_id, slot, current_appointment, dry_run, timeout_minutes):
        """Send the proposal to the owner. Returns the Telegram message id (None if Telegram failed)."""
        owner = self._owner(search_id)
        chat_id = self.auth.chat_id(owner["user_id"])
        footer = (f"Vuoi prenotarlo? Premi un pulsante, metti 👍/👎 o rispondi 'si'/'no' "
                  f"(attendo {timeout_minutes} minuti).")
        with self._cond:
            self._waiting[(search_id, slot.slot_id)] = True
            self._answers.pop((search_id, slot.slot_id), None)
        try:
            message_id = self.gateway.send(chat_id, self._text(search_id, slot, footer, current_appointment, dry_run),
                                           self._buttons(search_id, slot.slot_id))
        except TelegramError as e:
            log.warning(f"Proposta non inviata: {e}")
            message_id = None
        self.searches.decisions(search_id).record_proposal(slot, message_id)
        with self.db.tx() as conn:
            conn.execute("UPDATE searches SET status = 'waiting' WHERE id = ? AND status = 'active'", (search_id,))
        return message_id

    def wait_decision(self, search_id, slot_id, timeout):
        """Block until the owner answers or `timeout` seconds pass. Returns ACCEPT, REJECT or None."""
        key = (search_id, slot_id)
        deadline = time.monotonic() + timeout
        with self._cond:
            while key not in self._answers:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or key not in self._waiting:
                    return None
                self._cond.wait(remaining)
            self._waiting.pop(key, None)
            return self._answers.pop(key)

    def expire(self, search_id, slot_id):
        """The worker stopped waiting: later answers are handled as late answers."""
        with self._cond:
            self._waiting.pop((search_id, slot_id), None)
            self._cond.notify_all()
        with self.db.tx() as conn:
            conn.execute("UPDATE searches SET status = 'active' WHERE id = ? AND status = 'waiting'", (search_id,))

    def update(self, search_id, slot_id, footer, keep_buttons=False):
        row = self.db.query_one("SELECT slot_json, message_id FROM proposals WHERE search_id = ? AND slot_id = ?",
                                (search_id, slot_id))
        owner = self._owner(search_id)
        if not row or not row["message_id"] or not owner:
            return
        slot = Slot.from_dict(json.loads(row["slot_json"]))
        try:
            self.gateway.edit(self.auth.chat_id(owner["user_id"]), row["message_id"], self._text(search_id, slot, footer),
                              self._buttons(search_id, slot_id) if keep_buttons else None)
        except TelegramError as e:
            log.debug(f"Messaggio non aggiornato: {e}")

    # --- Telegram side ---

    def _resolve(self, answer):
        data = answer.get("data") or ""
        parts = data.split(":")
        if len(parts) == 3:
            return parts[1], parts[2]
        if answer.get("message_id") is not None:
            row = self.db.query_one("SELECT search_id, slot_id FROM proposals WHERE message_id = ?", (answer["message_id"],))
            if row:
                return row["search_id"], row["slot_id"]
        return None, None

    def handle_answer(self, answer, ack):
        search_id, slot_id = self._resolve(answer)
        owner = self._owner(search_id) if search_id else None
        chat_user = self.auth.user_by_chat(answer.get("chat_id"))
        if not owner or chat_user is None or chat_user != owner["user_id"]:
            ack("Proposta non riconosciuta")
            return
        key = (search_id, slot_id)
        with self._cond:
            if key in self._waiting:
                self._answers[key] = answer["decision"]
                self._cond.notify_all()
                ack("Ricevuto!")
                return
        self._late_answer(search_id, slot_id, answer["decision"], ack)

    def _late_answer(self, search_id, slot_id, decision, ack):
        store = self.searches.decisions(search_id)
        proposal = store.proposals.get(slot_id)
        if not proposal:
            ack("Proposta non più valida")
            return
        if proposal.get("status") == BOOKED:
            ack("Questo appuntamento è già stato prenotato")
        elif decision == REJECT:
            store.discard_id(slot_id)
            ack("Scartato")
            self.update(search_id, slot_id, "❌ Scartato: non ti verrà più proposto.")
        else:
            store.undiscard(slot_id)
            store.set_status(slot_id, ACCEPTED)
            ack("Accettato in ritardo: lo prenoterò appena lo ritrovo disponibile")
            self.update(search_id, slot_id, "✅ Accettato in ritardo: verrà prenotato appena lo ritrovo disponibile.")

    # --- notifications (alerts interface used by the scheduler) ---

    def notify_user(self, user_id, text):
        chat_id = self.auth.chat_id(user_id)
        if chat_id is None:
            return
        try:
            self.gateway.send(chat_id, text)
        except TelegramError as e:
            log.warning(f"Notifica non inviata: {e}")

    def alert_admin(self, key, text):
        now = self.clock()
        if now - self._last_alert.get(key, -1e18) < ADMIN_ALERT_DEDUP_SECONDS:
            return
        self._last_alert[key] = now
        log.warning(f"ALERT {key}: {text}")
        for row in self.db.query("SELECT id FROM users WHERE is_admin = 1 AND status = 'active'"):
            self.notify_user(row["id"], f"[RL Prenota · admin] {text}")
