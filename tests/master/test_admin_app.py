import re
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rlprenota.master.admin_app import create_admin_app
from rlprenota.master.canary import CanaryResult
from rlprenota.master.scheduler import Scheduler
from rlprenota.master.searches import SearchRepo
from rlprenota.master.validation import SearchSecrets, SearchSettings

ORIGIN = "https://rl-prenota.example.ts.net:8443"
SECRETS = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"})


class Alerts:
    def notify_user(self, *a):
        pass

    def alert_admin(self, *a):
        pass


@pytest.fixture
def services(db, crypto, auth, clock):
    searches = SearchRepo(db, crypto, clock=clock)
    scheduler = Scheduler(db, searches, Alerts(), clock=clock)
    canary = SimpleNamespace(run=lambda: CanaryResult("ok", "abc"))
    config = SimpleNamespace(invite_link=lambda token: f"https://t.me/RLBot?start={token}", workers=2, admin_origin=ORIGIN,
                             trusted_proxies=())
    return SimpleNamespace(db=db, crypto=crypto, auth=auth, searches=searches, scheduler=scheduler, canary=canary, config=config)


@pytest.fixture
def client(services):
    with TestClient(create_admin_app(services), base_url=ORIGIN, headers={"Origin": ORIGIN}, follow_redirects=False) as c:
        yield c


def login(client, auth, notifier, name, chat, admin):
    if auth.user_by_chat(chat) is None:
        auth.redeem_invite(auth.create_invite(name.title(), login_name=name, is_admin=admin), chat_id=chat, username=None)
    client.cookies.clear()
    client.post("/admin/login", data={"username": name})
    auth.approve(notifier.login_requests[-1][1], chat_id=chat)
    return client.get("/admin/login/wait")


def csrf(client):
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/").text).group(1)


def test_only_admins_get_in(client, auth, notifier):
    response = login(client, auth, notifier, "mario", 1111, admin=False)
    assert response.status_code == 403 and "amministratori" in response.text
    assert client.get("/admin/").status_code == 303            # still not logged in
    assert login(client, auth, notifier, "fede", 9000, admin=True).status_code == 303
    assert client.get("/admin/").status_code == 200


def test_paths_outside_admin_are_404(client):
    assert client.get("/").status_code == 404 and client.get("/docs").status_code == 404


