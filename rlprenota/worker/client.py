"""Signed HTTP client from a worker to the master's internal API."""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from rlprenota.signing import sign


class MasterUnavailable(Exception):
    pass


class LeaseLost(Exception):
    pass


class MasterClient:
    def __init__(self, base_url, secret, worker_name, clock=time.time):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise ValueError("URL del master non valido")
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.worker_name = worker_name
        self.clock = clock

    def _post(self, path, data=None, timeout=40):
        body = json.dumps(data or {}).encode()
        timestamp = f"{self.clock():.3f}"
        headers = {"Content-Type": "application/json", "X-RLP-Worker": self.worker_name, "X-RLP-Time": timestamp,
                   "X-RLP-Signature": sign(self.secret, "POST", path, timestamp, body)}
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method="POST")
        try:
            # Internal Docker network only; the scheme is validated in __init__
            with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 409:
                raise LeaseLost() from None
            raise MasterUnavailable(f"HTTP {e.code}") from None
        except (OSError, ValueError) as e:
            raise MasterUnavailable(str(e)) from None

    def lease(self):
        return self._post("/api/lease", {"worker": self.worker_name})

    def heartbeat(self, lease_id):
        self._post(f"/api/lease/{lease_id}/heartbeat")

    def permit(self, lease_id):
        return float(self._post(f"/api/lease/{lease_id}/permit")["wait"])

    def propose(self, lease_id, slot, current_appointment, dry_run, timeout_minutes):
        return self._post(f"/api/lease/{lease_id}/propose", {"slot": slot.to_dict(), "current_appointment": current_appointment,
                                                              "dry_run": dry_run, "timeout_minutes": timeout_minutes})["message_id"]

    def decision(self, lease_id, slot_id, wait):
        return self._post(f"/api/lease/{lease_id}/decision", {"slot_id": slot_id, "wait": wait}, timeout=wait + 15)["decision"]

    def expire(self, lease_id, slot_id):
        self._post(f"/api/lease/{lease_id}/expire", {"slot_id": slot_id})

    def update(self, lease_id, slot_id, footer, keep_buttons=False):
        self._post(f"/api/lease/{lease_id}/update", {"slot_id": slot_id, "footer": footer, "keep_buttons": keep_buttons})

    def notify(self, lease_id, text):
        self._post(f"/api/lease/{lease_id}/notify", {"text": text})

    def store(self, lease_id, op, **fields):
        self._post(f"/api/lease/{lease_id}/store", {"op": op, **fields})

    def complete(self, lease_id, **result):
        self._post(f"/api/lease/{lease_id}/complete", result)
