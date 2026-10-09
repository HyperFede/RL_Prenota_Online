"""Invite-only accounts with passwordless Telegram login.

Flow:
1. The admin creates an invite: a one-time t.me/<bot>?start=<token> link (24 h).
2. Opening it links the account to the user's Telegram chat (chat id + username, stored encrypted/hashed).
3. Login: the user types their Telegram username; the bot asks "Approve login from <device>?" with a button
   and a 6-digit fallback code. The request is bound to the browser that started it (cookie), so a leaked
   request id is useless.
4. The device gets a 30-day rolling session (only its hash is stored); sensitive actions need a Telegram
   confirmation in the last 10 minutes.
"""
import hmac
import re
import secrets
import time

from rlprenota.master.crypto import hash_token, new_token

INVITE_TTL_SECONDS = 24 * 3600
LOGIN_TTL_SECONDS = 5 * 60
SESSION_IDLE_SECONDS = 30 * 86400
SESSION_MAX_SECONDS = 365 * 86400
RECENT_AUTH_SECONDS = 10 * 60
MAX_CODE_ATTEMPTS = 5
LOGIN_STARTS_PER_USER = 5      # per window
LOGIN_STARTS_PER_CLIENT = 20
RATE_WINDOW_SECONDS = 15 * 60


class AuthError(Exception):
    pass


class SessionInfo:
    def __init__(self, user_id, display_name, is_admin, csrf_token, auth_at, clock):
        self.user_id = user_id
        self.display_name = display_name
        self.is_admin = bool(is_admin)
        self.csrf_token = csrf_token
        self.auth_at = auth_at
        self._clock = clock

    def recently_authenticated(self):
        return self._clock() - self.auth_at <= RECENT_AUTH_SECONDS

    def check_csrf(self, token):
        return bool(token) and hmac.compare_digest(str(token), self.csrf_token)


