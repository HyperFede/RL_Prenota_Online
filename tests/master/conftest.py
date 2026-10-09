import os

import pytest

from rlprenota.master.auth import AuthService
from rlprenota.master.crypto import Crypto
from rlprenota.master.db import Database


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeNotifier:
    def __init__(self):
        self.login_requests = []   # (chat_id, request_id, code, device)
        self.messages = []         # (chat_id, text)

    def send_login_request(self, chat_id, request_id, code, device):
        self.login_requests.append((chat_id, request_id, code, device))

    def send(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / "master.db"))


@pytest.fixture
def crypto():
    return Crypto({1: os.urandom(32)}, current=1)


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def auth(db, crypto, notifier, clock):
    return AuthService(db, crypto, notifier, clock=clock)


@pytest.fixture
def active_user(auth):
    """An invited user who linked Telegram: returns (user_id, chat_id, username)."""
    token = auth.create_invite("Mario", is_admin=False)
    user_id = auth.redeem_invite(token, chat_id=1111, username="Mario_R")
    return user_id, 1111, "Mario_R"
