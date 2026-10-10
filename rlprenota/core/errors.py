"""Error taxonomy used to decide how a failed search run is handled."""
import enum

from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException, TimeoutException,
                                        WebDriverException)

from rlprenota.core.redact import redact


class ErrorKind(enum.Enum):
    TRANSIENT = "transient"            # network/slow portal: retry with backoff
    LOGIN_REJECTED = "login_rejected"  # wrong data or expired prescription: pause and tell the user
    PORTAL_CHANGED = "portal_changed"  # page structure not as expected: may need a code update
    THROTTLED = "throttled"            # the portal is rate limiting us: back off globally
    INTERNAL = "internal"              # bug on our side


class PortalError(Exception):
    pass


class PortalChanged(PortalError):
    def __init__(self, selector, step, dom_snippet=""):
        self.selector = selector
        self.step = step
        self.dom_snippet = redact(dom_snippet or "")[:1000]
        super().__init__(f"Elemento '{selector}' non trovato durante: {step}")


class LoginRejected(PortalError):
    pass


class NoAvailability(PortalError):
    """The portal answered that there is nothing bookable online right now: a normal result, retried later."""


class PortalThrottled(PortalError):
    pass


_TRANSIENT_MARKERS = ("net::err", "chrome not reachable", "disconnected", "timed out", "timeout",
                      "session deleted", "no such window", "target window already closed", "connection refused")


def classify(error):
    if isinstance(error, PortalChanged):
        return ErrorKind.PORTAL_CHANGED
    if isinstance(error, LoginRejected):
        return ErrorKind.LOGIN_REJECTED
    if isinstance(error, PortalThrottled):
        return ErrorKind.THROTTLED
    if isinstance(error, (TimeoutException, StaleElementReferenceException)):
        return ErrorKind.TRANSIENT
    if isinstance(error, NoSuchElementException):
        return ErrorKind.PORTAL_CHANGED
    if isinstance(error, WebDriverException):
        message = (error.msg or str(error) or "").lower()
        return ErrorKind.TRANSIENT if any(m in message for m in _TRANSIENT_MARKERS) else ErrorKind.INTERNAL
    if isinstance(error, (ConnectionError, OSError)):
        return ErrorKind.TRANSIENT
    return ErrorKind.INTERNAL
