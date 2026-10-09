import threading
from datetime import date, datetime

import pytest

from rlprenota.core.models import Slot
from rlprenota.master.bot import BotGateway
from rlprenota.master.router import ProposalRouter
from rlprenota.master.searches import SearchRepo
from rlprenota.master.validation import SearchSecrets, SearchSettings
from rlprenota.telegram import TelegramBot
from tests.fakes import FakeTelegram

SECRETS = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"})
SLOT = Slot(datetime(2026, 11, 5, 8, 30), "ASST Niguarda", "Ospedale Niguarda", "MILANO", "MILANO CITTA'")


@pytest.fixture
def fake():
    server = FakeTelegram()
    yield server
    server.close()


@pytest.fixture
def setup(fake, db, crypto, clock):
    from rlprenota.master.auth import AuthService
    gateway = BotGateway(TelegramBot("TOKEN", "", api_url=fake.url), None, public_url="https://x/p/")
    auth = AuthService(db, crypto, gateway, clock=clock)
    gateway.auth = auth
    searches = SearchRepo(db, crypto, clock=clock)
    router = ProposalRouter(db, auth, searches, gateway, clock=clock)
    gateway.decision_handler = router.handle_answer
    owner = auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), 5555, None)
    form = {"label": "Visita", "location_mode": "provinces", "province": ["MILANO CITTA'"], "end_date": "2099-12-31"}
    search_id = searches.create(owner, SearchSettings.from_form(form, today=date(2026, 10, 9)), SECRETS)
    return router, gateway, searches, search_id, owner


def proposal_message(fake):
    body = fake.sent_to(5555)[-1]
    message_id = next(mid for mid, text, _ in fake.sent if text == body["text"])
    return body, message_id


def test_proposal_goes_to_the_owner_with_details(setup, fake):
    router, _, searches, search_id, owner = setup
    router.propose(search_id, SLOT, current_appointment="", dry_run=True, timeout_minutes=15)
    body, _ = proposal_message(fake)
    for expected in ("05/11/2026", "08:30", "Ospedale Niguarda", "MILANO CITTA'", "DRY RUN", "Prima prenotazione"):
        assert expected in body["text"]
    assert "MRTMTT25D09F205Z" not in body["text"]
    data = [b["callback_data"] for b in body["reply_markup"]["inline_keyboard"][0]]
    assert data == [f"a:{search_id}:{SLOT.slot_id}", f"r:{search_id}:{SLOT.slot_id}"]
    assert searches.get(owner, search_id).status == "waiting"


def test_live_answer_reaches_the_waiting_worker(setup, fake):
    router, gateway, _, search_id, _ = setup
    router.propose(search_id, SLOT, "", False, 15)
    body, message_id = proposal_message(fake)
    result = {}
    waiter = threading.Thread(target=lambda: result.update(d=router.wait_decision(search_id, SLOT.slot_id, timeout=5)))
    waiter.start()
    fake.press(message_id, body["reply_markup"]["inline_keyboard"][0][0]["callback_data"], chat_id=5555)
    gateway.poll_once()
    waiter.join(6)
    assert result["d"] == "accept"


def test_strangers_cannot_answer(setup, fake):
    router, gateway, searches, search_id, owner = setup
    router.propose(search_id, SLOT, "", False, 15)
    body, message_id = proposal_message(fake)
    fake.press(message_id, body["reply_markup"]["inline_keyboard"][0][1]["callback_data"], chat_id=6666)
    gateway.poll_once()
    assert router.wait_decision(search_id, SLOT.slot_id, timeout=0.2) is None
    assert searches.discarded(owner, search_id) == []


def test_no_answer_times_out(setup):
    router, _, _, search_id, _ = setup
    router.propose(search_id, SLOT, "", False, 15)
    assert router.wait_decision(search_id, SLOT.slot_id, timeout=0.2) is None


def test_late_reject_by_reaction_discards(setup, fake):
    router, gateway, searches, search_id, owner = setup
    router.propose(search_id, SLOT, "", False, 15)
    _, message_id = proposal_message(fake)
    router.expire(search_id, SLOT.slot_id)             # the worker gave up waiting
    fake.add_update({"message_reaction": {"chat": {"id": 5555}, "message_id": message_id,
                                          "new_reaction": [{"type": "emoji", "emoji": "👎"}]}})
    gateway.poll_once()
    assert [d["slot_id"] for d in searches.discarded(owner, search_id)] == [SLOT.slot_id]


def test_late_accept_marks_for_booking(setup, fake):
    router, gateway, searches, search_id, _ = setup
    router.propose(search_id, SLOT, "", False, 15)
    body, message_id = proposal_message(fake)
    router.expire(search_id, SLOT.slot_id)
    fake.press(message_id, body["reply_markup"]["inline_keyboard"][0][0]["callback_data"], chat_id=5555)
    gateway.poll_once()
    assert searches.decisions(search_id).is_pre_accepted(SLOT)
    assert any("ritardo" in text for _, text in [(m, b.get("text", "")) for m, b in fake.requests if m == "answerCallbackQuery"])


def test_update_edits_the_proposal(setup, fake):
    router, _, _, search_id, _ = setup
    router.propose(search_id, SLOT, "", False, 15)
    _, message_id = proposal_message(fake)
    router.update(search_id, SLOT.slot_id, "🎉 PRENOTATO CON SUCCESSO!")
    edits = [b for m, b in fake.requests if m == "editMessageText"]
    assert edits[-1]["message_id"] == message_id and "PRENOTATO" in edits[-1]["text"] and "reply_markup" not in edits[-1]


def test_admin_alerts_are_deduplicated(setup, fake, auth, clock):
    router, gateway, *_ = setup
    admin = router.auth.redeem_invite(router.auth.create_invite("Fede", login_name="fede", is_admin=True), 9000, None)
    assert admin
    router.alert_admin("portal_changed", "🛑 portale cambiato")
    router.alert_admin("portal_changed", "🛑 portale cambiato")
    assert len([b for b in fake.sent_to(9000) if "portale" in b["text"]]) == 1
    clock.advance(3601)
    router.alert_admin("portal_changed", "🛑 portale cambiato")
    assert len([b for b in fake.sent_to(9000) if "portale" in b["text"]]) == 2
