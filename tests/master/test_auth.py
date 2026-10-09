import os
import stat

import pytest

from rlprenota.master.auth import AuthError, SESSION_IDLE_SECONDS, LOGIN_TTL_SECONDS


def login(auth, notifier, username="mario_r", device="Chrome su iPhone"):
    browser_token = auth.start_login(username, device, client="1.2.3.4")
    return browser_token, (notifier.login_requests[-1] if notifier.login_requests else None)


def test_database_file_is_private(db):
    assert stat.S_IMODE(os.stat(db.path).st_mode) == 0o600


def test_invite_redeem_links_telegram(auth, db):
    token = auth.create_invite("Mario")
    user_id = auth.redeem_invite(token, chat_id=1111, username="@Mario_R")
    row = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    assert row["status"] == "active"
    assert b"Mario_R" not in (row["username_enc"] or b"") and "Mario_R" not in (row["username_hash"] or "")
    assert auth.user_by_chat(1111) == user_id
    assert auth.chat_id(user_id) == 1111


def test_invite_single_use_and_expiry(auth, clock):
    token = auth.create_invite("Mario")
    auth.redeem_invite(token, chat_id=1, username="a")
    with pytest.raises(AuthError):
        auth.redeem_invite(token, chat_id=2, username="b")
    late = auth.create_invite("Luca")
    clock.advance(24 * 3600 + 1)
    with pytest.raises(AuthError):
        auth.redeem_invite(late, chat_id=3, username="c")
    with pytest.raises(AuthError):
        auth.redeem_invite("forged-token", chat_id=4, username="d")


def test_one_telegram_account_per_user(auth, active_user):
    token = auth.create_invite("Altro")
    with pytest.raises(AuthError):
        auth.redeem_invite(token, chat_id=active_user[1], username="altro")


def test_login_with_approve_button(auth, notifier, active_user):
    browser, (chat_id, request_id, code, device) = login(auth, notifier)
    assert chat_id == active_user[1] and device == "Chrome su iPhone" and len(code) == 6
    assert auth.login_status(browser) == "pending"
    with pytest.raises(AuthError):
        auth.complete_login(browser)            # not approved yet
    assert auth.approve(request_id, chat_id=active_user[1])
    assert auth.login_status(browser) == "approved"
    session = auth.complete_login(browser)
    info = auth.session(session)
    assert info.user_id == active_user[0] and not info.is_admin
    with pytest.raises(AuthError):
        auth.complete_login(browser)            # one session per approval


def test_login_with_code_fallback(auth, notifier, active_user):
    browser, (_, _, code, _) = login(auth, notifier)
    assert not auth.check_code(browser, "000000" if code != "000000" else "111111")
    assert auth.check_code(browser, code)
    assert auth.session(auth.complete_login(browser)).user_id == active_user[0]


def test_code_brute_force_locks_the_request(auth, notifier, active_user):
    browser, (_, _, code, _) = login(auth, notifier)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert not auth.check_code(browser, wrong)
    assert not auth.check_code(browser, code)   # even the right code is refused now
    assert auth.login_status(browser) == "denied"


def test_unknown_user_looks_the_same(auth, notifier):
    browser = auth.start_login("nessuno", "Chrome", client="1.2.3.4")
    assert browser and auth.login_status(browser) == "pending"
    assert notifier.login_requests == []        # nothing sent, but the browser can't tell


def test_stolen_request_id_is_useless_without_the_browser(auth, notifier, active_user):
    browser, (_, request_id, code, _) = login(auth, notifier)
    with pytest.raises(AuthError):
        auth.complete_login(request_id)         # the request id is not the browser token
    assert not auth.approve(request_id, chat_id=9999)  # approving from another chat does nothing
    assert auth.login_status(browser) == "pending"


def test_deny_and_expiry(auth, notifier, active_user, clock):
    browser, (_, request_id, _, _) = login(auth, notifier)
    auth.deny(request_id, chat_id=active_user[1])
    assert auth.login_status(browser) == "denied"
    browser2, (_, request_id2, _, _) = login(auth, notifier)
    clock.advance(LOGIN_TTL_SECONDS + 1)
    assert not auth.approve(request_id2, chat_id=active_user[1])
    assert auth.login_status(browser2) == "expired"


