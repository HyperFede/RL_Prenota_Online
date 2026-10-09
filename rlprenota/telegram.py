import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlsplit

from rlprenota.core.decisions import ACCEPT, REJECT

ACCEPT_REACTIONS = {"👍", "👌", "🔥", "❤", "❤️"}
REJECT_REACTIONS = {"👎", "💩", "🤮"}
ACCEPT_WORDS = {"si", "sì", "ok", "accetto", "accetta", "prenota", "yes", "y", "s"}
REJECT_WORDS = {"no", "n", "scarta", "rifiuto", "rifiuta"}


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class TelegramError(Exception):
    pass


class TelegramBot:
    """Minimal Telegram Bot API client (no external dependencies).

    Proposals are sent with two inline buttons (Prenota / Scarta). The answer is also accepted as a
    👍 / 👎 reaction or as a text reply ("si" / "no") to the proposal message.
    """

    def __init__(self, token, chat_id, api_url="https://api.telegram.org"):
        parts = urlsplit(api_url)
        # Only HTTPS, except a local fake server in tests
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost"))):
            raise ValueError("L'API di Telegram deve usare HTTPS")
        self.api_url = api_url.rstrip("/")
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.offset = None
        self._context = _ssl_context()

    @property
    def enabled(self):
        return bool(self.token and self.chat_id)

    def _call(self, method, params=None, timeout=35):
        url = f"{self.api_url}/bot{self.token}/{method}"
        data = json.dumps(params or {}).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            # The URL scheme is validated in __init__ (HTTPS only)
            with urllib.request.urlopen(request, timeout=timeout, context=self._context) as response:  # nosec B310
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode("utf-8"))
            except ValueError:
                raise TelegramError(f"HTTP {e.code}") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                # Never fall back to an unverified connection: the bot token and health data travel on it
                raise TelegramError("certificato TLS di api.telegram.org non verificabile: installa 'certifi' "
                                    "o i certificati di Python (oppure la rete sta intercettando il traffico)") from None
            raise TelegramError(str(e.reason)) from None
        if not payload.get("ok"):
            raise TelegramError(payload.get("description", "risposta non valida"))
        return payload["result"]

    # --- sending ---

    def send(self, text, buttons=None):
        params = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", params)["message_id"]

    def edit(self, message_id, text, buttons=None):
        params = {"chat_id": self.chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
        # Without reply_markup Telegram removes the buttons
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self._call("editMessageText", params)
        except TelegramError as e:
            if "message is not modified" not in str(e):
                raise

    def notify(self, text):
        """Fire-and-forget message: a Telegram problem must never stop the search."""
        if not self.enabled:
            return None
        try:
            return self.send(text)
        except TelegramError as e:
            print(f"Errore invio messaggio Telegram: {e}")
            return None

    @staticmethod
    def decision_buttons(slot_id):
        return [[{"text": "✅ Prenota", "callback_data": f"a:{slot_id}"},
                 {"text": "❌ Scarta", "callback_data": f"r:{slot_id}"}]]

    # --- receiving ---

    def _is_our_chat(self, chat):
        return str((chat or {}).get("id")) == self.chat_id

    def poll(self, timeout=0):
        """Return the decisions received since the last call.

        Each decision is a dict: {"decision": ACCEPT|REJECT, "slot_id": ...} for button presses,
        or {"decision": ..., "message_id": ...} for reactions / text replies (the caller maps the message to a slot).
        """
        params = {"timeout": int(timeout), "allowed_updates": ["message", "callback_query", "message_reaction"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = self._call("getUpdates", params, timeout=timeout + 15)

        decisions = []
        for update in updates:
            self.offset = update["update_id"] + 1
            decision = self._parse_update(update)
            if decision:
                decisions.append(decision)
        return decisions

    def _parse_update(self, update):
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message") or {}
            if not self._is_our_chat(message.get("chat")):
                return None
            data = query.get("data") or ""
            kind, _, slot_id = data.partition(":")
            decision = {"a": ACCEPT, "r": REJECT}.get(kind)
            if not decision or not slot_id:
                return None
            return {"decision": decision, "slot_id": slot_id, "message_id": message.get("message_id"),
                    "callback_id": query.get("id")}

        if "message_reaction" in update:
            reaction = update["message_reaction"]
            if not self._is_our_chat(reaction.get("chat")):
                return None
            emojis = {r.get("emoji") for r in reaction.get("new_reaction", []) if r.get("type") == "emoji"}
            if emojis & REJECT_REACTIONS:
                decision = REJECT
            elif emojis & ACCEPT_REACTIONS:
                decision = ACCEPT
            else:
                return None
            return {"decision": decision, "message_id": reaction.get("message_id")}

        if "message" in update:
            message = update["message"]
            reply_to = message.get("reply_to_message")
            if not reply_to or not self._is_our_chat(message.get("chat")):
                return None
            word = (message.get("text") or "").strip().lower().strip("!.")
            decision = ACCEPT if word in ACCEPT_WORDS else REJECT if word in REJECT_WORDS else None
            if not decision:
                return None
            return {"decision": decision, "message_id": reply_to.get("message_id")}
        return None

    def answer_callback(self, callback_id, text):
        if not callback_id:
            return
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass  # only cosmetic (stops the loading spinner on the button)


if __name__ == "__main__":
    # Self-test: python telegram_bot.py <TOKEN> <CHAT_ID>
    # (without arguments it reads telegram_bot_token / telegram_chat_id from data_file.py)
    if len(sys.argv) == 3:
        bot = TelegramBot(sys.argv[1], sys.argv[2])
    else:
        import data_file
        bot = TelegramBot(getattr(data_file, "telegram_bot_token", ""), getattr(data_file, "telegram_chat_id", ""))
    if not bot.enabled:
        sys.exit("Token o Chat ID mancanti.")
    bot.poll()  # skip old updates
    message_id = bot.send("🧪 Test RL Prenota Online: premi un pulsante, metti 👍/👎 o rispondi 'si'/'no' a questo messaggio.",
                          TelegramBot.decision_buttons("test"))
    print("Messaggio inviato, in attesa di risposta (2 minuti)...")
    deadline = time.time() + 120
    while time.time() < deadline:
        for d in bot.poll(timeout=20):
            if d.get("slot_id") == "test" or d.get("message_id") == message_id:
                bot.answer_callback(d.get("callback_id"), "Ricevuto!")
                bot.edit(message_id, f"🧪 Test completato: risposta ricevuta = {d['decision']}")
                print("Risposta ricevuta:", d["decision"])
                sys.exit(0)
    print("Nessuna risposta ricevuta.")
