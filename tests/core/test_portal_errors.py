"""Typed errors and native-filter hints of rlprenota.core.portal, on small real DOM pages."""
from datetime import time
from types import SimpleNamespace
from unittest import mock

import pytest

from rlprenota.core import portal, runner
from rlprenota.core.errors import LoginRejected, PortalChanged
from rlprenota.core.filters import PortalHints
from rlprenota.core.models import SearchPreferences

IGNORED = runner.DEFAULT_IGNORED
PREFS = SearchPreferences(["MILANO CITTA'"], "", "", 60)

SEARCH_FORM = """<!doctype html><html><body>
<select id="provincia"><option></option><option>BERGAMO</option><option>MILANO CITTA'</option></select>
<label><input id="lun" type="checkbox" checked></label><label><input id="mar" type="checkbox" checked></label>
<label><input id="mer" type="checkbox" checked></label><label><input id="gio" type="checkbox" checked></label>
<label><input id="ven" type="checkbox" checked></label><label><input id="sab" type="checkbox" checked></label>
<label><input id="dom" type="checkbox" checked></label>
<label><input id="mattina" type="checkbox" checked></label><label><input id="pomeriggio" type="checkbox" checked></label>
<button class="submit" onclick="window.submitted = document.getElementById('provincia').value">Conferma</button>
</body></html>"""


def test_detect_booking_mode_portal_changed(load_html):
    driver = load_html("<p>Una pagina completamente diversa, nome RSSMRA80A01F205X</p>")
    with pytest.raises(PortalChanged) as raised:
        portal.detect_booking_mode(driver, timeout=1)
    assert "RSSMRA80A01F205X" not in raised.value.dom_snippet
    assert "diversa" in raised.value.dom_snippet


def test_search_without_province_select_is_portal_changed(load_html):
    driver = load_html("<p>niente modulo</p>")
    with mock.patch.object(portal, "WebDriverWait") as wait:
        wait.return_value.until.side_effect = portal.TimeoutException("no select")
        with pytest.raises(PortalChanged) as raised:
            portal.search_in_province(driver, IGNORED, "MILANO CITTA'", PREFS)
    assert raised.value.selector == "select#provincia"


def test_unknown_province_needs_user_action(load_html):
    driver = load_html(SEARCH_FORM)
    with pytest.raises(LoginRejected) as raised:
        portal.search_in_province(driver, IGNORED, "ROMA", PREFS)
    assert "BERGAMO" in str(raised.value)  # tells the user what is available


def test_search_applies_portal_hints_and_submits(load_html):
    driver = load_html(SEARCH_FORM)
    hints = PortalHints(weekdays={0, 1, 2, 3, 4}, morning=True, afternoon=False)
    assert portal.search_in_province(driver, IGNORED, "milano citta", PREFS, hints) is True
    checked = driver.execute_script(
        "return ['lun','mar','mer','gio','ven','sab','dom','mattina','pomeriggio'].map(function(i){return document.getElementById(i).checked;});")
    assert checked == [True, True, True, True, True, False, False, True, False]
    assert driver.execute_script("return window.submitted;") == "MILANO CITTA'"


def test_completa_dati_without_answer_asks_user_to_configure(load_html):
    driver = load_html("""<div class="modal-dialog"><input name="controllo" type="radio"><input name="controllo" type="radio">
        <button ng-click="prenotaCompletaDatiCtrl.conferma()">Conferma</button></div>""")
    prefs = SearchPreferences([], "", "", 60, visita_controllo=None)
    with pytest.raises(LoginRejected):
        portal.handle_completa_dati_modal(driver, prefs, IGNORED)


def test_run_cycle_rate_limits_and_propagates_portal_changes():
    calls = []
    session = SimpleNamespace(driver=None, ignored_exceptions=IGNORED, prefs=PREFS,
                              filter=SearchPreferences(["BERGAMO", "MILANO CITTA'"], "", "", 60).build_filter(),
                              before_search=lambda: calls.append("tick"))
    session.provinces = session.filter.provinces_to_search
    with mock.patch.object(portal, "search_in_province", side_effect=[False, PortalChanged("x", "y")]), \
            mock.patch.object(portal, "cleanup_ui_for_next_search"):
        with pytest.raises(PortalChanged):
            runner.run_cycle(session)
    assert calls == ["tick", "tick"]  # the limiter is consulted before every portal search


def test_time_hints_flow_from_preferences():
    prefs = SearchPreferences(["BERGAMO"], "", "", 60, weekdays={5}, time_from="15:00", time_to="18:00")
    hints = prefs.build_filter().portal_hints()
    assert hints.weekdays == {5} and not hints.morning and hints.afternoon
    assert prefs.build_filter().time_from == time(15, 0)
