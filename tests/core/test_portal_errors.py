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


def test_province_missing_from_the_list_is_a_temporary_hiccup(load_html):
    """Production: the list was being re-rendered and 'COMO' did not match. Never blame the user's data."""
    from rlprenota.core.errors import PortalHiccup
    driver = load_html(SEARCH_FORM)
    with pytest.raises(PortalHiccup):
        portal.search_in_province(driver, IGNORED, "ROMA", PREFS)


def test_province_is_found_even_when_its_option_text_is_not_rendered(load_html):
    driver = load_html(SEARCH_FORM.replace("<option>MILANO CITTA'</option>",
                                          "<option style=\"display:none\">MILANO CITTA'</option>"))
    assert portal.search_in_province(driver, IGNORED, "MILANO CITTA'", PREFS) is True
    assert driver.execute_script("return window.submitted;") == "MILANO CITTA'"


def test_only_real_data_problems_after_login_blame_the_user(load_html):
    from rlprenota.core.errors import PortalHiccup
    driver = load_html('<div class="modal-dialog"><span class="modal-title">Attenzione</span>'
                       'La sessione è scaduta, effettua nuovamente l\'accesso</div>')
    with pytest.raises(PortalHiccup):
        portal.detect_booking_mode(driver, timeout=2)
    driver = load_html('<div class="modal-dialog"><span class="modal-title">Attenzione</span>'
                       'La ricetta indicata non è valida o è scaduta</div>')
    with pytest.raises(LoginRejected):
        portal.detect_booking_mode(driver, timeout=2)


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
                              before_search=lambda: calls.append("tick"), open=mock.Mock())
    session.provinces = session.filter.provinces_to_search
    session.portal_hints = session.filter.portal_hints
    changed = [False, PortalChanged("x", "y"), PortalChanged("x", "y")]
    with mock.patch.object(portal, "search_in_province", side_effect=changed), \
            mock.patch.object(portal, "cleanup_ui_for_next_search"):
        with pytest.raises(PortalChanged):   # still missing right after a fresh login
            runner.run_cycle(session)
    assert calls == ["tick", "tick", "tick"]  # the limiter is consulted before every portal search
    session.open.assert_called_once()


def test_time_hints_flow_from_preferences():
    prefs = SearchPreferences(["BERGAMO"], "", "", 60, weekdays={5}, time_from="15:00", time_to="18:00")
    hints = prefs.build_filter().portal_hints()
    assert hints.weekdays == {5} and not hints.morning and hints.afternoon
    assert prefs.build_filter().time_from == time(15, 0)


def test_postpone_sets_the_portal_start_date(load_html):
    from datetime import date
    driver = load_html(SEARCH_FORM.replace("<button", '<input id="quando" type="text" value="22/10/2026" '
                                                     'oninput="window.typed = this.value"><button'))
    hints = PortalHints(weekdays=set(range(7)), morning=True, afternoon=True, start_from=date(2027, 5, 19))
    assert portal.search_in_province(driver, IGNORED, "BERGAMO", PREFS, hints) is True
    assert driver.execute_script("return window.typed;") == "19/05/2027"     # AngularJS hears the input event


def test_missing_start_date_field_is_not_an_error(load_html):
    from datetime import date
    driver = load_html(SEARCH_FORM)
    assert portal.set_start_date(driver, date(2027, 5, 19)) is False


def test_postpone_without_a_booked_appointment_asks_the_user():
    prefs = SearchPreferences(["BERGAMO"], "", "", 60, direzione="posticipa")
    session = runner.SearchSession(None, None, prefs, None, None)
    with mock.patch.object(portal, "open_search_flow", return_value=(portal.MODE_NEW, None)):
        with pytest.raises(LoginRejected, match="posticipare"):
            session.open()


def test_one_month_after():
    from datetime import datetime
    assert runner.one_month_after(datetime(2027, 1, 31, 9, 0)) == datetime(2027, 2, 28, 9, 0)
    assert runner.one_month_after(datetime(2026, 12, 15)) == datetime(2027, 1, 15)
    assert runner.one_month_after(datetime(2028, 1, 30)) == datetime(2028, 2, 29)


