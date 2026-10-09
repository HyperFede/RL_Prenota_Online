"""Early warning for portal changes, without a browser or a login.

Every 30 minutes: load the portal's home page, find its JavaScript bundle and check that every selector
and label our automation depends on is still in it. A missing marker pauses all searches and alerts the
admin; a new bundle that still has every marker is only a heads-up; an unreachable portal is alerted
after 3 consecutive failures (searches already retry on their own).
"""
import hashlib
import http.cookiejar
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

PORTAL_URL = "https://prenotasalute.regione.lombardia.it/prenotaonline/"
UNREACHABLE_ALERT_AFTER = 3

# Strings from the portal's templates that rlprenota.core.portal relies on (minified attribute syntax)
REQUIRED_MARKERS = [
    "id=cf ", "id=crs ", "id=codice ", "btn-riprenota", "dati-appuntamento-summary", "id=provincia ",
    "verifica_conferma_appuntamenti", "verificaPrenotazioneCtrl.conferma", "presaVisioneNote",
    "prenotaCompletaDatiCtrl.conferma", "consensoPrenotazione", "modifica-ricerca-info-testata",
    "doveQuandoModalCtrl.aggiorna", "appuntamento-field-title", "Presentarsi in", "lista-appuntamenti",
    "btn btn-primary submit", "id=telefono ", "Appuntamento prenotato",
]


class CanaryFetchError(Exception):
    pass


@dataclass
class CanaryResult:
    status: str     # ok | bundle_changed | markers_missing | unreachable
    detail: str = ""


def default_fetch(url, timeout=60):
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    # The portal needs cookies (it redirects in a loop without them)
    opener = default_fetch.opener = getattr(default_fetch, "opener", None) or urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), urllib.request.HTTPSHandler(context=context))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (RL Prenota Online canary)")]
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as e:
        raise CanaryFetchError(str(e)) from None


class Canary:
    def __init__(self, db, pauser, alerts, fetch=default_fetch, clock=time.time):
        self.db, self.pauser, self.alerts, self.fetch, self.clock = db, pauser, alerts, fetch, clock

    def _save(self, result):
        self.db.set_setting("canary_status", result.status)
        self.db.set_setting("canary_detail", result.detail[:500])
        self.db.set_setting("canary_at", self.clock())
        return result

    def run(self):
        try:
            html = self.fetch(PORTAL_URL).decode("utf-8", "replace")
            match = re.search(r'src=["\']?(scripts/app-[0-9a-f]+\.js)', html)
            if not match:
                raise CanaryFetchError("la pagina non contiene l'applicazione (manutenzione?)")
            bundle = self.fetch(PORTAL_URL + match.group(1))
        except CanaryFetchError as e:
            failures = int(self.db.get_setting("canary_failures", 0)) + 1
            self.db.set_setting("canary_failures", failures)
            if failures == UNREACHABLE_ALERT_AFTER:
                self.alerts.alert_admin("canary_unreachable",
                                        f"⚠️ Il portale non risponde da {failures} controlli consecutivi ({e}). "
                                        "Le ricerche continuano a riprovare da sole.")
            return self._save(CanaryResult("unreachable", str(e)))

        self.db.set_setting("canary_failures", 0)
        text = bundle.decode("utf-8", "replace")
        missing = [m.strip() for m in REQUIRED_MARKERS if m not in text]
        if missing:
            reason = f"Il portale è cambiato: non trovo {', '.join(missing)}"
            self.pauser.pause_global(reason)
            self.alerts.alert_admin("canary_markers", f"🛑 {reason}.\nTutte le ricerche sono sospese: va aggiornato il codice.")
            return self._save(CanaryResult("markers_missing", ", ".join(missing)))

        digest = hashlib.sha256(bundle).hexdigest()
        known = self.db.get_setting("canary_bundle_hash", "")
        self.db.set_setting("canary_bundle_hash", digest)
        if known and known != digest:
            self.alerts.alert_admin("canary_bundle", "ℹ️ Il portale ha aggiornato la sua applicazione. Tutti gli elementi usati "
                                                     "sono ancora presenti: le ricerche continuano. Tieni d'occhio i prossimi esiti.")
            return self._save(CanaryResult("bundle_changed", digest[:16]))
        return self._save(CanaryResult("ok", digest[:16]))
