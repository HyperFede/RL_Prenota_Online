"""Strict validation of everything users type into the web app."""
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from rlprenota.core.filters import MAX_KM, LocationMode
from rlprenota.core.models import SearchPreferences
from rlprenota.geo.distance import ComuniIndex

PROVINCES = ["BERGAMO", "BRESCIA", "COMO", "CREMONA", "LECCO", "LODI", "MANTOVA", "MILANO CITTA'", "MILANO PROVINCIA",
             "MONZA E DELLA BRIANZA", "PAVIA", "SONDRIO", "VARESE"]
MIN_REFRESH_SECONDS = 60
MAX_REFRESH_SECONDS = 3600
MAX_FACILITIES = 10


class ValidationError(ValueError):
    def __init__(self, field_name, message):
        super().__init__(message)
        self.field = field_name
        self.message = message


# --- codice fiscale (official check character, including "omocodia") ---

_ODD = dict(zip("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                [1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 1, 0, 5, 7, 9, 13, 15, 17, 19, 21, 2, 4, 18, 20, 11, 3, 6, 8, 12, 14,
                 16, 10, 22, 25, 24, 23]))
_EVEN = {c: (int(c) if c.isdigit() else ord(c) - ord("A")) for c in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
_OMO = "[0-9LMNPQRSTUV]"
_CF_RE = re.compile(rf"^[A-Z]{{6}}{_OMO}{{2}}[ABCDEHLMPRST]{_OMO}{{2}}[A-Z]{_OMO}{{3}}[A-Z]$")


def cf_check_char(first15):
    total = sum(_ODD[c] if i % 2 == 0 else _EVEN[c] for i, c in enumerate(first15))
    return chr(ord("A") + total % 26)


def valid_codice_fiscale(value):
    cf = (value or "").strip().upper()
    return bool(_CF_RE.match(cf)) and cf_check_char(cf[:15]) == cf[15]


def _mask(value, visible=4):
    return "•" * max(0, len(value) - visible) + value[-visible:] if value else ""


@dataclass(frozen=True)
class SearchSecrets:
    """Personal data needed to log in to the portal: stored only encrypted, never shown back in full."""
    codice_fiscale: str
    tessera: str
    ricetta: str
    telefono: str = ""
    email: str = ""

    @classmethod
    def from_form(cls, form):
        cf = (form.get("codice_fiscale") or "").strip().upper()
        if not valid_codice_fiscale(cf):
            raise ValidationError("codice_fiscale", "Codice fiscale non valido")
        tessera = (form.get("tessera") or "").strip()
        if not re.fullmatch(r"\d{5}", tessera):
            raise ValidationError("tessera", "Inserisci le ultime 5 cifre della tessera sanitaria")
        ricetta = re.sub(r"[\s-]", "", form.get("ricetta") or "").upper()
        if not re.fullmatch(r"[A-Z0-9]{5,16}", ricetta):
            raise ValidationError("ricetta", "Codice ricetta non valido (da 5 a 16 lettere o cifre)")
        telefono = re.sub(r"[\s./-]", "", form.get("telefono") or "")
        telefono = re.sub(r"^(\+|00)39", "", telefono)
        if telefono and not re.fullmatch(r"\d{6,15}", telefono):
            raise ValidationError("telefono", "Numero di telefono non valido")
        email = (form.get("email") or "").strip()
        if email and (len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email)):
            raise ValidationError("email", "Indirizzo email non valido")
        return cls(cf, tessera, ricetta, telefono, email)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)

    def masked(self):
        return {"codice_fiscale": _mask(self.codice_fiscale), "tessera": _mask(self.tessera, 2),
                "ricetta": _mask(self.ricetta), "telefono": _mask(self.telefono, 3), "email": "impostata" if self.email else ""}


def _parse_date(form, name, required=False):
    raw = (form.get(name) or "").strip()
    if not raw:
        if required:
            raise ValidationError(name, "Data obbligatoria")
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(name, "Data non valida") from None


def _parse_time(form, name):
    raw = (form.get(name) or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"\d{2}:\d{2}", raw):
        raise ValidationError(name, "Orario non valido (HH:MM)")
    try:
        datetime.strptime(raw, "%H:%M")
    except ValueError:
        raise ValidationError(name, "Orario non valido (HH:MM)") from None
    return raw


def _parse_int(form, name, low, high, default=None):
    raw = (form.get(name) or "").strip()
    if not raw and default is not None:
        return default
    if not re.fullmatch(r"\d{1,6}", raw) or not low <= int(raw) <= high:
        raise ValidationError(name, f"Valore tra {low} e {high}")
    return int(raw)


