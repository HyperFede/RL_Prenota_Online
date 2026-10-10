"""The portal's "no online availability" answer is a normal result, never a portal change (seen in production)."""
from types import SimpleNamespace
from unittest import mock

import pytest

from rlprenota.core import portal, runner
from rlprenota.core.errors import NoAvailability, PortalChanged
from rlprenota.core.models import SearchPreferences

IGNORED = runner.DEFAULT_IGNORED
PREFS = SearchPreferences(["COMO"], "", "", 60)

# What the page looked like in production: the search form gone, only the portal's alert on screen
CONTACT_CENTER_ALERT = """<!doctype html><html><body>
<div class="modal-dialog"><div class="modal-header"><span class="modal-title">Attenzione</span>
<button class="close">&times;</button></div>
<div class="modal-body">Attenzione: Al momento non ci sono disponibilita' online idonee alla prescrizione,
chiama il Contact Center Regionale</div>
<div class="modal-footer"><button class="btn btn-default">Chiudi</button></div></div>
</body></html>"""


def no_form_wait():
    wait = mock.patch.object(portal, "WebDriverWait")
    return wait


def test_missing_form_with_no_availability_alert_is_not_a_portal_change(load_html):
    driver = load_html(CONTACT_CENTER_ALERT)
    with mock.patch.object(portal, "WebDriverWait") as wait:
        wait.return_value.until.side_effect = portal.TimeoutException("no select")
        with pytest.raises(NoAvailability) as raised:
            portal.search_in_province(driver, IGNORED, "COMO", PREFS)
    assert "non ci sono disponibilit" in str(raised.value).lower()


def test_missing_form_without_any_alert_is_still_a_portal_change(load_html):
    driver = load_html("<p>pagina sconosciuta</p>")
    with mock.patch.object(portal, "WebDriverWait") as wait:
        wait.return_value.until.side_effect = portal.TimeoutException("no select")
        with pytest.raises(PortalChanged):
            portal.search_in_province(driver, IGNORED, "COMO", PREFS)


def test_contact_center_wording_counts_as_no_results(load_html):
    driver = load_html(CONTACT_CENTER_ALERT)
    assert portal.check_search_outcome(driver, "COMO", timeout=2) == "ERROR"


def session_for(provinces):
    prefs = SearchPreferences(provinces, "", "", 60)
    s = SimpleNamespace(driver=None, ignored_exceptions=IGNORED, prefs=prefs, filter=prefs.build_filter(),
                        before_search=None, open=mock.Mock())
    s.provinces = s.filter.provinces_to_search
    return s


def test_runner_relogs_and_retries_the_same_province():
    s = session_for(["BERGAMO", "BRESCIA", "COMO", "LECCO"])
    calls = [True, True, NoAvailability("Al momento non ci sono disponibilita' online"), True, True]
    with mock.patch.object(portal, "search_in_province", side_effect=calls) as search, \
            mock.patch.object(portal, "check_search_outcome", return_value="ERROR"), \
            mock.patch.object(portal, "cleanup_ui_for_next_search"):
        assert runner.run_cycle(s) == runner.CONTINUE
    searched = [c.args[2] for c in search.call_args_list]
    assert searched == ["BERGAMO", "BRESCIA", "COMO", "COMO", "LECCO"]   # Como is retried, not skipped
    s.open.assert_called_once_with(attempts=3)


def test_a_province_refused_twice_is_skipped_and_the_next_one_searched():
    s = session_for(["BERGAMO", "COMO", "LECCO"])
    calls = [True, NoAvailability("no"), NoAvailability("no"), True]
    with mock.patch.object(portal, "search_in_province", side_effect=calls) as search, \
            mock.patch.object(portal, "check_search_outcome", return_value="ERROR"), \
            mock.patch.object(portal, "cleanup_ui_for_next_search"):
        assert runner.run_cycle(s) == runner.CONTINUE
    assert [c.args[2] for c in search.call_args_list] == ["BERGAMO", "COMO", "COMO", "LECCO"]
    assert s.open.call_count == 2                  # once to retry Como, once to get the form back for Lecco


def test_runner_stops_the_cycle_quietly_when_the_portal_keeps_saying_no():
    s = session_for(["BERGAMO", "BRESCIA", "COMO"])
    with mock.patch.object(portal, "search_in_province", side_effect=NoAvailability("niente")) as search, \
            mock.patch.object(portal, "cleanup_ui_for_next_search"):
        assert runner.run_cycle(s) == runner.CONTINUE  # a normal "nothing now", retried at the next interval
    # Bergamo, Bergamo again after re-login, then Brescia right after another re-login: the portal refuses everything
    assert [c.args[2] for c in search.call_args_list] == ["BERGAMO", "BERGAMO", "BRESCIA"]
    assert s.open.call_count == 2


def test_cli_loop_keeps_trying_when_the_portal_says_no_at_login():
    s = session_for(["COMO"])
    s.mode = None
    opens = [NoAvailability("niente"), None]

    def open_():
        result = opens.pop(0)
        if result:
            raise result
        s.mode = portal.MODE_RESCHEDULE
    s.open = open_
    s.decider = SimpleNamespace(idle=lambda session, seconds: None)
    with mock.patch.object(runner, "run_cycle", side_effect=[runner.STOP]):
        runner.search_loop(s)        # first login said "no availability", the second worked and the cycle booked
    assert opens == []