class AuthService:
    def __init__(self, db, crypto, notifier, clock=time.time):
        self.db = db
        self.crypto = crypto
        self.notifier = notifier
        self.clock = clock

    # --- users and invites ---

    def create_invite(self, display_name, is_admin=False, user_id=None, login_name=None):
        """Returns the invite token (to put in t.me/<bot>?start=<token>).

        `login_name` is what the user types to log in (also works without a Telegram @username)."""
        token, now = new_token(18), self.clock()  # 24 chars: fits Telegram's 64-char start parameter
        with self.db.tx() as conn:
            if user_id is None:
                login_name = (login_name or display_name).strip().lower().replace(" ", "")
                if not re.fullmatch(r"[a-z0-9._]{3,32}", login_name):
                    raise AuthError("Nome di accesso non valido (3-32 tra lettere, cifre, punto e trattino basso)")
                login_hash = self.crypto.lookup_hash(login_name)
                if conn.execute("SELECT 1 FROM users WHERE login_hash = ? OR username_hash = ?", (login_hash, login_hash)).fetchone():
                    raise AuthError("Nome di accesso già usato")
                user_id = conn.execute("INSERT INTO users(display_name, is_admin, status, created_at) VALUES (?, ?, 'invited', ?)",
                                       (display_name.strip()[:60] or "Utente", int(is_admin), now)).lastrowid
                conn.execute("UPDATE users SET login_hash = ?, login_enc = ? WHERE id = ?",
                             (login_hash, self.crypto.encrypt(login_name, f"user:{user_id}"), user_id))
            conn.execute("INSERT INTO invites(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                         (hash_token(token), user_id, now + INVITE_TTL_SECONDS))
            self.db.audit("invite_created", user_id, conn=conn)
        return token

    def redeem_invite(self, token, chat_id, username):
        now = self.clock()
        chat_hash = self.crypto.lookup_hash(chat_id)
        username = (username or "").strip().lstrip("@")
        with self.db.tx() as conn:
            invite = conn.execute("SELECT * FROM invites WHERE token_hash = ?", (hash_token(token or ""),)).fetchone()
            if not invite or invite["used_at"] or invite["expires_at"] < now:
                raise AuthError("Invito non valido o scaduto")
            owner = conn.execute("SELECT id FROM users WHERE chat_hash = ?", (chat_hash,)).fetchone()
            if owner and owner["id"] != invite["user_id"]:
                raise AuthError("Questo account Telegram è già collegato a un altro utente")
            user_id = invite["user_id"]
            aad = f"user:{user_id}"
            conn.execute("""UPDATE users SET chat_hash = ?, chat_enc = ?, username_hash = ?, username_enc = ?, status = 'active'
                            WHERE id = ?""",
                         (chat_hash, self.crypto.encrypt(str(chat_id), aad),
                          self.crypto.lookup_hash(username) if username else None,
                          self.crypto.encrypt(username, aad) if username else None, user_id))
            conn.execute("UPDATE invites SET used_at = ? WHERE token_hash = ?", (now, invite["token_hash"]))
            self.db.audit("telegram_linked", user_id, conn=conn)
        return user_id

    def user_by_chat(self, chat_id):
        row = self.db.query_one("SELECT id FROM users WHERE chat_hash = ? AND status = 'active'", (self.crypto.lookup_hash(chat_id),))
        return row["id"] if row else None

    def chat_id(self, user_id):
        row = self.db.query_one("SELECT chat_enc FROM users WHERE id = ?", (user_id,))
        return int(self.crypto.decrypt(row["chat_enc"], f"user:{user_id}")) if row and row["chat_enc"] else None

    def login_name(self, user_id):
        row = self.db.query_one("SELECT login_enc FROM users WHERE id = ?", (user_id,))
        return self.crypto.decrypt(row["login_enc"], f"user:{user_id}") if row and row["login_enc"] else ""

    def username(self, user_id):
        row = self.db.query_one("SELECT username_enc FROM users WHERE id = ?", (user_id,))
        return self.crypto.decrypt(row["username_enc"], f"user:{user_id}") if row and row["username_enc"] else ""

    def set_user_status(self, user_id, status):
        if status not in ("active", "disabled"):
            raise ValueError(status)
        with self.db.tx() as conn:
            conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
            if status == "disabled":
                conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (self.clock(), user_id))
            self.db.audit(f"user_{status}", user_id, conn=conn)

    # --- rate limiting ---

    def _hit(self, conn, key, limit):
        now = self.clock()
        row = conn.execute("SELECT window_start, count FROM rate_limits WHERE key = ?", (key,)).fetchone()
        if not row or now - row["window_start"] > RATE_WINDOW_SECONDS:
            conn.execute("INSERT INTO rate_limits(key, window_start, count) VALUES (?, ?, 1) "
                         "ON CONFLICT(key) DO UPDATE SET window_start = excluded.window_start, count = 1", (key, now))
            return True
        if row["count"] >= limit:
            return False
        conn.execute("UPDATE rate_limits SET count = count + 1 WHERE key = ?", (key,))
        return True

    # --- login ---

    def start_login(self, username, device, client=""):
        """Start a login. Returns the browser token (cookie). Unknown users get an identical-looking response."""
        username = (username or "").strip().lstrip("@")[:64]
        device = (device or "")[:80]
        now = self.clock()
        browser_token, request_id = new_token(), new_token(12)
        code = f"{secrets.randbelow(10**6):06d}"
        lookup = self.crypto.lookup_hash(username)
        with self.db.tx() as conn:
            if not self._hit(conn, f"login-client:{client}", LOGIN_STARTS_PER_CLIENT) or \
                    not self._hit(conn, f"login-user:{lookup}", LOGIN_STARTS_PER_USER):
                raise AuthError("Troppi tentativi di accesso: riprova tra qualche minuto")
            user = conn.execute("SELECT id FROM users WHERE (login_hash = ? OR username_hash = ?) AND status = 'active'",
                                (lookup, lookup)).fetchone()
            conn.execute("""INSERT INTO login_requests(id, browser_hash, user_id, code_hash, device, created_at, expires_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (request_id, hash_token(browser_token), user["id"] if user else None,
                          hash_token(request_id + code), device, now, now + LOGIN_TTL_SECONDS))
        if user:
            self.notifier.send_login_request(self.chat_id(user["id"]), request_id, code, device)
        return browser_token

    def start_reauth(self, user_id, device):
        """Telegram confirmation for a sensitive action of an already logged-in user."""
        now = self.clock()
        browser_token, request_id = new_token(), new_token(12)
        code = f"{secrets.randbelow(10**6):06d}"
        with self.db.tx() as conn:
            if not self._hit(conn, f"reauth-user:{user_id}", LOGIN_STARTS_PER_USER):
                raise AuthError("Troppi tentativi: riprova tra qualche minuto")
            conn.execute("""INSERT INTO login_requests(id, browser_hash, user_id, code_hash, device, created_at, expires_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                         (request_id, hash_token(browser_token), user_id, hash_token(request_id + code),
                          f"{device} (conferma modifica dati)"[:80], now, now + LOGIN_TTL_SECONDS))
        self.notifier.send_login_request(self.chat_id(user_id), request_id, code, f"{device} (conferma modifica dati)")
        return browser_token

    def complete_reauth(self, browser_token, session_token):
        now = self.clock()
        with self.db.tx() as conn:
            req = conn.execute("SELECT * FROM login_requests WHERE browser_hash = ?", (hash_token(browser_token or ""),)).fetchone()
            session = conn.execute("SELECT user_id FROM sessions WHERE token_hash = ? AND revoked_at IS NULL",
                                   (hash_token(session_token or ""),)).fetchone()
            if (not req or not session or req["status"] != "approved" or req["expires_at"] < now
                    or req["user_id"] != session["user_id"]):
                raise AuthError("Conferma non valida")
            conn.execute("UPDATE login_requests SET status = 'used' WHERE id = ?", (req["id"],))
            conn.execute("UPDATE sessions SET auth_at = ? WHERE token_hash = ?", (now, hash_token(session_token)))
            self.db.audit("reauth", req["user_id"], conn=conn)

    def _request(self, browser_token):
        return self.db.query_one("SELECT * FROM login_requests WHERE browser_hash = ?", (hash_token(browser_token or ""),))

    def login_status(self, browser_token):
        req = self._request(browser_token)
        if not req:
            return "expired"
        if req["status"] == "pending" and req["expires_at"] < self.clock():
            return "expired"
        return req["status"]

    def _decide(self, request_id, chat_id, status):
        now = self.clock()
        with self.db.tx() as conn:
            req = conn.execute("SELECT * FROM login_requests WHERE id = ?", (request_id,)).fetchone()
            if not req or req["user_id"] is None or req["status"] != "pending" or req["expires_at"] < now:
                return False
            owner = conn.execute("SELECT chat_hash FROM users WHERE id = ?", (req["user_id"],)).fetchone()
            if not owner or not hmac.compare_digest(owner["chat_hash"] or "", self.crypto.lookup_hash(chat_id)):
                return False
            conn.execute("UPDATE login_requests SET status = ? WHERE id = ?", (status, request_id))
            self.db.audit(f"login_{status}", req["user_id"], req["device"], conn=conn)
        return True

    def approve(self, request_id, chat_id):
        return self._decide(request_id, chat_id, "approved")

    def deny(self, request_id, chat_id):
        return self._decide(request_id, chat_id, "denied")

    def check_code(self, browser_token, code):
        now = self.clock()
        with self.db.tx() as conn:
            req = conn.execute("SELECT * FROM login_requests WHERE browser_hash = ?", (hash_token(browser_token or ""),)).fetchone()
            if not req or req["status"] != "pending" or req["expires_at"] < now:
                return False
            ok = req["user_id"] is not None and hmac.compare_digest(hash_token(req["id"] + str(code or "").strip()), req["code_hash"])
            if ok:
                conn.execute("UPDATE login_requests SET status = 'approved' WHERE id = ?", (req["id"],))
                return True
            attempts = req["attempts"] + 1
            conn.execute("UPDATE login_requests SET attempts = ?, status = ? WHERE id = ?",
                         (attempts, "denied" if attempts >= MAX_CODE_ATTEMPTS else "pending", req["id"]))
            return False

    def complete_login(self, browser_token):
        """Exchange an approved login request for a session token (once)."""
        now = self.clock()
        session_token = new_token()
        with self.db.tx() as conn:
            req = conn.execute("SELECT * FROM login_requests WHERE browser_hash = ?", (hash_token(browser_token or ""),)).fetchone()
            if not req or req["status"] != "approved" or req["user_id"] is None or req["expires_at"] < now:
                raise AuthError("Accesso non confermato")
            conn.execute("UPDATE login_requests SET status = 'used' WHERE id = ?", (req["id"],))
            conn.execute("""INSERT INTO sessions(token_hash, user_id, csrf_secret, device, created_at, last_seen_at, expires_at, auth_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (hash_token(session_token), req["user_id"], new_token(), req["device"], now, now,
                          now + SESSION_IDLE_SECONDS, now))
            conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now, req["user_id"]))
            # Old login requests are useless: keep the table small
            conn.execute("DELETE FROM login_requests WHERE expires_at < ?", (now - 86400,))
        return session_token

    # --- sessions ---

    def session(self, session_token):
        if not session_token:
            return None
        now = self.clock()
        row = self.db.query_one("""SELECT s.*, u.display_name, u.is_admin, u.status AS user_status FROM sessions s
                                   JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?""", (hash_token(session_token),))
        if (not row or row["revoked_at"] or row["expires_at"] < now or row["user_status"] != "active"
                or now - row["created_at"] > SESSION_MAX_SECONDS):
            return None
        if now - row["last_seen_at"] > 3600:  # rolling expiry, without a write on every request
            with self.db.tx() as conn:
                conn.execute("UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                             (now, now + SESSION_IDLE_SECONDS, row["token_hash"]))
        return SessionInfo(row["user_id"], row["display_name"], row["is_admin"], row["csrf_secret"], row["auth_at"], self.clock)

    def mark_recent_auth(self, session_token):
        with self.db.tx() as conn:
            conn.execute("UPDATE sessions SET auth_at = ? WHERE token_hash = ?", (self.clock(), hash_token(session_token)))

    def revoke(self, session_token):
        with self.db.tx() as conn:
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (self.clock(), hash_token(session_token or "")))

    def revoke_all(self, user_id):
        with self.db.tx() as conn:
            conn.execute("UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL", (self.clock(), user_id))
            self.db.audit("sessions_revoked", user_id, conn=conn)

    def list_sessions(self, user_id):
        return self.db.query("""SELECT device, created_at, last_seen_at FROM sessions
                                WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY last_seen_at DESC""",
                             (user_id, self.clock()))