def test_postpone_hints_start_from_the_booked_day():
    from datetime import date
    from rlprenota.core.models import Appointment
    session = runner.SearchSession(None, None, SearchPreferences(["BERGAMO"], "", "", 60, direzione="posticipa"), None, None)
    session.current_appointment = Appointment("19/05/2027 - 08:00", "Varese", None)
    assert session.portal_hints().start_from == date(2027, 5, 19)
    assert session.max_result_pages() == runner.POSTPONE_RESULT_PAGES
    normal = runner.SearchSession(None, None, SearchPreferences(["BERGAMO"], "", "", 60), None, None)
    normal.current_appointment = session.current_appointment
    assert normal.portal_hints().start_from is None and normal.max_result_pages() == portal.MAX_RESULT_PAGES


def test_hybrid_keeps_the_portal_start_date_and_walks_deeper():
    from rlprenota.core.models import Appointment
    session = runner.SearchSession(None, None, SearchPreferences(["BERGAMO"], "", "", 60, direzione="entrambe"), None, None)
    session.current_appointment = Appointment("19/05/2027 - 08:00", "Varese", None)
    assert session.portal_hints().start_from is None                   # the earlier slots are wanted too
    assert session.max_result_pages() == runner.POSTPONE_RESULT_PAGES
    with mock.patch.object(portal, "open_search_flow", return_value=(portal.MODE_NEW, None)):
        session.open()                                                 # nothing booked yet: a first booking
    assert session.current_appointment is None


# --- results of the previous province must never be read as the current one's ---

def _slot(when, comune, sede="Ospedale"):
    return {"when": when, "azienda": "ASST", "comune": comune, "sede": sede}


def test_slot_province_comes_from_its_comune(load_html):
    from tests.fakes import portal_page
    driver = load_html(portal_page([_slot("05/11/2026 - 08:30", "BREGNANO"), _slot("06/11/2026 - 08:30", "NOWHERE")]))
    slots = portal.extract_slots(driver, "MILANO CITTA'")
    assert [s.provincia for s in slots] == ["COMO", "MILANO CITTA'"]   # unknown comune: the searched province


def test_same_appointment_has_one_identity_whatever_was_searched(load_html):
    from tests.fakes import portal_page
    driver = load_html(portal_page([_slot("05/11/2026 - 08:30", "MANERBIO")]))
    seen_as_brescia = portal.extract_slots(driver, "BRESCIA")[0]
    seen_as_como = portal.extract_slots(driver, "COMO")[0]                # stale read during the next search
    assert seen_as_como.slot_id == seen_as_brescia.slot_id and seen_as_como.provincia == "BRESCIA"


def test_outcome_waits_until_the_previous_list_is_replaced(load_html):
    from tests.fakes import portal_page
    driver = load_html(portal_page([_slot("05/11/2026 - 08:30", "MANERBIO")]))
    stale = portal.results_fingerprint(driver)
    assert "MANERBIO" in stale
    assert portal.check_search_outcome(driver, "COMO", timeout=1.5, stale=stale) == "TIMEOUT"
    driver.execute_script("setTimeout(function () { SLOTS = [{when: '07/11/2026 - 10:00', azienda: 'ASST', "
                          "comune: 'BREGNANO', sede: 'Radiologia'}]; page = 0; render(); }, 1000);")
    assert portal.check_search_outcome(driver, "COMO", timeout=5, stale=stale) == "RESULTS"
    assert [s.comune for s in portal.extract_slots(driver, "COMO")] == ["BREGNANO"]


def test_runner_checks_for_stale_results_only_after_another_province():
    prefs = SearchPreferences(["BERGAMO", "BRESCIA"], "", "", 60)
    session = SimpleNamespace(driver=None, ignored_exceptions=IGNORED, prefs=prefs, filter=prefs.build_filter(),
                              before_search=None, open=mock.Mock())
    session.provinces = session.filter.provinces_to_search
    session.portal_hints = session.filter.portal_hints
    outcomes = mock.Mock(return_value="RESULTS")
    with mock.patch.object(portal, "search_in_province", return_value=True), \
            mock.patch.object(portal, "results_fingerprint", return_value="lista di BERGAMO"), \
            mock.patch.object(portal, "check_search_outcome", outcomes), \
            mock.patch.object(portal, "cleanup_ui_for_next_search"), \
            mock.patch.object(runner, "process_results", return_value="NEXT"):
        runner.run_cycle(session)
    stale = [c.kwargs.get("stale") for c in outcomes.call_args_list]
    assert stale == ["", "lista di BERGAMO"]       # first province: nothing of this cycle on the page yet