def test_overview_never_shows_personal_data(client, auth, notifier, services):
    user = auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), 1111, None)
    form = {"label": "Cardiologia", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2099-12-31"}
    services.searches.create(user, SearchSettings.from_form(form, today=date(2026, 10, 9)), SECRETS)
    login(client, auth, notifier, "fede", 9000, admin=True)
    page = client.get("/admin/").text
    assert "Cardiologia" in page and "Mario" in page
    assert "MRTMTT25D09F205Z" not in page and "0300A1234567890" not in page and "205Z" not in page


def test_invite_shows_the_link_once(client, auth, notifier):
    login(client, auth, notifier, "fede", 9000, admin=True)
    response = client.post("/admin/invite", data={"name": "Nonna Pina", "login": "pina", "csrf": csrf(client)})
    assert response.status_code == 200 and "https://t.me/RLBot?start=" in response.text and "pina" in response.text
    bad = client.post("/admin/invite", data={"name": "X", "login": "no spaces!", "csrf": csrf(client)})
    assert bad.status_code == 422


def test_disable_user_revokes_sessions_and_pauses_searches(client, auth, notifier, services):
    login(client, auth, notifier, "mario", 1111, admin=False)
    user = auth.user_by_chat(1111)
    form = {"label": "V", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2099-12-31"}
    search_id = services.searches.create(user, SearchSettings.from_form(form, today=date(2026, 10, 9)), SECRETS)
    login(client, auth, notifier, "fede", 9000, admin=True)
    client.post(f"/admin/users/{user}/disable", data={"csrf": csrf(client)})
    assert services.searches.get(user, search_id).status == "paused"
    assert auth.user_by_chat(1111) is None


def test_global_pause_and_resume(client, auth, notifier, services):
    login(client, auth, notifier, "fede", 9000, admin=True)
    services.scheduler.pause_global("Il portale sembra cambiato: '#cf' non trovato")
    page = client.get("/admin/").text
    assert "#cf" in page and "Riprendi tutte le ricerche" in page
    client.post("/admin/global/resume", data={"csrf": csrf(client)})
    assert not services.scheduler.globally_paused()
    client.post("/admin/global/pause", data={"csrf": csrf(client), "reason": "manutenzione"})
    assert services.scheduler.globally_paused()


def test_limits_are_validated(client, auth, notifier, services):
    login(client, auth, notifier, "fede", 9000, admin=True)
    assert client.post("/admin/limits", data={"csrf": csrf(client), "per_user": "2", "total": "6"}).status_code == 303
    assert services.db.get_setting("max_active_per_user") == "2" and services.db.get_setting("max_active_total") == "6"
    assert client.post("/admin/limits", data={"csrf": csrf(client), "per_user": "0", "total": "6"}).status_code == 422
    assert client.post("/admin/limits", data={"csrf": csrf(client), "per_user": "5", "total": "999"}).status_code == 422


def test_canary_can_be_run_on_demand(client, auth, notifier):
    login(client, auth, notifier, "fede", 9000, admin=True)
    assert client.post("/admin/canary/run", data={"csrf": csrf(client)}).status_code == 303


def test_admin_actions_need_csrf(client, auth, notifier):
    login(client, auth, notifier, "fede", 9000, admin=True)
    assert client.post("/admin/global/resume", data={}).status_code == 403
    assert client.post("/admin/invite", data={"name": "X", "login": "xyz"}).status_code == 403


# --- invites, roles and account deletion (bugs found in production) ---

def invite(client, name="Massi", login_name="massi", role=None):
    data = {"name": name, "login": login_name, "csrf": csrf(client)}
    if role is not None:
        data["role"] = role
    return client.post("/admin/invite", data=data)


def test_invite_role_is_explicit_and_defaults_to_user(client, auth, notifier, db):
    login(client, auth, notifier, "fede", 9000, admin=True)
    response = invite(client)
    assert response.status_code == 200 and "Utente" in response.text
    assert db.query_one("SELECT is_admin FROM users WHERE display_name = 'Massi'")["is_admin"] == 0
    invite(client, "Luca", "luca", role="admin")
    assert db.query_one("SELECT is_admin FROM users WHERE display_name = 'Luca'")["is_admin"] == 1
    assert invite(client, "Strano", "strano", role="root").status_code == 422
    # The old checkbox can't sneak admin rights in anymore
    client.post("/admin/invite", data={"name": "X", "login": "xxx", "admin": "on", "csrf": csrf(client)})
    assert db.query_one("SELECT is_admin FROM users WHERE display_name = 'X'")["is_admin"] == 0


def test_invite_result_does_not_auto_refresh_into_a_blank_page(client, auth, notifier):
    login(client, auth, notifier, "fede", 9000, admin=True)
    page = invite(client).text
    assert 'http-equiv="refresh"' not in page and "https://t.me/RLBot?start=" in page
    reloaded = client.get("/admin/invite")
    assert reloaded.status_code == 303 and reloaded.headers["location"] == "/admin/"


def test_lost_link_can_be_regenerated_and_the_old_one_dies(client, auth, notifier, db):
    login(client, auth, notifier, "fede", 9000, admin=True)
    first = re.search(r"start=([\w-]+)", invite(client).text).group(1)
    user_id = db.query_one("SELECT id FROM users WHERE display_name = 'Massi'")["id"]
    assert "Nuovo link" in client.get("/admin/").text
    second = re.search(r"start=([\w-]+)", client.post(f"/admin/users/{user_id}/invite", data={"csrf": csrf(client)}).text).group(1)
    assert second != first
    with pytest.raises(Exception):
        auth.redeem_invite(first, chat_id=7777, username=None)      # superseded
    assert auth.redeem_invite(second, chat_id=7777, username=None) == user_id
    # Linked users don't get new links (that would let someone else take over the account)
    assert client.post(f"/admin/users/{user_id}/invite", data={"csrf": csrf(client)}).status_code == 409


def test_change_role(client, auth, notifier, db):
    login(client, auth, notifier, "fede", 9000, admin=True)
    invite(client, role="admin")
    user_id = db.query_one("SELECT id FROM users WHERE display_name = 'Massi'")["id"]
    client.post(f"/admin/users/{user_id}/role", data={"role": "user", "csrf": csrf(client)})
    assert db.query_one("SELECT is_admin FROM users WHERE id = ?", (user_id,))["is_admin"] == 0
    me = auth.user_by_chat(9000)
    assert client.post(f"/admin/users/{me}/role", data={"role": "user", "csrf": csrf(client)}).status_code == 422
    assert db.query_one("SELECT is_admin FROM users WHERE id = ?", (me,))["is_admin"] == 1


def test_delete_account_removes_everything_after_confirmation(client, auth, notifier, services, db):
    login(client, auth, notifier, "mario", 1111, admin=False)
    user = auth.user_by_chat(1111)
    form = {"label": "V", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2099-12-31"}
    search_id = services.searches.create(user, SearchSettings.from_form(form, today=date(2026, 10, 9)), SECRETS)
    login(client, auth, notifier, "fede", 9000, admin=True)
    confirm = client.get(f"/admin/users/{user}/delete")
    assert confirm.status_code == 200 and "Mario" in confirm.text and "1 ricerc" in confirm.text
    assert db.query_one("SELECT id FROM users WHERE id = ?", (user,)) is not None   # nothing deleted by the GET
    response = client.post(f"/admin/users/{user}/delete", data={"csrf": csrf(client)})
    assert response.status_code == 303
    assert db.query_one("SELECT id FROM users WHERE id = ?", (user,)) is None
    assert db.query_one("SELECT id FROM searches WHERE id = ?", (search_id,)) is None
    assert db.query("SELECT * FROM sessions WHERE user_id = ?", (user,)) == []
    assert auth.user_by_chat(1111) is None


def test_cannot_delete_yourself_or_unknown_users(client, auth, notifier):
    login(client, auth, notifier, "fede", 9000, admin=True)
    me = auth.user_by_chat(9000)
    assert client.post(f"/admin/users/{me}/delete", data={"csrf": csrf(client)}).status_code == 422
    assert client.get("/admin/users/999/delete").status_code == 404
    assert client.post("/admin/users/999/delete", data={"csrf": csrf(client)}).status_code == 404
    assert client.post(f"/admin/users/{me}/delete", data={}).status_code == 403
