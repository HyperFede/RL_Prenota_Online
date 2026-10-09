import re

import pytest
from fastapi.testclient import TestClient

from rlprenota.master.app import Services, create_user_app
from rlprenota.master.auth import RECENT_AUTH_SECONDS
from rlprenota.master.searches import SearchRepo

PREFIX = "k3J9xQ2mZ7"
ORIGIN = "https://rl.example.ts.net"


@pytest.fixture
def services(db, crypto, auth, clock, notifier):
    return Services(db=db, crypto=crypto, auth=auth, searches=SearchRepo(db, crypto, clock=clock), clock=clock)


@pytest.fixture
def client(services):
    app = create_user_app(services, PREFIX)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}, follow_redirects=False) as c:
        yield c


def url(path=""):
    return f"/{PREFIX}/{path}"


def logged_in(client, auth, notifier, name="mario", chat=1111):
    if auth.user_by_chat(chat) is None:
        auth.redeem_invite(auth.create_invite(name.title(), login_name=name), chat_id=chat, username=None)
    client.cookies.clear()
    response = client.post(url("login"), data={"username": name}, headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari"})
    assert response.status_code == 303
    request_id = notifier.login_requests[-1][1]
    auth.approve(request_id, chat_id=chat)
    response = client.get(url("login/wait"))
    assert response.status_code == 303 and response.headers["location"] == url()
    return client


def csrf(client, path):
    page = client.get(url(path))
    assert page.status_code == 200, page.text[:200]
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


SEARCH_FORM = {"label": "Cardiologia", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2099-12-31",
               "refresh_seconds": "120", "dry_run": "on", "codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345",
               "ricetta": "0300A1234567890", "telefono": "3331234567", "telegram_timeout": "15"}


def create_search(client, **overrides):
    data = {**SEARCH_FORM, **overrides, "csrf": csrf(client, "searches/new")}
    return client.post(url("searches"), data=data)


# --- hiding and headers ---

def test_everything_outside_the_prefix_is_a_bare_404(client):
    for path in ["/", "/login", "/admin", "/wrongprefix/", "/docs", "/openapi.json", f"/{PREFIX}x/"]:
        response = client.get(path)
        assert response.status_code == 404 and response.text == "", path


def test_security_headers(client):
    response = client.get(url("login"))
    h = response.headers
    assert "script-src 'none'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    # Not "no-referrer": with it browsers send "Origin: null" on form posts and every POST would be refused
    assert h["referrer-policy"] == "same-origin" and "max-age" in h["strict-transport-security"]
    assert "no-store" in h["cache-control"]


def test_unauthenticated_redirects_to_login(client):
    response = client.get(url())
    assert response.status_code == 303 and response.headers["location"] == url("login")


# --- login ---

def test_login_flow_and_cookie_flags(client, auth, notifier):
    auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), chat_id=1111, username=None)
    response = client.post(url("login"), data={"username": "mario"}, headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari"})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie and f"path=/{PREFIX.lower()}" in cookie
    assert notifier.login_requests[-1][3] == "Safari su iPhone"
    page = client.get(url("login/wait"))
    assert page.status_code == 200 and 'http-equiv="refresh"' in page.text
    auth.approve(notifier.login_requests[-1][1], chat_id=1111)
    response = client.get(url("login/wait"))
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get(url()).status_code == 200


def test_login_with_code(client, auth, notifier):
    auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), chat_id=1111, username=None)
    client.post(url("login"), data={"username": "mario"})
    code = notifier.login_requests[-1][2]
    response = client.post(url("login/code"), data={"code": code})
    assert response.status_code == 303 and client.get(url()).status_code == 200


def test_unknown_user_sees_the_same_waiting_page(client, notifier):
    response = client.post(url("login"), data={"username": "nessuno"})
    assert response.status_code == 303 and response.headers["location"] == url("login/wait")
    assert notifier.login_requests == []


