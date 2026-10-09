"""Offline distances between Lombardy comuni (centroids from OpenStreetMap, ODbL).

Only the user's home *comune* is needed: no address ever leaves the NAS.
"""
import csv
import math
import os
import re
import unicodedata
from functools import lru_cache

TABLE = os.path.join(os.path.dirname(__file__), "comuni_lombardia.csv")
EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def normalize_name(name):
    """'Cantù', "CANTU'", 'Milano (MI)' and 'MILANO - MI' -> 'CANTU' / 'MILANO'."""
    name = re.sub(r"\(.*?\)|\s-\s.*$", " ", name or "")
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split())


class Comune:
    __slots__ = ("istat", "name", "province", "lat", "lon")

    def __init__(self, istat, name, province, lat, lon):
        self.istat, self.name, self.province, self.lat, self.lon = istat, name, province, lat, lon

    def __repr__(self):
        return f"Comune({self.name!r}, {self.province!r})"


class ComuniIndex:
    def __init__(self, comuni):
        self._by_name = {}
        for comune in comuni:
            self._by_name.setdefault(normalize_name(comune.name), comune)
        self._all = list(comuni)

    @classmethod
    @lru_cache(maxsize=1)
    def load(cls, path=TABLE):
        with open(path, encoding="utf-8") as f:
            rows = [Comune(r["istat"], r["nome"], r["provincia"], float(r["lat"]), float(r["lon"])) for r in csv.DictReader(f)]
        return cls(rows)

    def __len__(self):
        return len(self._all)

    def find(self, name):
        return self._by_name.get(normalize_name(name)) if name else None

    def provinces(self):
        return sorted({c.province for c in self._all})

    def distance_km(self, a, b):
        ca, cb = self.find(a), self.find(b)
        if ca is None or cb is None:
            return None
        return haversine_km(ca.lat, ca.lon, cb.lat, cb.lon)

    def provinces_within(self, home, km):
        """Provinces having at least one comune within `km` of `home` (in a stable order)."""
        origin = self.find(home)
        if origin is None:
            raise ValueError(f"Comune sconosciuto: {home}")
        found = {c.province for c in self._all if haversine_km(origin.lat, origin.lon, c.lat, c.lon) <= km}
        found.add(origin.province)
        return sorted(found)
