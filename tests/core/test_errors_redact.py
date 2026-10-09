import logging

from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException

from rlprenota.core.errors import (ErrorKind, LoginRejected, PortalChanged, PortalThrottled, classify)
from rlprenota.core.redact import RedactingFilter, redact


def test_classify_explicit_errors():
    assert classify(PortalChanged("select#provincia", "search form")) is ErrorKind.PORTAL_CHANGED
    assert classify(LoginRejected("Ricetta non trovata")) is ErrorKind.LOGIN_REJECTED
    assert classify(PortalThrottled("HTTP 429")) is ErrorKind.THROTTLED


def test_classify_selenium_errors():
    assert classify(TimeoutException("slow")) is ErrorKind.TRANSIENT
    assert classify(WebDriverException("net::ERR_NAME_NOT_RESOLVED")) is ErrorKind.TRANSIENT
    assert classify(WebDriverException("chrome not reachable")) is ErrorKind.TRANSIENT
    # A missing element that our code did not anticipate is a portal change signal
    assert classify(NoSuchElementException("no such element: #cf")) is ErrorKind.PORTAL_CHANGED


def test_classify_unknown_is_internal():
    assert classify(ValueError("bug")) is ErrorKind.INTERNAL
    assert classify(KeyError("x")) is ErrorKind.INTERNAL


def test_portal_changed_carries_diagnostics():
    error = PortalChanged("select#provincia", "search form", dom_snippet="<div>RSSMRA80A01F205X</div>")
    assert error.selector == "select#provincia"
    assert "RSSMRA80A01F205X" not in error.dom_snippet  # diagnostics are redacted at creation


def test_redact_masks_health_identifiers():
    text = ("CF RSSMRA80A01F205X ricetta 0300A1234567890 nre 030012345678901 tel +39 333 123 4567 "
            "mail mario.rossi@example.com")
    out = redact(text)
    for secret in ["RSSMRA80A01F205X", "0300A1234567890", "030012345678901", "333 123 4567", "mario.rossi@example.com"]:
        assert secret not in out
    assert "CF" in out and "ricetta" in out


def test_redact_keeps_dates_and_times():
    text = "Trovato 05/11/2026 - 08:30 in MILANO CITTA'"
    assert redact(text) == text


def test_logging_filter_redacts_message_and_args(caplog):
    logger = logging.getLogger("rl-test")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="rl-test"):
        logger.info("login con %s", "RSSMRA80A01F205X")
    assert "RSSMRA80A01F205X" not in caplog.text
