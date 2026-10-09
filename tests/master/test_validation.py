from datetime import date, time

import pytest

from rlprenota.master.validation import (ValidationError, SearchSettings, SearchSecrets, valid_codice_fiscale)

TODAY = date(2026, 10, 9)


@pytest.mark.parametrize("cf", ["MRTMTT25D09F205Z", "mrtmtt25d09f205z", " MRTMTT25D09F205Z ",
                                "MRTMTT25D09F2LRF"])  # omocodia: digits of the comune code replaced by letters
def test_valid_codice_fiscale(cf):
    assert valid_codice_fiscale(cf)


@pytest.mark.parametrize("cf", ["MRTMTT25D09F205A", "MRTMTT25D09F205", "MRTMTT25D09F205ZZ", "1234567890123456", ""])
def test_invalid_codice_fiscale(cf):
    assert not valid_codice_fiscale(cf)


def secrets_form(**overrides):
    form = {"codice_fiscale": "mrtmtt25d09f205z", "tessera": "12345", "ricetta": "0300a1234567890",
            "telefono": "+39 333 123 4567", "email": ""}
    form.update(overrides)
    return form


def test_secrets_are_normalized():
    s = SearchSecrets.from_form(secrets_form())
    assert (s.codice_fiscale, s.tessera, s.ricetta, s.telefono) == ("MRTMTT25D09F205Z", "12345", "0300A1234567890", "3331234567")


@pytest.mark.parametrize("field,value", [("codice_fiscale", "MRTMTT25D09F205A"), ("tessera", "1234"), ("tessera", "12a45"),
                                         ("ricetta", "AB1"), ("ricetta", "0300A1234567890123"), ("ricetta", "0300A-<script>"),
                                         ("telefono", "12"), ("email", "not-an-email")])
def test_invalid_secrets(field, value):
    with pytest.raises(ValidationError) as raised:
        SearchSecrets.from_form(secrets_form(**{field: value}))
    assert raised.value.field == field


def test_masked_secrets_never_show_full_values():
    masked = SearchSecrets.from_form(secrets_form()).masked()
    assert masked["codice_fiscale"] == "••••••••••••205Z" and masked["ricetta"].endswith("7890")
    assert "MRTMTT" not in str(masked)


def settings_form(**overrides):
    form = {"label": "Visita cardiologica", "location_mode": "provinces", "province": ["MILANO CITTA'", "BERGAMO"],
            "start_date": "2026-10-10", "end_date": "2026-12-31", "refresh_seconds": "120", "dry_run": "on",
            "visita_controllo": "", "telegram_timeout": "15"}
    form.update(overrides)
    return form


def test_settings_happy_path():
    s = SearchSettings.from_form(settings_form(weekdays=["0", "2"], time_from="08:00", time_to="12:30"), today=TODAY)
    assert s.province == ["MILANO CITTA'", "BERGAMO"] and s.dry_run is True and s.refresh_seconds == 120
    assert s.weekdays == [0, 2] and s.time_from == "08:00" and s.visita_controllo is None
    prefs = s.to_preferences(SearchSecrets.from_form(secrets_form()))
    assert prefs.start_date == "10/10/2026" and prefs.end_date == "31/12/2026" and prefs.telefono == "3331234567"
    assert prefs.build_filter().time_to == time(12, 30)
    assert SearchSettings.from_json(s.to_json()) == s


@pytest.mark.parametrize("overrides,field", [
    ({"province": ["ROMA"]}, "province"),
    ({"province": []}, "province"),
    ({"refresh_seconds": "30"}, "refresh_seconds"),
    ({"refresh_seconds": "abc"}, "refresh_seconds"),
    ({"end_date": "2026-01-01"}, "end_date"),            # before start
    ({"end_date": "2026-10-01", "start_date": ""}, "end_date"),  # in the past
    ({"start_date": "31/12/2026"}, "start_date"),        # wrong format
    ({"location_mode": "radius", "home_comune": "Roma", "max_km": "10"}, "home_comune"),
    ({"location_mode": "radius", "home_comune": "Milano", "max_km": "500"}, "max_km"),
    ({"location_mode": "facilities", "facilities": ""}, "facilities"),
    ({"location_mode": "teleport"}, "location_mode"),
    ({"weekdays": ["7"]}, "weekdays"),
    ({"time_from": "14:00", "time_to": "09:00"}, "time_to"),
    ({"time_from": "25:00"}, "time_from"),
    ({"label": "x" * 61}, "label"),
    ({"telegram_timeout": "0"}, "telegram_timeout"),
])
def test_invalid_settings(overrides, field):
    with pytest.raises(ValidationError) as raised:
        SearchSettings.from_form(settings_form(**overrides), today=TODAY)
    assert raised.value.field == field


def test_radius_and_facilities_settings():
    s = SearchSettings.from_form(settings_form(location_mode="radius", home_comune="sesto san giovanni", max_km="30"), today=TODAY)
    assert s.home_comune == "Sesto San Giovanni" and s.max_km == 30
    f = SearchSettings.from_form(settings_form(location_mode="facilities",
                                               facilities="Niguarda | MILANO CITTA'\nOspedale di Circolo | VARESE"), today=TODAY)
    assert f.facilities == [{"name": "Niguarda", "province": "MILANO CITTA'"},
                            {"name": "Ospedale di Circolo", "province": "VARESE"}]