def test_login_rate_limit(auth, notifier, active_user):
    for _ in range(5):
        auth.start_login("mario_r", "Chrome", client="1.2.3.4")
    with pytest.raises(AuthError):
        auth.start_login("mario_r", "Chrome", client="5.6.7.8")   # per-user limit
    assert len(notifier.login_requests) == 5


def test_disabled_user_cannot_log_in(auth, notifier, active_user, db):
    auth.set_user_status(active_user[0], "disabled")
    auth.start_login("mario_r", "Chrome", client="1.2.3.4")
    assert notifier.login_requests == []


def test_session_rolls_and_expires_when_idle(auth, notifier, active_user, clock):
    browser, (_, request_id, _, _) = login(auth, notifier)
    auth.approve(request_id, chat_id=active_user[1])
    session = auth.complete_login(browser)
    for _ in range(3):  # used every 20 days: stays logged in
        clock.advance(20 * 86400)
        assert auth.session(session) is not None
    clock.advance(SESSION_IDLE_SECONDS + 1)
    assert auth.session(session) is None


def test_revocation(auth, notifier, active_user):
    sessions = []
    for _ in range(2):
        browser, (_, request_id, _, _) = login(auth, notifier)
        auth.approve(request_id, chat_id=active_user[1])
        sessions.append(auth.complete_login(browser))
    auth.revoke(sessions[0])
    assert auth.session(sessions[0]) is None and auth.session(sessions[1]) is not None
    auth.revoke_all(active_user[0])
    assert auth.session(sessions[1]) is None
    assert auth.session("garbage") is None and auth.session("") is None


def test_recent_auth_for_sensitive_actions(auth, notifier, active_user, clock):
    browser, (_, request_id, _, _) = login(auth, notifier)
    auth.approve(request_id, chat_id=active_user[1])
    session = auth.complete_login(browser)
    assert auth.session(session).recently_authenticated()
    clock.advance(11 * 60)
    assert not auth.session(session).recently_authenticated()


def test_csrf_tokens_are_per_session(auth, notifier, active_user):
    tokens = []
    for _ in range(2):
        browser, (_, request_id, _, _) = login(auth, notifier)
        auth.approve(request_id, chat_id=active_user[1])
        info = auth.session(auth.complete_login(browser))
        tokens.append(info.csrf_token)
        assert info.check_csrf(info.csrf_token) and not info.check_csrf("x") and not info.check_csrf(None)
    assert tokens[0] != tokens[1]


def test_login_name_works_without_telegram_username(auth, notifier):
    token = auth.create_invite("Nonna Pina", login_name="Pina")
    user_id = auth.redeem_invite(token, chat_id=2222, username=None)   # no Telegram @username
    auth.start_login("pina", "Safari su iPad", client="1.2.3.4")
    assert notifier.login_requests[-1][0] == 2222
    assert auth.login_name(user_id) == "pina"


def test_login_names_are_unique(auth):
    auth.create_invite("Mario", login_name="mario")
    with pytest.raises(AuthError):
        auth.create_invite("Mario Bis", login_name="Mario")
    with pytest.raises(AuthError):
        auth.create_invite("Strano", login_name="no spaces!")


def test_reauth_refreshes_the_session(auth, notifier, active_user, clock):
    browser, (_, request_id, _, _) = login(auth, notifier)
    auth.approve(request_id, chat_id=active_user[1])
    session = auth.complete_login(browser)
    clock.advance(3600)
    assert not auth.session(session).recently_authenticated()
    again = auth.start_reauth(active_user[0], "Chrome")
    auth.approve(notifier.login_requests[-1][1], chat_id=active_user[1])
    auth.complete_reauth(again, session)
    assert auth.session(session).recently_authenticated()


def test_reauth_of_another_user_cannot_upgrade_my_session(auth, notifier, active_user):
    browser, (_, request_id, _, _) = login(auth, notifier)
    auth.approve(request_id, chat_id=active_user[1])
    my_session = auth.complete_login(browser)
    other = auth.redeem_invite(auth.create_invite("Luigi", login_name="luigi"), chat_id=2222, username=None)
    theirs = auth.start_reauth(other, "Chrome")
    auth.approve(notifier.login_requests[-1][1], chat_id=2222)
    with pytest.raises(AuthError):
        auth.complete_reauth(theirs, my_session)
