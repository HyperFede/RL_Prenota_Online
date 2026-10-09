import hashlib
import json
import os
import time

from rlprenota.core.models import Slot

DEFAULT_STATE_FILE = "stato_ricerca.json"

# User answers
ACCEPT = "accept"
REJECT = "reject"

# Proposal statuses
PENDING = "pending"      # sent to the user, waiting for an answer
EXPIRED = "expired"      # no answer in time: not proposed again in this run, but the user can still answer later
ACCEPTED = "accepted"    # accepted after it expired: book it as soon as it shows up again
REJECTED = "rejected"
BOOKED = "booked"


class MemoryDecisionStore:
    """Remembers the user's answers for each appointment slot (in memory).

    Subclasses persist the changes through the _saved_* hooks: a JSON file for the command line,
    the master database on the NAS.
    """

    def __init__(self, discarded=None, proposals=None):
        self.discarded = discarded if discarded is not None else {}
        self.proposals = proposals if proposals is not None else {}
        self._start_new_run()

    def _start_new_run(self):
        # Answers that expired in a previous run can be proposed again after a restart
        for proposal in self.proposals.values():
            if proposal.get("status") in (PENDING, EXPIRED):
                proposal["status"] = EXPIRED
                proposal["session_expired"] = False

    # persistence hooks
    def _saved_proposal(self, slot_id):
        pass

    def _saved_discard(self, slot_id):
        pass

    def _removed_discard(self, slot_id):
        pass

    # --- queries ---

    def is_discarded(self, slot: Slot):
        return slot.slot_id in self.discarded

    def status(self, slot: Slot):
        proposal = self.proposals.get(slot.slot_id)
        return proposal.get("status") if proposal else None

    def should_propose(self, slot: Slot):
        """True if the user has to be asked about this slot."""
        if self.is_discarded(slot):
            return False
        proposal = self.proposals.get(slot.slot_id)
        if not proposal:
            return True
        if proposal.get("status") == EXPIRED:
            # Not answered in this run: don't spam the same slot again until the user answers or restarts
            return not proposal.get("session_expired", False)
        # PENDING/BOOKED need no question, ACCEPTED is booked directly (see is_pre_accepted)
        return False

    def is_pre_accepted(self, slot: Slot):
        """The user accepted this slot after its proposal had expired: book it without asking again."""
        return not self.is_discarded(slot) and self.status(slot) == ACCEPTED

    def slot_id_for_message(self, message_id):
        for slot_id, proposal in self.proposals.items():
            if message_id is not None and proposal.get("message_id") == message_id:
                return slot_id
        return None

    def slot_for_id(self, slot_id):
        proposal = self.proposals.get(slot_id)
        return Slot.from_dict(proposal["slot"]) if proposal else None

    # --- updates ---

    def record_proposal(self, slot: Slot, message_id=None):
        self.proposals[slot.slot_id] = {"slot": slot.to_dict(), "message_id": message_id,
                                        "status": PENDING, "ts": int(time.time())}
        self._saved_proposal(slot.slot_id)

    def set_status(self, slot_id, status):
        proposal = self.proposals.get(slot_id)
        if not proposal:
            return
        proposal["status"] = status
        proposal["session_expired"] = status == EXPIRED
        self._saved_proposal(slot_id)

    def discard(self, slot: Slot):
        self.discarded[slot.slot_id] = slot.to_dict()
        self._saved_discard(slot.slot_id)
        if slot.slot_id in self.proposals:
            self.set_status(slot.slot_id, REJECTED)

    def discard_id(self, slot_id):
        slot = self.slot_for_id(slot_id)
        if slot:
            self.discard(slot)

    def undiscard(self, slot_id):
        if self.discarded.pop(slot_id, None) is not None:
            self._removed_discard(slot_id)


class DecisionStore(MemoryDecisionStore):
    """Command-line store: saved in a JSON file, so a rejected appointment is never proposed again,
    not even after a restart. Data is kept per prescription (by hash, the code itself is not written).
    """

    def __init__(self, prescription_n, path=DEFAULT_STATE_FILE):
        self.path = path
        self.key = hashlib.sha256(prescription_n.strip().upper().encode("utf-8")).hexdigest()[:16]
        self._data = {"version": 1, "prescriptions": {}}
        self._load()
        bucket = self._data["prescriptions"].setdefault(self.key, {})
        super().__init__(bucket.setdefault("discarded", {}), bucket.setdefault("proposals", {}))
        self._save()

    def _load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("prescriptions"), dict):
                self._data = data
        except (OSError, ValueError) as e:
            print(f"Attenzione: impossibile leggere {self.path} ({e}). Riparto da uno stato vuoto.")

    def _save(self):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def _saved_proposal(self, slot_id):
        self._save()

    def _saved_discard(self, slot_id):
        self._save()

    def _removed_discard(self, slot_id):
        self._save()
