"""How the user is asked about a slot: Telegram, the terminal, or (on the NAS) the master service.

A decider answers ACCEPT, REJECT or None (no answer in time) and keeps the user informed.
"""
import logging
import time

from rlprenota.core.decisions import ACCEPT, ACCEPTED, BOOKED, REJECT
from rlprenota.core.models import Slot
from rlprenota.telegram import TelegramBot, TelegramError

log = logging.getLogger(__name__)


def proposal_text(session, slot, footer):
    header = "🏥 DISPONIBILITÀ TROVATA"
    if session.current_appointment is not None:
        context = f"🔁 Appuntamento attuale: {session.current_appointment.date}"
    else:
        context = "🆕 Prima prenotazione (la ricetta non ha ancora un appuntamento)"
    dry_run = "\n🧪 DRY RUN attivo: anche se accetti, NON verrà prenotato." if session.prefs.dry_run else ""
    return f"{header}\n\n{slot.describe()}\n\n{context}{dry_run}\n\n{footer}"


class Decider:
    def propose(self, session, slot):
        """Ask about `slot` (blocking). Returns ACCEPT, REJECT or None."""
        raise NotImplementedError

    def update(self, session, slot_id, footer, keep_buttons=False):
        """Tell the user what happened to a proposal."""

    def notify(self, text):
        """Free-form message to the user."""

    def idle(self, session, seconds):
        """Wait between cycles, still recording late answers when possible."""
        time.sleep(seconds)


class TerminalDecider(Decider):
    def __init__(self, ask=input):
        self.ask = ask

    def propose(self, session, slot):
        session.store.record_proposal(slot)
        answer = self.ask("Prenotare questo appuntamento? [S] sì / [N] scarta per sempre / [Invio] salta per ora: ").strip().upper()
        if answer in ("S", "SI", "SÌ", "Y", "YES"):
            return ACCEPT
        if answer in ("N", "NO"):
            return REJECT
        return None

    def notify(self, text):
        log.info(text)


class TelegramDecider(Decider):
    """Proposals with Prenota/Scarta buttons; falls back to the terminal if Telegram is unreachable."""

    def __init__(self, bot, timeout_minutes=15, fallback=None):
        self.bot = bot
        self.timeout_minutes = timeout_minutes
        self.fallback = fallback or TerminalDecider()

    def propose(self, session, slot):
        footer = (f"Vuoi prenotarlo? Premi un pulsante, metti 👍/👎 o rispondi 'si'/'no' "
                  f"(attendo {self.timeout_minutes} minuti).")
        try:
            message_id = self.bot.send(proposal_text(session, slot, footer), TelegramBot.decision_buttons(slot.slot_id))
        except TelegramError as e:
            log.info(f"Impossibile inviare la proposta su Telegram ({e}): chiedo sul terminale.")
            return self.fallback.propose(session, slot)

        session.store.record_proposal(slot, message_id)
        log.info(f"-> Proposta inviata su Telegram, attendo la risposta per {self.timeout_minutes} minuti...")
        deadline = time.time() + self.timeout_minutes * 60
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            decision = self.process_answers(session, timeout=max(1, min(25, remaining)), waiting_for=slot.slot_id)
            if decision:
                return decision
        return None

    def update(self, session, slot_id, footer, keep_buttons=False):
        proposal = session.store.proposals.get(slot_id)
        if not proposal or not proposal.get("message_id"):
            return
        slot = Slot.from_dict(proposal["slot"])
        buttons = TelegramBot.decision_buttons(slot_id) if keep_buttons else None
        try:
            self.bot.edit(proposal["message_id"], proposal_text(session, slot, footer), buttons)
        except TelegramError as e:
            log.debug(f"-> Impossibile aggiornare il messaggio Telegram: {e}")

    def notify(self, text):
        self.bot.notify(text)

    def idle(self, session, seconds):
        # Keep listening to Telegram, so late answers are recorded while waiting
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.process_answers(session, timeout=max(1, min(25, int(deadline - time.time()))))

    # --- answers ---

    def process_answers(self, session, timeout=0, waiting_for=None):
        """Poll Telegram once. Returns the decision for `waiting_for` if it arrived, otherwise None."""
        try:
            answers = self.bot.poll(timeout=timeout)
        except TelegramError as e:
            log.info(f"Errore di comunicazione con Telegram: {e}")
            time.sleep(5)
            return None
        decision = None
        for answer in answers:
            if waiting_for and self.apply_answer(session, answer, waiting_for) == waiting_for:
                decision = answer["decision"]
            elif not waiting_for:
                self.apply_answer(session, answer)
        return decision

    def apply_answer(self, session, answer, waiting_for=None):
        """Record an answer coming from Telegram. Returns the slot_id it refers to (or None)."""
        store, bot = session.store, self.bot
        slot_id = answer.get("slot_id") or store.slot_id_for_message(answer.get("message_id"))
        if not slot_id or slot_id not in store.proposals:
            bot.answer_callback(answer.get("callback_id"), "Proposta non riconosciuta")
            return None
        if slot_id == waiting_for:
            bot.answer_callback(answer.get("callback_id"), "Ricevuto!")
            return slot_id
        apply_late_answer(session, slot_id, answer["decision"], self,
                          lambda text: bot.answer_callback(answer.get("callback_id"), text))
        return slot_id


def apply_late_answer(session, slot_id, decision, decider, acknowledge=lambda text: None):
    """An answer to an older proposal (expired, or from a previous run)."""
    store = session.store
    status = store.proposals[slot_id].get("status")
    if status == BOOKED:
        acknowledge("Questo appuntamento è già stato prenotato")
    elif decision == REJECT:
        store.discard_id(slot_id)
        acknowledge("Scartato")
        decider.update(session, slot_id, "❌ Scartato: non ti verrà più proposto.")
        log.info(f"-> Proposta {slot_id} scartata in ritardo, non verrà più proposta.")
    else:
        # The user wants it after all: book it as soon as it shows up again (if still better than the current one)
        store.undiscard(slot_id)
        store.set_status(slot_id, ACCEPTED)
        acknowledge("Ok! Lo prenoterò appena lo ritrovo disponibile")
        decider.update(session, slot_id, "✅ Accettato in ritardo: verrà prenotato appena lo ritrovo disponibile.")
        log.info(f"-> Proposta {slot_id} accettata in ritardo, verrà prenotata appena ritrovata.")
