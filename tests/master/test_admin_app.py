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
