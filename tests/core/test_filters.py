from datetime import datetime, time

import pytest

from rlprenota.core.filters import SearchFilter, LocationMode
from rlprenota.core.models import Slot
from rlprenota.geo.distance import ComuniIndex

COMUNI = ComuniIndex.load()


def slot(when="05/11/2026 08:30", comune="MILANO", sede="Ospedale Niguarda", azienda="ASST Grande Ospedale Metropolitano Niguarda",
         prov="MILANO CITTA'"):
    return Slot(datetime.strptime(when, "%d/%m/%Y %H:%M"), azienda, sede, comune, prov)


def make(**kwargs):
    return SearchFilter(comuni=COMUNI, **kwargs)


def test_no_constraints_accepts_everything():
    assert make().accepts(slot()).ok


def test_date_window_is_inclusive_on_whole_days():
    f = make(start_date=datetime(2026, 11, 5).date(), end_date=datetime(2026, 11, 5).date())
    assert f.accepts(slot("05/11/2026 00:00")).ok
    assert f.accepts(slot("05/11/2026 23:59")).ok
    assert not f.accepts(slot("04/11/2026 23:59")).ok
    assert not f.accepts(slot("06/11/2026 00:00")).ok


def test_weekdays():
    f = make(weekdays={0, 2})  # Monday, Wednesday; 2 Nov 2026 is a Monday
    assert f.accepts(slot("02/11/2026 09:00")).ok
    assert f.accepts(slot("04/11/2026 09:00")).ok
    verdict = f.accepts(slot("03/11/2026 09:00"))
    assert not verdict.ok and "giorno" in verdict.reason


@pytest.mark.parametrize("hhmm,ok", [("07:59", False), ("08:00", True), ("12:59", True), ("13:00", True), ("13:01", False)])
def test_time_window_inclusive_bounds(hhmm, ok):
    f = make(time_from=time(8, 0), time_to=time(13, 0))
    assert f.accepts(slot(f"05/11/2026 {hhmm}")).ok is ok


def test_time_window_rejects_inverted_bounds():
    with pytest.raises(ValueError):
        make(time_from=time(14, 0), time_to=time(9, 0))


def test_radius_mode():
    f = make(location_mode=LocationMode.RADIUS, home_comune="Milano", max_km=20)
    assert f.accepts(slot(comune="MILANO")).ok
    assert f.accepts(slot(comune="SESTO SAN GIOVANNI")).ok
    far = f.accepts(slot(comune="BERGAMO", prov="BERGAMO"))
    assert not far.ok and "km" in far.reason
    unknown = f.accepts(slot(comune="BOH"))
    assert not unknown.ok and "comune" in unknown.reason


@pytest.mark.parametrize("km", [0, -5, 201])
def test_radius_bounds_validated(km):
    with pytest.raises(ValueError):
        make(location_mode=LocationMode.RADIUS, home_comune="Milano", max_km=km)


def test_radius_requires_known_home():
    with pytest.raises(ValueError):
        make(location_mode=LocationMode.RADIUS, home_comune="Roma", max_km=10)


def test_radius_provinces_to_search():
    f = make(location_mode=LocationMode.RADIUS, home_comune="Milano", max_km=15)
    assert "MILANO CITTA'" in f.provinces_to_search() and "SONDRIO" not in f.provinces_to_search()


def test_facilities_mode_fuzzy_match():
    f = make(location_mode=LocationMode.FACILITIES,
             facilities=[{"name": "niguarda", "province": "MILANO CITTA'"},
                         {"name": "Ospedale di Circolo Varese", "province": "VARESE"}])
    assert f.accepts(slot(sede="OSPEDALE NIGUARDA - PAD. 1")).ok
    assert f.accepts(slot(sede="Presidio Ospedale di Circolo", azienda="ASST Sette Laghi Varese", prov="VARESE")).ok
    assert not f.accepts(slot(sede="Ospedale San Carlo", azienda="ASST Santi Paolo e Carlo")).ok
    assert f.provinces_to_search() == ["MILANO CITTA'", "VARESE"]


def test_facilities_mode_needs_at_least_one():
    with pytest.raises(ValueError):
        make(location_mode=LocationMode.FACILITIES, facilities=[])


def test_provinces_mode():
    f = make(location_mode=LocationMode.PROVINCES, provinces=["MILANO CITTA'", "BERGAMO"])
    assert f.provinces_to_search() == ["MILANO CITTA'", "BERGAMO"]
    assert f.accepts(slot()).ok


def test_portal_hints_for_native_filters():
    f = make(weekdays={0, 1, 2, 3, 4}, time_from=time(8, 0), time_to=time(12, 0))
    hints = f.portal_hints()
    assert hints.weekdays == {0, 1, 2, 3, 4}
    assert hints.morning is True and hints.afternoon is False
    afternoon = make(time_from=time(14, 0)).portal_hints()
    assert afternoon.morning is False and afternoon.afternoon is True
    # A window crossing midday must keep both halves enabled
    both = make(time_from=time(11, 0), time_to=time(15, 0)).portal_hints()
    assert both.morning and both.afternoon
