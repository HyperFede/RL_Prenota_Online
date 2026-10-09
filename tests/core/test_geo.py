import pytest

from rlprenota.geo.distance import ComuniIndex, haversine_km


@pytest.fixture(scope="module")
def comuni():
    return ComuniIndex.load()


def test_table_covers_lombardy(comuni):
    assert len(comuni) > 1400
    assert comuni.find("Milano").province == "MILANO CITTA'"
    assert comuni.find("Sesto San Giovanni").province == "MILANO PROVINCIA"
    assert comuni.find("Monza").province == "MONZA E DELLA BRIANZA"


@pytest.mark.parametrize("variant", ["MILANO", "milano", "  Milano ", "Milano (MI)", "MILANO - MI"])
def test_find_tolerates_portal_spellings(comuni, variant):
    assert comuni.find(variant).name == "Milano"


def test_find_handles_accents_and_apostrophes(comuni):
    assert comuni.find("CANTU'").name == "Cantù"
    assert comuni.find("Cantu").name == "Cantù"


def test_unknown_comune_is_none(comuni):
    assert comuni.find("Roma") is None
    assert comuni.find("") is None
    assert comuni.find(None) is None


def test_haversine_known_distance():
    # London (51.5074, -0.1278) -> Paris (48.8566, 2.3522): 343.5 km great-circle distance
    assert 343 < haversine_km(51.5074, -0.1278, 48.8566, 2.3522) < 344.5
    assert haversine_km(45.0, 9.0, 45.0, 9.0) == 0


def test_distance_between_comuni(comuni):
    assert 44 < comuni.distance_km("Milano", "Bergamo") < 50  # centroids ~47 km apart
    assert comuni.distance_km("Milano", "Roma") is None


def test_provinces_within_radius(comuni):
    near = comuni.provinces_within("Milano", 15)
    assert "MILANO CITTA'" in near and "MILANO PROVINCIA" in near
    assert "SONDRIO" not in near and "MANTOVA" not in near
    assert set(comuni.provinces_within("Milano", 300)) == set(comuni.provinces())
    with pytest.raises(ValueError):
        comuni.provinces_within("Roma", 10)