def _as_list(value):
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


@dataclass(frozen=True)
class SearchSettings:
    """Non-secret search options (stored as JSON)."""
    label: str
    location_mode: str
    province: list = field(default_factory=list)
    home_comune: str = ""
    max_km: int = 0
    facilities: list = field(default_factory=list)
    start_date: str = ""          # ISO yyyy-mm-dd
    end_date: str = ""
    weekdays: list = field(default_factory=list)
    time_from: str = ""
    time_to: str = ""
    refresh_seconds: int = 300
    dry_run: bool = True
    visita_controllo: object = None
    telegram_timeout: int = 15
    continua_dopo_prenotazione: bool = False

    @classmethod
    def from_form(cls, form, today=None):
        today = today or date.today()
        label = (form.get("label") or "").strip() or "Ricerca"
        if len(label) > 60:
            raise ValidationError("label", "Nome troppo lungo (max 60 caratteri)")

        try:
            mode = LocationMode(form.get("location_mode") or "provinces")
        except ValueError:
            raise ValidationError("location_mode", "Modalità non valida") from None

        province, home, max_km, facilities = [], "", 0, []
        if mode is LocationMode.PROVINCES:
            province = [p for p in _as_list(form.get("province")) if p]
            if not province or any(p not in PROVINCES for p in province):
                raise ValidationError("province", "Scegli almeno una provincia valida")
        elif mode is LocationMode.RADIUS:
            comune = ComuniIndex.load().find(form.get("home_comune"))
            if comune is None:
                raise ValidationError("home_comune", "Comune non trovato in Lombardia")
            home = comune.name
            max_km = _parse_int(form, "max_km", 1, MAX_KM)
        else:
            for line in (form.get("facilities") or "").splitlines():
                if not line.strip():
                    continue
                name, _, prov = line.partition("|")
                name, prov = name.strip(), prov.strip().upper()
                if not name or len(name) > 80 or (prov and prov not in PROVINCES):
                    raise ValidationError("facilities", f"Struttura non valida: {line.strip()[:40]}")
                facilities.append({"name": name, "province": prov})
            if not facilities or len(facilities) > MAX_FACILITIES:
                raise ValidationError("facilities", f"Indica da 1 a {MAX_FACILITIES} strutture (una per riga: nome | provincia)")

        start = _parse_date(form, "start_date")
        end = _parse_date(form, "end_date")
        if end and end < today:
            raise ValidationError("end_date", "La data di fine è già passata")
        if start and end and end < start:
            raise ValidationError("end_date", "La data di fine precede quella di inizio")

        weekdays = []
        for raw in _as_list(form.get("weekdays")):
            if not re.fullmatch(r"[0-6]", str(raw)):
                raise ValidationError("weekdays", "Giorno non valido")
            weekdays.append(int(raw))
        time_from, time_to = _parse_time(form, "time_from"), _parse_time(form, "time_to")
        if time_from and time_to and time_to < time_from:
            raise ValidationError("time_to", "L'orario di fine precede quello di inizio")

        controllo = {"si": True, "no": False}.get((form.get("visita_controllo") or "").strip().lower())
        return cls(label=label, location_mode=mode.value, province=province, home_comune=home, max_km=max_km,
                   facilities=facilities, start_date=start.isoformat() if start else "", end_date=end.isoformat() if end else "",
                   weekdays=sorted(set(weekdays)), time_from=time_from or "", time_to=time_to or "",
                   refresh_seconds=_parse_int(form, "refresh_seconds", MIN_REFRESH_SECONDS, MAX_REFRESH_SECONDS, 300),
                   dry_run=form.get("dry_run") in ("on", "true", "1", True),
                   visita_controllo=controllo,
                   telegram_timeout=_parse_int(form, "telegram_timeout", 1, 120, 15),
                   continua_dopo_prenotazione=form.get("continua_dopo_prenotazione") in ("on", "true", "1", True))

    def to_json(self):
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        return cls(**json.loads(text))

    def to_preferences(self, secrets):
        def italian(iso):
            return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y") if iso else ""
        return SearchPreferences(
            self.province, italian(self.start_date), italian(self.end_date), self.refresh_seconds, self.dry_run,
            telefono=secrets.telefono, email=secrets.email, visita_controllo=self.visita_controllo,
            telegram_timeout_minuti=self.telegram_timeout, continua_dopo_prenotazione=self.continua_dopo_prenotazione,
            location_mode=self.location_mode, home_comune=self.home_comune, max_km=self.max_km or None,
            facilities=self.facilities, weekdays=set(self.weekdays) if self.weekdays else None,
            time_from=self.time_from or None, time_to=self.time_to or None)
