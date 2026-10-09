"""End-to-end: master internal API (HTTP) + bot on a fake Telegram + a worker with its own headless Chromium."""
import json
import os
import socket
import threading
import time
from datetime import date
from unittest import mock

import pytest
import uvicorn

from rlprenota.core import portal
from rlprenota.core.errors import PortalChanged
from rlprenota.master.auth import AuthService
from rlprenota.master.bot import BotGateway
from rlprenota.master.internal_api import create_internal_app
from rlprenota.master.router import ProposalRouter
from rlprenota.master.scheduler import Scheduler
from rlprenota.master.searches import SearchRepo
from rlprenota.master.validation import SearchSecrets, SearchSettings
from rlprenota.signing import sign
from rlprenota.telegram import TelegramBot
from rlprenota.worker.browser import BrowserManager, make_chromium
from rlprenota.worker.client import MasterClient, MasterUnavailable
from rlprenota.worker.worker import Worker
from tests.fakes import FakeTelegram, portal_page

SECRET = os.urandom(32)
SECRETS = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"})
SLOTS = [{"when": "05/11/2026 - 08:30", "azienda": "ASST Niguarda", "comune": "MILANO", "sede": "Ospedale Niguarda"},
         {"when": "06/11/2026 - 09:00", "azienda": "ASST Niguarda", "comune": "MILANO", "sede": "Ospedale Niguarda"}]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def stack(db, crypto, tmp_path):
    fake = FakeTelegram()
    gateway = BotGateway(TelegramBot("TOKEN", "", api_url=fake.url), None, public_url="https://x/p/")
    auth = AuthService(db, crypto, gateway)
    gateway.auth = auth
    searches = SearchRepo(db, crypto)
    router = ProposalRouter(db, auth, searches, gateway)
    gateway.decision_handler = router.handle_answer
    scheduler = Scheduler(db, searches, router, workers=1)
    app = create_internal_app(scheduler, router, searches, SECRET)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    stop = threading.Event()

    def bot_loop():
        while not stop.is_set():
            gateway.poll_once()
            time.sleep(0.05)
    threading.Thread(target=bot_loop, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    owner = auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), 5555, None)
    admin = auth.redeem_invite(auth.create_invite("Fede", login_name="fede", is_admin=True), 9000, None)
    page = tmp_path / "portal.html"
    page.write_text(portal_page(SLOTS), encoding="utf-8")
    yield {"fake": fake, "searches": searches, "scheduler": scheduler, "owner": owner, "admin": admin,
           "url": f"http://127.0.0.1:{port}", "page": page.as_uri(), "db": db}
    stop.set()
    server.should_exit = True
    fake.close()


@pytest.fixture(scope="module")
def browsers():
    manager = BrowserManager(factory=lambda: make_chromium(headless=True))
    yield manager
    manager.kill()


def create(stack, **kw):
    form = {"label": "Visita", "location_mode": "provinces", "province": ["MILANO CITTA'"], "end_date": "2099-12-31",
            "telegram_timeout": "1", **kw}
    return stack["searches"].create(stack["owner"], SearchSettings.from_form(form, today=date(2026, 10, 9)), SECRETS)


def mock_portal(page_uri):
    def open_flow(driver, *args, **kwargs):
        driver.get(page_uri)
        return portal.MODE_NEW, None
    return mock.patch.multiple(portal, open_search_flow=open_flow, search_in_province=mock.Mock(return_value=True),
                               check_search_outcome=mock.Mock(return_value="RESULTS"), cleanup_ui_for_next_search=mock.Mock())


def worker(stack, browsers, secret=SECRET):
    return Worker(MasterClient(stack["url"], secret, "w1"), browsers, name="w1", sleep=lambda s: None)


