import os

import pytest
from selenium import webdriver


@pytest.fixture(scope="session")
def chrome():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    yield driver
    driver.quit()


@pytest.fixture
def load_html(chrome, tmp_path):
    def load(html):
        page = tmp_path / "page.html"
        page.write_text(html, encoding="utf-8")
        chrome.get(page.as_uri())
        return chrome
    return load


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    from rlprenota.master.db import Database
    return Database(str(tmp_path / "master.db"))


@pytest.fixture
def crypto():
    from rlprenota.master.crypto import Crypto
    return Crypto({1: os.urandom(32)}, current=1)
