"""Which slots a search accepts: location (provinces / radius / facilities), dates, weekdays and hours."""
import enum
from datetime import time

from rlprenota.geo.distance import normalize_name

MAX_KM = 200

# The portal's "mattina"/"pomeriggio" boundary is not documented: these thresholds are deliberately
# conservative, so the portal never hides a slot that our exact time filter would accept.
MORNING_ENDS_BEFORE = time(14, 0)
AFTERNOON_STARTS_AFTER = time(12, 0)

WEEKDAY_NAMES = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]


class LocationMode(str, enum.Enum):
    PROVINCES = "provinces"
    RADIUS = "radius"
    FACILITIES = "facilities"


class Verdict:
    __slots__ = ("ok", "reason")

    def __init__(self, ok, reason=""):
        self.ok, self.reason = ok, reason

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"Verdict({self.ok}, {self.reason!r})"


ACCEPTED = Verdict(True)


class PortalHints:
    """Constraints that can be pushed into the portal's own search form."""

    def __init__(self, weekdays, morning, afternoon):
        self.weekdays, self.morning, self.afternoon = weekdays, morning, afternoon


def _tokens(text):
    return set(normalize_name(text).split())


class SearchFilter:
    def __init__(self, comuni, location_mode=LocationMode.PROVINCES, provinces=None, home_comune=None, max_km=None,
                 facilities=None, start_date=None, end_date=None, weekdays=None, time_from=None, time_to=None):
        self.comuni = comuni
        self.location_mode = LocationMode(location_mode)
        self.provinces = list(provinces or [])
        self.start_date, self.end_date = start_date, end_date
        self.weekdays = set(weekdays) if weekdays else None
        self.time_from, self.time_to = time_from, time_to
        self.facilities = [dict(f) for f in (facilities or [])]
        self.home = None
        self.max_km = max_km

        if self.weekdays is not None and not self.weekdays <= set(range(7)):
            raise ValueError("Giorni della settimana non validi")
        if time_from and time_to and time_from > time_to:
            raise ValueError("L'orario di inizio deve precedere quello di fine")
        if start_date and end_date and start_date > end_date:
            raise ValueError("La data di inizio deve precedere quella di fine")

        if self.location_mode is LocationMode.RADIUS:
            if max_km is None or not 0 < max_km <= MAX_KM:
                raise ValueError(f"La distanza massima deve essere tra 1 e {MAX_KM} km")
            self.home = comuni.find(home_comune)
            if self.home is None:
                raise ValueError(f"Comune di partenza sconosciuto: {home_comune}")
        elif self.location_mode is LocationMode.FACILITIES:
            if not self.facilities:
                raise ValueError("Scegli almeno una struttura")
            for facility in self.facilities:
                facility["_tokens"] = _tokens(facility.get("name"))
                if not facility["_tokens"]:
                    raise ValueError("Nome della struttura vuoto")

    # --- which provinces must be searched on the portal ---

    def provinces_to_search(self):
        if self.location_mode is LocationMode.RADIUS:
            return self.comuni.provinces_within(self.home.name, self.max_km)
        if self.location_mode is LocationMode.FACILITIES:
            seen = []
            for facility in self.facilities:
                if facility.get("province") and facility["province"] not in seen:
                    seen.append(facility["province"])
            return seen
        return list(self.provinces)

    def portal_hints(self):
        morning = self.time_from is None or self.time_from < MORNING_ENDS_BEFORE
        afternoon = self.time_to is None or self.time_to > AFTERNOON_STARTS_AFTER
        return PortalHints(set(self.weekdays) if self.weekdays else set(range(7)), morning, afternoon)

    # --- per-slot decision ---

    def accepts(self, slot):
        when = slot.when
        if self.start_date and when.date() < self.start_date:
            return Verdict(False, "prima della data di inizio")
        if self.end_date and when.date() > self.end_date:
            return Verdict(False, "dopo la data di fine")
        if self.weekdays is not None and when.weekday() not in self.weekdays:
            return Verdict(False, f"giorno non richiesto ({WEEKDAY_NAMES[when.weekday()]})")
        if self.time_from and when.time() < self.time_from:
            return Verdict(False, "orario troppo presto")
        if self.time_to and when.time() > self.time_to:
            return Verdict(False, "orario troppo tardi")

        if self.location_mode is LocationMode.RADIUS:
            comune = self.comuni.find(slot.comune)
            if comune is None:
                return Verdict(False, f"comune non riconosciuto ({slot.comune})")
            km = self.comuni.distance_km(self.home.name, comune.name)
            if km > self.max_km:
                return Verdict(False, f"troppo lontano ({km:.0f} km)")
        elif self.location_mode is LocationMode.FACILITIES:
            place = _tokens(f"{slot.sede} {slot.azienda}")
            if not any(f["_tokens"] <= place and (not f.get("province") or f["province"] == slot.provincia)
                       for f in self.facilities):
                return Verdict(False, "struttura non tra quelle scelte")
        return ACCEPTED