def test_proposal_accepted_on_telegram_is_booked(stack, browsers):
    search_id = create(stack)
    stack["fake"].answers = ["a"]
    with mock_portal(stack["page"]):
        assert worker(stack, browsers).run_once() is True
    view = stack["searches"].get(stack["owner"], search_id)
    assert view.status == "booked" and "05/11/2026 08:30" in view.last_result
    assert stack["searches"].secrets_for(search_id) is None            # personal data wiped
    texts = [b["text"] for b in stack["fake"].sent_to(5555)]
    assert any("Ospedale Niguarda" in t and "08:30" in t for t in texts)
    assert any("PRENOTATO" in b["text"] for m, b in stack["fake"].requests if m == "editMessageText")


def test_rejection_is_stored_and_next_slot_proposed(stack, browsers):
    search_id = create(stack)
    stack["fake"].answers = ["r", None]
    with mock_portal(stack["page"]), mock.patch("rlprenota.worker.worker.MasterDecider.propose", autospec=True,
                                               side_effect=_short_wait_propose()):
        worker(stack, browsers).run_once()
    discarded = stack["searches"].discarded(stack["owner"], search_id)
    assert [d["slot"]["when"] for d in discarded] == ["05/11/2026 08:30"]
    proposals = [b["text"] for b in stack["fake"].sent_to(5555) if "reply_markup" in b]
    assert len(proposals) == 2 and "06/11/2026" in proposals[1]
    assert stack["searches"].get(stack["owner"], search_id).status == "active"


def _short_wait_propose():
    """MasterDecider.propose with a 3-second answer window instead of minutes."""
    from rlprenota.worker.worker import MasterDecider
    original = MasterDecider.propose

    def propose(self, session, slot):
        self.timeout_minutes = 0.05
        return original(self, session, slot)
    return propose


def test_portal_change_on_two_searches_alerts_admin_and_pauses(stack, browsers):
    create(stack)
    create(stack)
    failing = mock.patch.object(portal, "open_search_flow", side_effect=PortalChanged("#cf", "login", "<p>pagina nuova</p>"))
    with failing:
        w = worker(stack, browsers)
        assert w.run_once() and w.run_once()
    assert stack["scheduler"].globally_paused()
    admin_texts = [b["text"] for b in stack["fake"].sent_to(9000)]
    assert any("#cf" in t for t in admin_texts)
    assert worker(stack, browsers).run_once() is False                  # nothing is leased while paused


def test_watchdog_kills_a_stuck_run(stack, browsers):
    search_id = create(stack)
    with mock_portal(stack["page"]), mock.patch("rlprenota.worker.worker.run_cycle", side_effect=lambda s: time.sleep(4)):
        w = Worker(MasterClient(stack["url"], SECRET, "w1"), browsers, name="w1", sleep=lambda s: None, run_timeout=1)
        w.run_once()
    row = stack["db"].query_one("SELECT consecutive_failures, last_result FROM searches WHERE id = ?", (search_id,))
    assert row["consecutive_failures"] == 1


def test_requests_must_be_signed_and_fresh(stack):
    with pytest.raises(MasterUnavailable):
        MasterClient(stack["url"], os.urandom(32), "intruder").lease()
    old = MasterClient(stack["url"], SECRET, "w1", clock=lambda: time.time() - 120)
    with pytest.raises(MasterUnavailable):
        old.lease()
    # Replaying a captured request is refused
    import urllib.request
    body = json.dumps({"worker": "w1"}).encode()
    ts = f"{time.time():.3f}"
    headers = {"X-RLP-Time": ts, "X-RLP-Signature": sign(SECRET, "POST", "/api/lease", ts, body), "Content-Type": "application/json"}
    first = urllib.request.urlopen(urllib.request.Request(stack["url"] + "/api/lease", data=body, headers=headers))
    assert first.status in (200, 204)
    with pytest.raises(urllib.error.HTTPError) as replay:
        urllib.request.urlopen(urllib.request.Request(stack["url"] + "/api/lease", data=body, headers=headers))
    assert replay.value.code == 401