def test_cross_origin_posts_are_refused(client):
    assert client.post(url("login"), data={"username": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post(url("login"), data={"username": "x"}, headers={"Origin": ""}).status_code == 403
    assert client.post(url("login"), data={"username": "x"}, headers={"Origin": "null"}).status_code == 403


def test_logout_revokes_the_session(client, auth, notifier):
    logged_in(client, auth, notifier)
    client.post(url("logout"), data={"csrf": csrf(client, "")})
    assert client.get(url()).status_code == 303


# --- searches ---

def test_new_search_form_defaults(client, auth, notifier):
    logged_in(client, auth, notifier)
    page = client.get(url("searches/new")).text
    assert re.search(r'value="provinces" id="mode-provinces"\s+checked', page)
    assert 'name="dry_run" checked' in page


def test_create_search_and_secrets_never_shown(client, auth, notifier):
    logged_in(client, auth, notifier)
    response = create_search(client)
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert page.status_code == 200 and "Cardiologia" in page.text
    assert "MRTMTT25D09F205Z" not in page.text and "205Z" in page.text
    assert "0300A1234567890" not in page.text
    dashboard = client.get(url())
    assert "Cardiologia" in dashboard.text and "MRTMTT25D09F205Z" not in dashboard.text


def test_validation_errors_are_shown_without_echoing_secrets(client, auth, notifier):
    logged_in(client, auth, notifier)
    response = create_search(client, codice_fiscale="MRTMTT25D09F205A")
    assert response.status_code == 422 and "Codice fiscale non valido" in response.text
    assert "MRTMTT25D09F205A" not in response.text


def test_csrf_required(client, auth, notifier):
    logged_in(client, auth, notifier)
    assert client.post(url("searches"), data=SEARCH_FORM).status_code == 403
    assert client.post(url("searches"), data={**SEARCH_FORM, "csrf": "forged"}).status_code == 403


def test_html_is_escaped(client, auth, notifier):
    logged_in(client, auth, notifier)
    location = create_search(client, label="<script>alert(1)</script>").headers["location"]
    page = client.get(location).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_limit_message(client, auth, notifier):
    logged_in(client, auth, notifier)
    for _ in range(3):
        assert create_search(client).status_code == 303
    response = create_search(client)
    assert response.status_code == 409 and "massimo" in response.text


def test_other_users_get_404_everywhere(client, auth, notifier):
    logged_in(client, auth, notifier, "mario", 1111)
    search_path = create_search(client).headers["location"]
    search_id = search_path.rstrip("/").rsplit("/", 1)[-1]
    logged_in(client, auth, notifier, "luigi", 2222)
    token = csrf(client, "")
    assert client.get(url(f"searches/{search_id}")).status_code == 404
    assert client.get(url(f"searches/{search_id}/edit")).status_code == 404
    for action in ("pause", "resume", "delete", "undiscard/abc"):
        assert client.post(url(f"searches/{search_id}/{action}"), data={"csrf": token}).status_code == 404, action
    assert client.post(url(f"searches/{search_id}/edit"), data={**SEARCH_FORM, "csrf": token}).status_code == 404


def test_pause_resume_delete(client, auth, notifier, services):
    logged_in(client, auth, notifier)
    search_id = create_search(client).headers["location"].rstrip("/").rsplit("/", 1)[-1]
    token = csrf(client, "")
    client.post(url(f"searches/{search_id}/pause"), data={"csrf": token})
    assert services.searches.get(1, search_id).status == "paused"
    client.post(url(f"searches/{search_id}/resume"), data={"csrf": token})
    assert services.searches.get(1, search_id).status == "active"
    client.post(url(f"searches/{search_id}/delete"), data={"csrf": token})
    assert services.searches.get(1, search_id) is None


def test_changing_personal_data_needs_a_recent_telegram_confirmation(client, auth, notifier, services, clock):
    logged_in(client, auth, notifier)
    search_id = create_search(client).headers["location"].rstrip("/").rsplit("/", 1)[-1]
    clock.advance(RECENT_AUTH_SECONDS + 1)
    form = {**SEARCH_FORM, "tessera": "54321", "csrf": csrf(client, f"searches/{search_id}/edit")}
    response = client.post(url(f"searches/{search_id}/edit"), data=form)
    assert response.status_code == 403 and "Telegram" in response.text
    assert services.searches.secrets_for(search_id).tessera == "12345"
    # Only non-secret settings: no confirmation needed
    form = {**SEARCH_FORM, "codice_fiscale": "", "tessera": "", "ricetta": "", "telefono": "", "label": "Rinominata",
            "csrf": csrf(client, f"searches/{search_id}/edit")}
    assert client.post(url(f"searches/{search_id}/edit"), data=form).status_code == 303
    assert services.searches.get(1, search_id).settings.label == "Rinominata"
    assert services.searches.secrets_for(search_id).tessera == "12345"
