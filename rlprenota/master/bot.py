"""The master's single Telegram bot: invites, login approvals, and routing of proposal answers.

Only one process may poll a bot token, so every user's answers arrive here and are routed to the
right search (see scheduler/worker for proposals).
"""
import logging

from rlprenota.telegram import TelegramError, parse_update

log = logging.getLogger(__name__)

PRIVATE_BOT = ("Questo bot è privato. Se hai ricevuto un invito, apri il link che ti è stato inviato.")


class BotGateway:
    def __init__(self, bot, auth, public_url, decision_handler=None):
        self.bot = bot
        self.auth = auth
        self.public_url = public_url
        # decision_handler(answer, ack): answers to slot proposals (set by the scheduler)
        self.decision_handler = decision_handler

    # --- outgoing (AuthService notifier interface) ---

    def send(self, chat_id, text, buttons=None):
        return self.bot.send(text, buttons, chat_id=chat_id)

    def edit(self, chat_id, message_id, text, buttons=None):
        self.bot.edit(message_id, text, buttons, chat_id=chat_id)

    def send_login_request(self, chat_id, request_id, code, device):
        text = (f"🔐 Richiesta di accesso a RL Prenota Online da: {device or 'dispositivo sconosciuto'}.\n\n"
                f"Sei tu? Se non hai chiesto tu l'accesso, premi No.\n\nCodice alternativo: {code} (valido 5 minuti)")
        buttons = [[{"text": "✅ Sì, sono io", "callback_data": f"l:{request_id}:y"},
                    {"text": "❌ No", "callback_data": f"l:{request_id}:n"}]]
        try:
            self.send(chat_id, text, buttons)
        except TelegramError as e:
            log.warning(f"Impossibile inviare la richiesta di accesso: {e}")

    # --- incoming ---

    def poll_once(self, timeout=0):
        """Process pending updates. Returns False if Telegram could not be reached."""
        try:
            updates = self.bot.get_updates(timeout)
        except TelegramError as e:
            log.warning(f"Telegram non raggiungibile: {e}")
            return False
        for update in updates:
            try:
                self.handle(update)
            except Exception:
                # One malformed update must never stop the bot (details may contain personal data: not logged)
                log.exception("Errore nella gestione di un aggiornamento Telegram")
        return True

    def handle(self, update):
        parsed = parse_update(update)
        if not parsed or parsed.get("chat_id") is None:
            return
        kind = parsed["kind"]
        if kind == "text":
            self._handle_text(parsed)
        elif kind == "callback" and parsed["data"].startswith("l:"):
            self._handle_login_button(parsed)
        elif kind == "decision":
            if self.decision_handler:
                self.decision_handler(parsed, lambda text: self.bot.answer_callback(parsed.get("callback_id"), text))
            else:
                self.bot.answer_callback(parsed.get("callback_id"), "Servizio non disponibile, riprova tra poco")
        else:
            self.bot.answer_callback(parsed.get("callback_id"), "Pulsante non più valido")

    def _handle_text(self, parsed):
        chat_id, text = parsed["chat_id"], parsed["text"]
        if text.startswith("/start "):
            token = text.split(" ", 1)[1].strip()
            try:
                user_id = self.auth.redeem_invite(token, chat_id, parsed.get("username"))
            except Exception:
                self.send(chat_id, "Questo invito non è valido o è scaduto. Chiedi un nuovo invito.")
                return
            login = self.auth.login_name(user_id)
            self.send(chat_id, f"✅ Account collegato! Per accedere apri {self.public_url} e usa il nome di accesso: {login}\n\n"
                               "Riceverai qui le richieste di accesso e le disponibilità trovate.")
            return
        if self.auth.user_by_chat(chat_id) is None:
            self.send(chat_id, PRIVATE_BOT)
        else:
            self.send(chat_id, f"Per gestire le tue ricerche apri {self.public_url}")

    def _handle_login_button(self, parsed):
        _, request_id, choice = (parsed["data"].split(":") + ["", ""])[:3]
        chat_id = parsed["chat_id"]
        approved = choice == "y"
        ok = self.auth.approve(request_id, chat_id) if approved else self.auth.deny(request_id, chat_id)
        if not ok:
            self.bot.answer_callback(parsed.get("callback_id"), "Richiesta scaduta o non valida")
            return
        self.bot.answer_callback(parsed.get("callback_id"), "Fatto")
        text = "✅ Accesso approvato." if approved else "❌ Accesso negato. Se non eri tu, nessun problema: non è entrato nessuno."
        try:
            self.edit(chat_id, parsed["message_id"], text)
        except TelegramError:
            pass
