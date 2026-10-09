import pytest

from rlprenota.master.bot import BotGateway
from rlprenota.telegram import TelegramBot
from tests.fakes import FakeTelegram

PUBLIC_URL = "https://rl-prenota.example.ts.net/abc123/"


@pytest.fixture
def fake():
    server = FakeTelegram()
    yield server
    server.close()


@pytest.fixture
def gateway(fake, db, crypto, clock):
    from rlprenota.master.auth import AuthService
    bot = TelegramBot("TOKEN", "", api_url=fake.url)
    gw = BotGateway(bot, None, public_url=PUBLIC_URL)
    gw.auth = AuthService(db, crypto, gw, clock=clock)   # the gateway is the auth notifier
    return gw


def test_start_with_invite_links_the_account(gateway, fake):
    token = gateway.auth.create_invite("Mario Rossi", login_name="mario")
    fake.user_message(5555, f"/start {token}", username="MarioR")
    gateway.poll_once()
    assert gateway.auth.user_by_chat(5555) is not None
    reply = fake.sent_to(5555)[-1]["text"]
    assert "mario" in reply and PUBLIC_URL in reply


def test_bad_invite_or_strangers_get_a_polite_refusal(gateway, fake):
    fake.user_message(6666, "/start forged-token")
    fake.user_message(7777, "ciao")
    gateway.poll_once()
    assert gateway.auth.user_by_chat(6666) is None
    assert "non è valido" in fake.sent_to(6666)[-1]["text"]
    assert "privato" in fake.sent_to(7777)[-1]["text"]


def test_login_approval_flow(gateway, fake):
    token = gateway.auth.create_invite("Mario", login_name="mario")
    fake.user_message(5555, f"/start {token}")
    gateway.poll_once()
    browser = gateway.auth.start_login("mario", "Chrome su Mac", client="1.1.1.1")
    request = fake.sent_to(5555)[-1]
    assert "Chrome su Mac" in request["text"]
    buttons = request["reply_markup"]["inline_keyboard"][0]
    approve = next(b["callback_data"] for b in buttons if b["callback_data"].endswith(":y"))
    message_id = next(mid for mid, text, _ in fake.sent if "Chrome su Mac" in text)

    fake.press(message_id, approve, chat_id=9999)   # someone else can't approve
    gateway.poll_once()
    assert gateway.auth.login_status(browser) == "pending"

    fake.press(message_id, approve, chat_id=5555)
    gateway.poll_once()
    assert gateway.auth.login_status(browser) == "approved"
    assert any(method == "editMessageText" and "approvato" in body["text"] for method, body in fake.requests)


def test_login_denied_from_telegram(gateway, fake):
    token = gateway.auth.create_invite("Mario", login_name="mario")
    fake.user_message(5555, f"/start {token}")
    gateway.poll_once()
    browser = gateway.auth.start_login("mario", "Chrome", client="1.1.1.1")
    request = fake.sent_to(5555)[-1]
    deny = next(b["callback_data"] for b in request["reply_markup"]["inline_keyboard"][0] if b["callback_data"].endswith(":n"))
    message_id = next(mid for mid, text, _ in fake.sent if "Chrome" in text)
    fake.press(message_id, deny, chat_id=5555)
    gateway.poll_once()
    assert gateway.auth.login_status(browser) == "denied"


def test_decisions_are_forwarded_to_the_handler(gateway, fake):
    received = []
    gateway.decision_handler = lambda answer, ack: received.append(answer) or ack("ok")
    fake.press(10, "a:search1:slot1", chat_id=5555)
    gateway.poll_once()
    assert received[0]["decision"] == "accept" and received[0]["data"] == "a:search1:slot1"
    assert any(method == "answerCallbackQuery" for method, _ in fake.requests)


def test_telegram_timeout_is_a_telegram_error(fake):
    import socket
    from rlprenota.telegram import TelegramError
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)   # accepts the connection but never answers
    bot = TelegramBot("TOKEN", "1", api_url=f"http://127.0.0.1:{silent.getsockname()[1]}")
    with pytest.raises(TelegramError):
        bot._call("getUpdates", {}, timeout=1)
    silent.close()


def test_telegram_outage_does_not_crash_the_loop(gateway, fake):
    fake.close()
    assert gateway.poll_once() is False   # error swallowed, reported as False
