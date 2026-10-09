from datetime import datetime
import hashlib
import re

class Patient:
    def __init__(self, codice_fiscale: str, tessera_sanitaria: str):
        self.codice_fiscale = codice_fiscale
        self.tessera_sanitaria = tessera_sanitaria


class Prescription:
    def __init__(self, prescription_n: str, patient: Patient):
        # Initialize the prescription with the code
        self.prescription_n = prescription_n
        self.codice_fiscale = patient.codice_fiscale
        self.tessera_sanitaria = patient.tessera_sanitaria


class Appointment:
    def __init__(self, date: str, address: str, prescription: Prescription):
        # Initialize the appointment with date, address and prescription
        self.date = date
        self.address = address
        self.prescription = prescription

    def change_app(self, new_date: str, new_address: str):
        self.date = new_date
        self.address = new_address

    def get_datetime(self):
        return datetime.strptime(self.date, "%d/%m/%Y - %H:%M")


def _normalize(text):
    # Collapse whitespace and ignore case so that cosmetic DOM differences don't create "new" slots
    return re.sub(r"\s+", " ", (text or "")).strip().upper()


class Slot:
    """A single availability shown in the results list.

    Two slots are the same appointment only if date, time, location and province all match:
    a different hour, day or structure is a different appointment.
    """
    def __init__(self, when: datetime, azienda: str, sede: str, comune: str, provincia: str, index=None):
        self.when = when
        self.azienda = azienda or ""
        self.sede = sede or ""
        self.comune = comune or ""
        self.provincia = provincia or ""
        self.index = index  # position in the currently displayed results page (not part of the identity)

    @property
    def date_str(self):
        return self.when.strftime("%d/%m/%Y")

    @property
    def time_str(self):
        return self.when.strftime("%H:%M")

    @property
    def appointment_date_str(self):
        # Same format used by Appointment.date
        return self.when.strftime("%d/%m/%Y - %H:%M")

    @property
    def address(self):
        return ", ".join(p for p in [self.sede, self.comune, self.azienda] if p)

    @property
    def slot_id(self):
        key = "|".join([self.date_str, self.time_str, _normalize(self.azienda), _normalize(self.sede),
                        _normalize(self.comune), _normalize(self.provincia)])
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    def to_dict(self):
        return {"when": self.when.strftime("%d/%m/%Y %H:%M"), "azienda": self.azienda, "sede": self.sede,
                "comune": self.comune, "provincia": self.provincia}

    @classmethod
    def from_dict(cls, data):
        return cls(datetime.strptime(data["when"], "%d/%m/%Y %H:%M"), data.get("azienda"), data.get("sede"),
                   data.get("comune"), data.get("provincia"))

    def describe(self):
        lines = [f"📅 Data: {self.date_str}", f"🕒 Ora: {self.time_str}"]
        if self.sede:
            lines.append(f"🏥 Struttura: {self.sede}")
        if self.azienda:
            lines.append(f"🏛 Azienda: {self.azienda}")
        if self.comune:
            lines.append(f"📍 Comune: {self.comune}")
        lines.append(f"🗺 Provincia: {self.provincia}")
        return "\n".join(lines)


class SearchPreferences:
    def __init__(self, province, start_date, end_date, refresh_frequency, dry_run=True, telegram_token="", telegram_chat_id="",
                 telefono="", email="", visita_controllo=None, telegram_timeout_minuti=15, continua_dopo_prenotazione=False):
        self.province = province
        self.start_date = start_date
        self.end_date = end_date
        self.refresh_frequency = refresh_frequency
        self.dry_run = dry_run
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        # Only needed for a first-time booking (the portal asks for contacts and the "controllo/follow-up" flag)
        self.telefono = telefono
        self.email = email
        self.visita_controllo = visita_controllo
        self.telegram_timeout_minuti = telegram_timeout_minuti
        self.continua_dopo_prenotazione = continua_dopo_prenotazione

    def get_start_date_input(self):
        return ''.join(filter(str.isdigit, self.start_date))

    def get_start_date_datetime(self):
        return datetime.strptime(self.start_date, "%d/%m/%Y")

    def get_end_date_datetime(self):
        return datetime.strptime(self.end_date, "%d/%m/%Y")

    def is_in_date_window(self, when: datetime):
        # Empty dates mean "no limit"
        if self.start_date and when.date() < self.get_start_date_datetime().date():
            return False
        if self.end_date and when.date() > self.get_end_date_datetime().date():
            return False
        return True
