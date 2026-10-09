from datetime import date

import pytest

from rlprenota.master.scheduler import LeaseLost, Scheduler, LEASE_TTL_SECONDS
from rlprenota.master.searches import SearchRepo
from rlprenota.master.validation import SearchSecrets, SearchSettings

SECRETS = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"})


def settings(refresh="120", **kw):
    form = {"label": "Visita", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2099-12-31",
            "refresh_seconds": refresh}
    form.update(kw)
    return SearchSettings.from_form(form, today=date(2026, 10, 9))


class Alerts:
    def __init__(self):
        self.user, self.admin = [], []

    def notify_user(self, user_id, text):
        self.user.append((user_id, text))

    def alert_admin(self, key, text):
        self.admin.append((key, text))


@pytest.fixture
def alerts():
    return Alerts()


@pytest.fixture
def repo(db, crypto, clock):
    return SearchRepo(db, crypto, clock=clock)


@pytest.fixture
def scheduler(db, repo, clock, alerts):
    return Scheduler(db, repo, alerts, clock=clock, workers=2)


@pytest.fixture
def user(auth):
    return auth.redeem_invite(auth.create_invite("Mario", login_name="mario"), 1111, None)


def test_lease_gives_due_search_with_secrets_once(scheduler, repo, user):
    search_id = repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    assert lease.search_id == search_id and lease.secrets == SECRETS and lease.user_id == user
    assert scheduler.lease("w2") is None              # never two workers on the same search


def test_paused_or_not_due_searches_are_not_leased(scheduler, repo, user, clock):
    paused = repo.create(user, settings(), SECRETS)
    repo.pause(user, paused)
    assert scheduler.lease("w1") is None
    active = repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="continue", duration=30)
    assert scheduler.lease("w1") is None              # next run only after the refresh interval
    clock.advance(121)
    assert scheduler.lease("w1").search_id == active


def test_expired_lease_is_requeued_and_late_completion_refused(scheduler, repo, user, clock):
    repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    clock.advance(LEASE_TTL_SECONDS + 1)               # worker died: no heartbeat
    scheduler.reap_expired()
    again = scheduler.lease("w2")
    assert again is not None and again.lease_id != lease.lease_id
    with pytest.raises(LeaseLost):
        scheduler.complete(lease.lease_id, outcome="continue", duration=10)
    with pytest.raises(LeaseLost):
        scheduler.heartbeat(lease.lease_id)


def test_heartbeat_keeps_the_lease(scheduler, repo, user, clock):
    repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    for _ in range(5):
        clock.advance(LEASE_TTL_SECONDS - 5)
        scheduler.heartbeat(lease.lease_id)
        scheduler.reap_expired()
    scheduler.complete(lease.lease_id, outcome="continue", duration=10)


def test_oldest_due_first(scheduler, repo, user, clock):
    first = repo.create(user, settings(), SECRETS)
    clock.advance(1)
    second = repo.create(user, settings(), SECRETS)
    assert scheduler.lease("w1").search_id == first
    assert scheduler.lease("w2").search_id == second


def test_transient_errors_back_off_then_circuit_breaker(scheduler, repo, user, clock, alerts, db):
    search_id = repo.create(user, settings(), SECRETS)
    waits = []
    for _ in range(5):
        lease = scheduler.lease("w1")
        assert lease is not None
        scheduler.complete(lease.lease_id, outcome="error", error_kind="transient", message="timeout", duration=5)
        row = db.query_one("SELECT next_run_at, status FROM searches WHERE id = ?", (search_id,))
        waits.append(row["next_run_at"] - clock())
        clock.advance(row["next_run_at"] - clock() + 1)
    assert waits[:4] == [60, 120, 240, 480]
    assert repo.get(user, search_id).status == "error"        # 5th consecutive failure: paused
    assert alerts.user and alerts.admin


def test_success_resets_failures(scheduler, repo, user, clock, db):
    search_id = repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="error", error_kind="transient", message="x", duration=5)
    clock.advance(61)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="continue", duration=5, current_appointment="05/11/2026 - 08:30")
    row = db.query_one("SELECT consecutive_failures, current_appointment FROM searches WHERE id = ?", (search_id,))
    assert row["consecutive_failures"] == 0 and row["current_appointment"] == "05/11/2026 - 08:30"


def test_login_rejected_pauses_and_tells_the_user(scheduler, repo, user, alerts):
    search_id = repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="error", error_kind="login_rejected", message="Ricetta scaduta", duration=5)
    assert repo.get(user, search_id).status == "error"
    assert "Ricetta scaduta" in alerts.user[-1][1] and not alerts.admin


def test_portal_change_on_two_searches_pauses_everything(scheduler, repo, user, auth, clock, alerts):
    other = auth.redeem_invite(auth.create_invite("Luigi", login_name="luigi"), 2222, None)
    repo.create(user, settings(), SECRETS)
    repo.create(other, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="error", error_kind="portal_changed", message="x", selector="select#provincia",
                       step="ricerca", duration=5)
    assert not scheduler.globally_paused() and not alerts.admin
    lease = scheduler.lease("w2")
    scheduler.complete(lease.lease_id, outcome="error", error_kind="portal_changed", message="x", selector="select#provincia",
                       step="ricerca", duration=5)
    assert scheduler.globally_paused()
    assert "select#provincia" in alerts.admin[-1][1]
    clock.advance(3600)
    assert scheduler.lease("w1") is None               # nothing runs until the admin resumes
    scheduler.resume_global()
    assert scheduler.lease("w1") is not None


def test_throttling_pauses_globally_for_a_while(scheduler, repo, user, clock, alerts):
    repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="error", error_kind="throttled", message="429", duration=5)
    clock.advance(300)
    assert scheduler.lease("w1") is None
    clock.advance(400)
    assert scheduler.lease("w1") is not None
    assert alerts.admin


def test_booked_finishes_the_search_unless_continue(scheduler, repo, user, clock):
    done = repo.create(user, settings(), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="booked", message="Prenotato 05/11/2026 08:30", duration=5)
    assert repo.get(user, done).status == "booked" and repo.secrets_for(done) is None
    keep = repo.create(user, settings(continua_dopo_prenotazione="on"), SECRETS)
    lease = scheduler.lease("w1")
    scheduler.complete(lease.lease_id, outcome="booked", message="Prenotato", duration=5, current_appointment="05/11/2026 - 08:30")
    view = repo.get(user, keep)
    assert view.status == "active" and view.current_appointment == "05/11/2026 - 08:30"


def test_interval_stretches_fairly_when_busy(scheduler, repo, user, auth, clock, db):
    users = [user] + [auth.redeem_invite(auth.create_invite(f"Utente{i}", login_name=f"utente{i}"), 3000 + i, None)
                      for i in range(3)]
    ids = [repo.create(u, settings(refresh="60"), SECRETS) for u in users]
    for search_id in ids:
        lease = scheduler.lease("w1")
        scheduler.complete(lease.lease_id, outcome="continue", duration=90)  # slow cycles
    rows = db.query("SELECT next_run_at, effective_interval FROM searches")
    # 4 searches x 90 s / 2 workers = 180 s: asking for 60 s is not sustainable
    assert all(r["effective_interval"] >= 180 for r in rows)


def test_permits_space_out_portal_searches(scheduler, clock):
    waits = [scheduler.permit() for _ in range(3)]
    assert waits[0] == 0 and 2.5 <= waits[1] <= 3.5 and 5 <= waits[2] <= 7
