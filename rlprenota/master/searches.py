"""Searches of each user: every query is scoped by user_id, secrets are encrypted and wiped at the end."""
import json
import time
from datetime import date, datetime

from rlprenota.core.decisions import MemoryDecisionStore
from rlprenota.core.models import ANTICIPA, Slot
from rlprenota.master.crypto import new_token
from rlprenota.master.validation import SearchSecrets, SearchSettings

RUNNING = ("active", "waiting")          # statuses that use worker time and count against the limits (see _check_limits)
ENDED = ("booked", "expired")
# When two records of one appointment are merged, the stronger decision wins
STATUS_STRENGTH = {"pending": 1, "expired": 2, "rejected": 3, "accepted": 4, "booked": 5}
DEFAULT_MAX_ACTIVE_PER_USER = 3
DEFAULT_MAX_ACTIVE_TOTAL = 10
IDLE_EXPIRY_SECONDS = 60 * 86400
CONTINUE_WINDOW_SECONDS = 48 * 3600     # after a search ends, its owner or the admin may continue it this long


def appointment_datetime(text):
    """The booked appointment as stored ("05/11/2026 - 08:30"), or None if unknown."""
    for fmt in ("%d/%m/%Y - %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt)
        except ValueError:
            continue
    return None


class NotFound(Exception):
    """Missing, or belonging to someone else: deliberately indistinguishable."""


class LimitReached(Exception):
    pass


class SearchView:
    """What the web pages may show (secrets only masked)."""

    def __init__(self, row, crypto):
        self.id = row["id"]
        self.status = row["status"]
        self.settings = SearchSettings.from_json(row["settings_json"])
        self.created_at = row["created_at"]
        self.updated_at = row["updated_at"]
        self.next_run_at = row["next_run_at"]
        self.last_run_at = row["last_run_at"]
        self.last_result = row["last_result"]
        self.current_appointment = row["current_appointment"]
        self.consecutive_failures = row["consecutive_failures"]
        self.effective_interval = row["effective_interval"]
        self.has_secrets = row["secrets_enc"] is not None
        self.ended = self.status in ENDED
        self.continue_until = row["ended_at"] + CONTINUE_WINDOW_SECONDS if row["ended_at"] else None
        self.masked = (SearchSecrets.from_dict(crypto.decrypt_json(row["secrets_enc"], f"search:{row['id']}")).masked()
                       if self.has_secrets else {})


class SearchRepo:
    def __init__(self, db, crypto, clock=time.time):
        self.db = db
        self.crypto = crypto
        self.clock = clock

    def _limits(self):
        return (int(self.db.get_setting("max_active_per_user", DEFAULT_MAX_ACTIVE_PER_USER)),
                int(self.db.get_setting("max_active_total", DEFAULT_MAX_ACTIVE_TOTAL)))

    def _check_limits(self, conn, user_id):
        per_user, total = self._limits()
        # RUNNING statuses, written literally so the SQL stays fully static
        mine = conn.execute("SELECT COUNT(*) FROM searches WHERE user_id = ? AND status IN ('active', 'waiting')",
                            (user_id,)).fetchone()[0]
        if mine >= per_user:
            raise LimitReached(f"Puoi avere al massimo {per_user} ricerche attive: mettine in pausa una")
        everyone = conn.execute("SELECT COUNT(*) FROM searches WHERE status IN ('active', 'waiting')").fetchone()[0]
        if everyone >= total:
            raise LimitReached("Il servizio ha raggiunto il numero massimo di ricerche attive: riprova più tardi")

    @staticmethod
    def _revoke_lease(conn, search_id):
        """A worker may be running this search with the old settings: tell it to drop the run."""
        conn.execute("UPDATE leases SET revoked = 1 WHERE search_id = ?", (search_id,))

    def _owned(self, conn, user_id, search_id):
        row = conn.execute("SELECT * FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id)).fetchone()
        if row is None:
            raise NotFound()
        return row

    # --- user actions ---

    def create(self, user_id, settings, secrets):
        search_id, now = new_token(9), self.clock()
        with self.db.tx() as conn:
            self._check_limits(conn, user_id)
            conn.execute("""INSERT INTO searches(id, user_id, status, settings_json, secrets_enc, created_at, updated_at, next_run_at)
                            VALUES (?, ?, 'active', ?, ?, ?, ?, ?)""",
                         (search_id, user_id, settings.to_json(),
                          self.crypto.encrypt_json(secrets.to_dict(), f"search:{search_id}"), now, now, now))
            self.db.audit("search_created", user_id, search_id, conn=conn)
        return search_id

    def list_for_user(self, user_id):
        rows = self.db.query("SELECT * FROM searches WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
        return [self._view(r) for r in rows]

    def get(self, user_id, search_id):
        row = self.db.query_one("SELECT * FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        return self._view(row) if row else None

    def _view(self, row):
        view = SearchView(row, self.crypto)
        view.blocked = self.continuable(row) if view.ended else None   # "" = the owner may continue it
        return view

    def update(self, user_id, search_id, settings, secrets=None):
        with self.db.tx() as conn:
            if self._owned(conn, user_id, search_id)["status"] in ENDED:
                raise LimitReached("Questa ricerca è conclusa: creane una nuova")
            # Run again right away with the new settings ('waiting' was about a proposal of the dropped run)
            conn.execute("""UPDATE searches SET settings_json = ?, updated_at = ?, next_run_at = ?,
                            status = CASE status WHEN 'waiting' THEN 'active' ELSE status END WHERE id = ?""",
                         (settings.to_json(), self.clock(), self.clock(), search_id))
            self._revoke_lease(conn, search_id)
            if secrets is not None:
                conn.execute("UPDATE searches SET secrets_enc = ? WHERE id = ?",
                             (self.crypto.encrypt_json(secrets.to_dict(), f"search:{search_id}"), search_id))
            self.db.audit("search_updated", user_id, f"{search_id} secrets={'yes' if secrets else 'no'}", conn=conn)

    def pause(self, user_id, search_id):
        with self.db.tx() as conn:
            if self._owned(conn, user_id, search_id)["status"] in ENDED:
                return
            conn.execute("UPDATE searches SET status = 'paused', updated_at = ? WHERE id = ?", (self.clock(), search_id))
            self._revoke_lease(conn, search_id)
            self.db.audit("search_paused", user_id, search_id, conn=conn)

    def resume(self, user_id, search_id):
        with self.db.tx() as conn:
            row = self._owned(conn, user_id, search_id)
            if row["status"] in RUNNING:
                return
            if row["status"] in ENDED or row["secrets_enc"] is None:
                raise LimitReached("Questa ricerca è conclusa: creane una nuova")
            self._check_limits(conn, user_id)
            conn.execute("""UPDATE searches SET status = 'active', consecutive_failures = 0, updated_at = ?, next_run_at = ?
                            WHERE id = ?""", (self.clock(), self.clock(), search_id))
            self.db.audit("search_resumed", user_id, search_id, conn=conn)

    def delete(self, user_id, search_id):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
            conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))  # cascades to proposals/discarded/leases
            self.db.audit("search_deleted", user_id, search_id, conn=conn)

    def discarded(self, user_id, search_id):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
        rows = self.db.query("SELECT slot_id, slot_json, ts FROM discarded WHERE search_id = ? ORDER BY ts DESC", (search_id,))
        return [{"slot_id": r["slot_id"], "slot": json.loads(r["slot_json"]), "ts": r["ts"]} for r in rows]

    def undiscard(self, user_id, search_id, slot_id):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
            conn.execute("DELETE FROM discarded WHERE search_id = ? AND slot_id = ?", (search_id, slot_id))
            conn.execute("DELETE FROM proposals WHERE search_id = ? AND slot_id = ?", (search_id, slot_id))
            self.db.audit("slot_undiscarded", user_id, search_id, conn=conn)

    def proposals(self, user_id, search_id, limit=20):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
        rows = self.db.query("SELECT slot_json, status, ts FROM proposals WHERE search_id = ? ORDER BY ts DESC LIMIT ?",
                             (search_id, limit))
        return [{"slot": json.loads(r["slot_json"]), "status": r["status"], "ts": r["ts"]} for r in rows]

    # --- service side (scheduler / workers) ---

    def secrets_for(self, search_id):
        row = self.db.query_one("SELECT secrets_enc FROM searches WHERE id = ?", (search_id,))
        if not row or row["secrets_enc"] is None:
            return None
        return SearchSecrets.from_dict(self.crypto.decrypt_json(row["secrets_enc"], f"search:{search_id}"))

    def _appointment_ahead(self, current_appointment):
        when = appointment_datetime(current_appointment)
        return when is not None and when > datetime.fromtimestamp(self.clock())

    def finish(self, search_id, status, result="", keep_for_continuation=True):
        """End a search. The personal data needed to log in is wiped, unless the booked appointment is still
        ahead: then it is kept for CONTINUE_WINDOW_SECONDS, so the search can be continued (see reopen), and
        expire_finished wipes it when that window closes."""
        if status not in ENDED:
            raise ValueError(status)
        with self.db.tx() as conn:
            row = conn.execute("SELECT current_appointment FROM searches WHERE id = ?", (search_id,)).fetchone()
            keep = keep_for_continuation and row is not None and self._appointment_ahead(row["current_appointment"])
            now = self.clock()
            conn.execute("""UPDATE searches SET status = ?, last_result = ?, updated_at = ?, ended_at = ? WHERE id = ?""",
                         (status, result[:300], now, now, search_id))
            if not keep:
                conn.execute("UPDATE searches SET secrets_enc = NULL WHERE id = ?", (search_id,))
            conn.execute("DELETE FROM leases WHERE search_id = ?", (search_id,))
            self.db.audit(f"search_{status}", None, search_id, conn=conn)

    def expire_finished(self, today=None):
        """Searches past their end date, or untouched for 60 days, are closed; ended searches lose the secrets
        they kept for a possible continuation once the 48 h window closes (or the appointment passes)."""
        today = (today or date.today()).isoformat()
        idle_limit = self.clock() - IDLE_EXPIRY_SECONDS
        expired = []
        for row in self.db.query("SELECT id, settings_json, updated_at, status FROM searches WHERE status NOT IN (?, ?)", ENDED):
            end = json.loads(row["settings_json"]).get("end_date")
            past_end = bool(end and end < today)
            if past_end or row["updated_at"] < idle_limit:
                self.finish(row["id"], "expired", "Ricerca conclusa: data di fine superata o inattiva da 60 giorni",
                            keep_for_continuation=not past_end)
                expired.append(row["id"])
        for row in self.db.query("""SELECT id, current_appointment, ended_at FROM searches
                                    WHERE status IN (?, ?) AND secrets_enc IS NOT NULL""", ENDED):
            if not (self._in_window(row) and self._appointment_ahead(row["current_appointment"])):
                with self.db.tx() as conn:
                    conn.execute("UPDATE searches SET secrets_enc = NULL WHERE id = ?", (row["id"],))
                    self.db.audit("search_secrets_wiped", None, row["id"], conn=conn)
        return expired

    def _in_window(self, row):
        return row["ended_at"] is not None and self.clock() < row["ended_at"] + CONTINUE_WINDOW_SECONDS

    def continuable(self, row, today=None):
        """Why a closed search can't be continued, or "" if it can (row from the searches table)."""
        if row["status"] not in ENDED:
            return "La ricerca non è conclusa"
        if SearchSettings.from_json(row["settings_json"]).direzione != ANTICIPA:
            return "Una ricerca che può posticipare si conclude con la prenotazione"
        if not self._appointment_ahead(row["current_appointment"]):
            return "La data della prenotazione è già passata (o non è nota)"
        if not self._in_window(row):
            return "Sono passate più di 48 ore dalla conclusione: i dati di accesso sono stati cancellati"
        if row["secrets_enc"] is None:
            return "I dati di accesso sono già stati cancellati: serve una nuova ricerca"
        end = json.loads(row["settings_json"]).get("end_date")
        if end and end < (today or date.today()).isoformat():
            return "La data di fine della ricerca è già passata"
        return ""

    def reopen(self, search_id, actor_id, today=None, owner_id=None):
        """Continue a closed search (booked or expired) while its booked appointment is still ahead: by the
        admin, or by its owner (owner_id). The portal then finds the booking and looks for an earlier slot."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
            if row is None or (owner_id is not None and row["user_id"] != owner_id):
                raise NotFound()
            reason = self.continuable(row, today)
            if reason:
                raise LimitReached(reason)
            self._check_limits(conn, row["user_id"])
            now = self.clock()
            who = "dall'amministratore" if owner_id is None else "dal proprietario"
            conn.execute("""UPDATE searches SET status = 'active', consecutive_failures = 0, updated_at = ?, next_run_at = ?,
                            ended_at = NULL, last_result = ? WHERE id = ?""", (now, now, f"Ricerca ripresa {who}", search_id))
            self.db.audit("search_reopened", actor_id, search_id, conn=conn)

    def repair_provinces(self, real_province):
        """One-off repair: slots saved with the province being searched instead of their own (read while the
        page still showed the previous province's results). For the searches still running: a slot whose
        correctly labelled twin exists is deleted (keeping the stronger decision), otherwise it is relabelled,
        so the user's decision keeps applying to that appointment. Returns {"deleted": n, "relabelled": n}."""
        counts = {"deleted": 0, "relabelled": 0}
        with self.db.tx() as conn:
            open_ids = [r["id"] for r in conn.execute("SELECT id FROM searches WHERE status NOT IN (?, ?)", ENDED)]
            for search_id in open_ids:
                for table in ("proposals", "discarded"):
                    rows = conn.execute(f"SELECT * FROM {table} WHERE search_id = ?", (search_id,)).fetchall()
                    for row in rows:
                        data = json.loads(row["slot_json"])
                        right = real_province(data.get("comune"), data.get("provincia"))
                        if right == data.get("provincia"):
                            continue
                        fixed = Slot.from_dict({**data, "provincia": right})
                        twin = conn.execute(f"SELECT * FROM {table} WHERE search_id = ? AND slot_id = ?",
                                            (search_id, fixed.slot_id)).fetchone()
                        if twin is None:
                            conn.execute(f"UPDATE {table} SET slot_id = ?, slot_json = ? WHERE search_id = ? AND slot_id = ?",
                                         (fixed.slot_id, json.dumps(fixed.to_dict()), search_id, row["slot_id"]))
                            counts["relabelled"] += 1
                            continue
                        if table == "proposals" and STATUS_STRENGTH.get(row["status"], 0) > STATUS_STRENGTH.get(twin["status"], 0):
                            conn.execute("UPDATE proposals SET status = ? WHERE search_id = ? AND slot_id = ?",
                                         (row["status"], search_id, fixed.slot_id))
                        conn.execute(f"DELETE FROM {table} WHERE search_id = ? AND slot_id = ?",
                                     (search_id, row["slot_id"]))
                        counts["deleted"] += 1
            self.db.audit("slots_repaired", None, json.dumps(counts), conn=conn)
        return counts

    def decisions(self, search_id):
        return DbDecisionStore(self.db, search_id)


class DbDecisionStore(MemoryDecisionStore):
    """Decision store of one search, written through to the master database."""

    def __init__(self, db, search_id):
        self.db = db
        self.search_id = search_id
        discarded = {r["slot_id"]: json.loads(r["slot_json"])
                     for r in db.query("SELECT slot_id, slot_json FROM discarded WHERE search_id = ?", (search_id,))}
        proposals = {r["slot_id"]: {"slot": json.loads(r["slot_json"]), "message_id": r["message_id"], "status": r["status"],
                                    "session_expired": bool(r["session_expired"]), "ts": r["ts"]}
                     for r in db.query("SELECT * FROM proposals WHERE search_id = ?", (search_id,))}
        super().__init__(discarded, proposals)

    def _saved_proposal(self, slot_id):
        p = self.proposals[slot_id]
        with self.db.tx() as conn:
            conn.execute("""INSERT INTO proposals(search_id, slot_id, slot_json, message_id, status, session_expired, ts)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(search_id, slot_id) DO UPDATE SET slot_json = excluded.slot_json,
                                message_id = excluded.message_id, status = excluded.status,
                                session_expired = excluded.session_expired, ts = excluded.ts""",
                         (self.search_id, slot_id, json.dumps(p["slot"]), p.get("message_id"), p["status"],
                          int(p.get("session_expired", False)), p.get("ts", time.time())))

    def _saved_discard(self, slot_id):
        with self.db.tx() as conn:
            conn.execute("""INSERT INTO discarded(search_id, slot_id, slot_json, ts) VALUES (?, ?, ?, ?)
                            ON CONFLICT(search_id, slot_id) DO NOTHING""",
                         (self.search_id, slot_id, json.dumps(self.discarded[slot_id]), time.time()))

    def _removed_discard(self, slot_id):
        with self.db.tx() as conn:
            conn.execute("DELETE FROM discarded WHERE search_id = ? AND slot_id = ?", (self.search_id, slot_id))
