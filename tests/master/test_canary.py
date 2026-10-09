import pytest

from rlprenota.master.canary import REQUIRED_MARKERS, Canary, CanaryFetchError

GOOD_JS = " ".join(REQUIRED_MARKERS).encode()
HTML = '<html><script src=scripts/vendor-1.js></script><script src=scripts/app-abc123.js></script></html>'


class Pauser:
    def __init__(self):
        self.reasons = []

    def pause_global(self, reason):
        self.reasons.append(reason)


class Alerts:
    def __init__(self):
        self.admin = []

    def alert_admin(self, key, text):
        self.admin.append((key, text))


def fetcher(js=GOOD_JS, html=HTML, fail=False):
    def fetch(url):
        if fail:
            raise CanaryFetchError("HTTP 502")
        return html.encode() if url.endswith("/prenotaonline/") else js
    return fetch


@pytest.fixture
def parts(db, clock):
    return Pauser(), Alerts()


def test_healthy_portal(db, clock, parts):
    pauser, alerts = parts
    result = Canary(db, pauser, alerts, fetch=fetcher(), clock=clock).run()
    assert result.status == "ok" and not pauser.reasons and not alerts.admin
    assert db.get_setting("canary_status") == "ok"


def test_missing_selector_pauses_everything(db, clock, parts):
    pauser, alerts = parts
    broken = GOOD_JS.replace(b"btn-riprenota", b"btn-rebook").replace(b"presaVisioneNote", b"")
    result = Canary(db, pauser, alerts, fetch=fetcher(js=broken), clock=clock).run()
    assert result.status == "markers_missing"
    assert "btn-riprenota" in pauser.reasons[0] and "presaVisioneNote" in alerts.admin[0][1]


def test_new_bundle_with_all_markers_is_only_a_heads_up(db, clock, parts):
    pauser, alerts = parts
    canary = Canary(db, pauser, alerts, fetch=fetcher(), clock=clock)
    canary.run()
    changed = Canary(db, pauser, alerts, fetch=fetcher(js=GOOD_JS + b" nuova versione"), clock=clock)
    assert changed.run().status == "bundle_changed"
    assert not pauser.reasons and "aggiornato" in alerts.admin[-1][1]
    assert changed.run().status == "ok"          # the new hash is now the known one


def test_unreachable_portal_alerts_only_after_three_failures(db, clock, parts):
    pauser, alerts = parts
    canary = Canary(db, pauser, alerts, fetch=fetcher(fail=True), clock=clock)
    for _ in range(2):
        assert canary.run().status == "unreachable"
    assert not alerts.admin
    canary.run()
    assert alerts.admin and not pauser.reasons
    Canary(db, pauser, alerts, fetch=fetcher(), clock=clock).run()
    assert db.get_setting("canary_failures") == "0"


def test_page_without_bundle_is_a_change(db, clock, parts):
    pauser, alerts = parts
    result = Canary(db, pauser, alerts, fetch=fetcher(html="<html>manutenzione</html>"), clock=clock).run()
    assert result.status == "unreachable"   # maintenance page: counted as unreachable, not as a code change
