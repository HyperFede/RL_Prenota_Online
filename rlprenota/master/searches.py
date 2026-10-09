"""Searches of each user: every query is scoped by user_id, secrets are encrypted and wiped at the end."""
import json
import time
from datetime import date

from rlprenota.core.decisions import MemoryDecisionStore
from rlprenota.master.crypto import new_token
from rlprenota.master.validation import SearchSecrets, SearchSettings

RUNNING = ("active", "waiting")          # statuses that use worker time and count against the limits (see _check_limits)
ENDED = ("booked", "expired")
DEFAULT_MAX_ACTIVE_PER_USER = 3
DEFAULT_MAX_ACTIVE_TOTAL = 10
IDLE_EXPIRY_SECONDS = 60 * 86400


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
        return [SearchView(r, self.crypto) for r in rows]

    def get(self, user_id, search_id):
        row = self.db.query_one("SELECT * FROM searches WHERE id = ? AND user_id = ?", (search_id, user_id))
        return SearchView(row, self.crypto) if row else None

    def update(self, user_id, search_id, settings, secrets=None):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
            conn.execute("UPDATE searches SET settings_json = ?, updated_at = ?, next_run_at = ? WHERE id = ?",
                         (settings.to_json(), self.clock(), self.clock(), search_id))
            if secrets is not None:
                conn.execute("UPDATE searches SET secrets_enc = ? WHERE id = ?",
                             (self.crypto.encrypt_json(secrets.to_dict(), f"search:{search_id}"), search_id))
            self.db.audit("search_updated", user_id, f"{search_id} secrets={'yes' if secrets else 'no'}", conn=conn)

    def pause(self, user_id, search_id):
        with self.db.tx() as conn:
            self._owned(conn, user_id, search_id)
            conn.execute("UPDATE searches SET status = 'paused', updated_at = ? WHERE id = ?", (self.clock(), search_id))
            self.db.audit("search_paused", user_id, search_id, conn=conn)

    def resume(self, user_id, search_id):
        with self.db.tx() as conn:
            row = self._owned(conn, user_id, search_id)
            if row["status"] in RUNNING:
                return
            if row["secrets_enc"] is None:
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

    def finish(self, search_id, status, result=""):
        """End a search: the personal data needed to log in is wiped."""
        if status not in ENDED:
            raise ValueError(status)
        with self.db.tx() as conn:
            conn.execute("""UPDATE searches SET status = ?, secrets_enc = NULL, last_result = ?, updated_at = ? WHERE id = ?""",
                         (status, result[:300], self.clock(), search_id))
            conn.execute("DELETE FROM leases WHERE search_id = ?", (search_id,))
            self.db.audit(f"search_{status}", None, search_id, conn=conn)

    def expire_finished(self, today=None):
        """Searches past their end date, or untouched for 60 days, are closed and their secrets wiped."""
        today = (today or date.today()).isoformat()
        idle_limit = self.clock() - IDLE_EXPIRY_SECONDS
        expired = []
        for row in self.db.query("SELECT id, settings_json, updated_at, status FROM searches WHERE status NOT IN (?, ?)", ENDED):
            end = json.loads(row["settings_json"]).get("end_date")
            if (end and end < today) or row["updated_at"] < idle_limit:
                self.finish(row["id"], "expired", "Ricerca conclusa: data di fine superata o inattiva da 60 giorni")
                expired.append(row["id"])
        return expired

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
