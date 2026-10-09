"""Test doubles: a fake Telegram Bot API server and a mock of the portal's results page."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

CHAT_ID = "4242"


class FakeTelegram:
    """Speaks just enough of the Bot API. `answers` is a list of decisions ("a", "r" or None) used, in order,
    to answer each proposal (message with buttons) by pressing the corresponding button."""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.sent = []       # (message_id, text, has_buttons)
        self.requests = []   # (method, body) of every call, for multi-chat assertions
        self.edits = []      # (message_id, text, has_buttons)
        self.callbacks_answered = []
        self.pending_updates = []
        self._next_message_id = 100
        self._next_update_id = 1
        self._lock = threading.Lock()
        self._closed = False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                method = self.path.rsplit("/", 1)[-1]
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                result = fake.handle(method, body)
                payload = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        if not self._closed:
            self._closed = True
            self.server.shutdown()
            self.server.server_close()

    def add_update(self, update):
        with self._lock:
            update["update_id"] = self._next_update_id
            self._next_update_id += 1
            self.pending_updates.append(update)

    def press(self, message_id, data, chat_id=CHAT_ID):
        self.add_update({"callback_query": {"id": f"cb{message_id}", "data": data,
                                            "message": {"message_id": message_id, "chat": {"id": int(chat_id)}}}})

    def user_message(self, chat_id, text, username=None, reply_to=None):
        message = {"message_id": 1, "chat": {"id": int(chat_id)}, "from": {"id": int(chat_id)}, "text": text}
        if username:
            message["from"]["username"] = username
        if reply_to:
            message["reply_to_message"] = {"message_id": reply_to}
        self.add_update({"message": message})

    def sent_to(self, chat_id):
        return [body for method, body in self.requests if method == "sendMessage" and str(body["chat_id"]) == str(chat_id)]

    def handle(self, method, body):
        self.requests.append((method, body))
        if method == "sendMessage":
            with self._lock:
                message_id = self._next_message_id
                self._next_message_id += 1
            buttons = body.get("reply_markup", {}).get("inline_keyboard")
            self.sent.append((message_id, body["text"], bool(buttons)))
            if buttons and self.answers:
                answer = self.answers.pop(0)
                if answer:
                    slot_id = buttons[0][0]["callback_data"].split(":", 1)[1]
                    self.press(message_id, f"{answer}:{slot_id}")
            return {"message_id": message_id}
        if method == "editMessageText":
            self.edits.append((body["message_id"], body["text"], "reply_markup" in body))
            return True
        if method == "answerCallbackQuery":
            self.callbacks_answered.append(body.get("text"))
            return True
        if method == "getUpdates":
            offset = body.get("offset") or 0
            with self._lock:
                self.pending_updates = [u for u in self.pending_updates if u["update_id"] >= offset]
                updates = list(self.pending_updates)
            if not updates:
                time.sleep(0.1)  # a tiny stand-in for long polling
            return updates
        raise ValueError(method)


# Same markup structure as the portal's templates (disponibilita.tmpl.html / verifica-prenotazione.tmpl.html)
PORTAL_PAGE = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="results"></div>
<ul class="pagination"><li class="pagination-next"><a href="#" onclick="nextPage(); return false;">&gt;</a></li></ul>
<div id="modal" style="display:none"><div class="modal-dialog">
  <div class="modal-header"><span class="modal-title">Conferma appuntamento</span></div>
  <div class="dati-appuntamento-summary"><div class="row">
    <div class="appuntamento-field-title"><span>Data e ora</span></div>
    <div class="appuntamento-field-value"><span id="modal-date"></span></div></div></div>
  <div class="note-prepazione-descrizione"><p>Portare la ricetta.</p></div>
  <label><input id="presaVisioneNote" type="checkbox" onchange="document.getElementById('conf').disabled = !this.checked"></label>
  <div class="modal-footer">
    <button class="btn btn-default" ng-click="verificaPrenotazioneCtrl.annulla()" onclick="closeModal()">Chiudi</button>
    <button id="conf" class="btn btn-primary" ng-click="verificaPrenotazioneCtrl.conferma()" disabled onclick="confirmBooking()">Conferma</button>
  </div></div></div>
<h4 id="done" style="display:none">Appuntamento prenotato</h4>
<script>
var SLOTS = __SLOTS__;
var PER_PAGE = 5, page = 0, opened = null;
window.booked = null; window.summariesOpened = [];
function field(title, value) {
  return '<div class="row"><div class="appuntamento-field-title"><span>' + title + '</span></div>' +
         '<div class="appuntamento-field-value"><span>' + value + '</span></div></div>';
}
function render() {
  var html = '<ul class="lista-appuntamenti">';
  SLOTS.slice(page * PER_PAGE, (page + 1) * PER_PAGE).forEach(function (s, i) {
    html += '<li class="appuntamento"><div class="row"><div>' + field('Data e ora', s.when) + field('Azienda', s.azienda) +
            field('Comune', s.comune) + field('Presentarsi in', s.sede) + '</div>' +
            '<div class="ui-disponibilita-action-buttons"><button id="verifica_conferma_appuntamenti" onclick="openModal(' +
            (page * PER_PAGE + i) + ')">Verifica e conferma</button></div></div></li>';
  });
  document.getElementById('results').innerHTML = html + '</ul>';
  var next = document.querySelector('li.pagination-next');
  next.className = (page + 1) * PER_PAGE >= SLOTS.length ? 'pagination-next disabled' : 'pagination-next';
}
function nextPage() { page++; render(); }
function openModal(i) {
  opened = i; window.summariesOpened.push(i);
  document.getElementById('modal-date').textContent = SLOTS[i].when;
  document.getElementById('modal').style.display = 'block';
}
function closeModal() { document.getElementById('modal').style.display = 'none'; opened = null; }
function confirmBooking() {
  window.booked = opened; closeModal();
  document.getElementById('results').innerHTML = '';
  document.getElementById('done').style.display = 'block';
}
render();
</script></body></html>"""


def portal_page(slots):
    return PORTAL_PAGE.replace("__SLOTS__", json.dumps(slots))
