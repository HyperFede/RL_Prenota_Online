from datetime import date

import pytest

from rlprenota.master.searches import LimitReached, SearchRepo, NotFound
from rlprenota.master.validation import SearchSecrets, SearchSettings

TODAY = date(2026, 10, 9)


def settings(**kw):
    form = {"label": "Cardiologia", "location_mode": "provinces", "province": ["BERGAMO"], "end_date": "2026-12-31",
            "refresh_seconds": "120"}
    form.update(kw)
    return SearchSettings.from_form(form, today=TODAY)


SECRETS = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "12345", "ricetta": "0300A1234567890"})


@pytest.fixture
def repo(db, crypto, clock):
    return SearchRepo(db, crypto, clock=clock)


@pytest.fixture
def two_users(auth):
    a = auth.redeem_invite(auth.create_invite("Alice"), 1, "alice")
    b = auth.redeem_invite(auth.create_invite("Bruno"), 2, "bruno")
    return a, b


def test_create_and_read_own_search(repo, two_users, db):
    a, _ = two_users
    search_id = repo.create(a, settings(), SECRETS)
    assert len(search_id) >= 12
    view = repo.get(a, search_id)
    assert view.status == "active" and view.settings.label == "Cardiologia"
    assert view.masked["codice_fiscale"].endswith("205Z")
    raw = db.query_one("SELECT secrets_enc FROM searches WHERE id = ?", (search_id,))["secrets_enc"]
    assert b"MRTMTT25D09F205Z" not in raw
    assert repo.secrets_for(search_id) == SECRETS


def test_other_users_cannot_see_or_touch_a_search(repo, two_users):
    a, b = two_users
    search_id = repo.create(a, settings(), SECRETS)
    assert repo.get(b, search_id) is None
    assert repo.list_for_user(b) == []
    for action in (lambda: repo.pause(b, search_id), lambda: repo.resume(b, search_id), lambda: repo.delete(b, search_id),
                   lambda: repo.update(b, search_id, settings(label="hack")), lambda: repo.undiscard(b, search_id, "x")):
        with pytest.raises(NotFound):
            action()
    assert repo.get(a, search_id).settings.label == "Cardiologia"


def test_limits_per_user_and_global(repo, auth, two_users, db):
    a, b = two_users
    for _ in range(3):
        repo.create(a, settings(), SECRETS)
    with pytest.raises(LimitReached):
        repo.create(a, settings(), SECRETS)
    # Paused searches don't use resources: one can be paused and another created
    first = repo.list_for_user(a)[0].id
    repo.pause(a, first)
    repo.create(a, settings(), SECRETS)
    with pytest.raises(LimitReached):
        repo.resume(a, first)

    db.set_setting("max_active_total", 3)  # user A already runs 3
    with pytest.raises(LimitReached):
        repo.create(b, settings(), SECRETS)


def test_update_keeps_secrets_unless_replaced(repo, two_users):
    a, _ = two_users
    search_id = repo.create(a, settings(), SECRETS)
    repo.update(a, search_id, settings(label="Nuovo nome"))
    assert repo.get(a, search_id).settings.label == "Nuovo nome" and repo.secrets_for(search_id) == SECRETS
    other = SearchSecrets.from_form({"codice_fiscale": "MRTMTT25D09F205Z", "tessera": "54321", "ricetta": "0300A1234567891"})
    repo.update(a, search_id, settings(), other)
    assert repo.secrets_for(search_id).tessera == "54321"


def test_finished_searches_lose_their_secrets(repo, two_users, db):
    a, _ = two_users
    search_id = repo.create(a, settings(), SECRETS)
    repo.finish(search_id, "booked", "Prenotato il 05/11/2026 08:30")      # appointment unknown: nothing to continue
    assert repo.secrets_for(search_id) is None
    view = repo.get(a, search_id)
    assert view.status == "booked" and "05/11/2026" in view.last_result
    with pytest.raises(LimitReached):  # can't resume without data
        repo.resume(a, search_id)


def execute(db, sql, params):
    with db.tx() as conn:
        conn.execute(sql, params)


def booked(repo, db, user, appointment, end_date="2027-12-31"):
    search_id = repo.create(user, settings(end_date=end_date), SECRETS)
    execute(db, "UPDATE searches SET current_appointment = ? WHERE id = ?", (appointment, search_id))
    repo.finish(search_id, "booked", f"Prenotato: {appointment}")
    return search_id


def row(db, search_id):
    return db.query_one("SELECT * FROM searches WHERE id = ?", (search_id,))


# clock() is 15/01/2027 09:00 in Rome (08:00 UTC)

def test_booked_search_keeps_its_secrets_for_48_hours(repo, two_users, db, clock):
    a, _ = two_users
    search_id = booked(repo, db, a, "20/02/2027 - 10:00")
    assert repo.secrets_for(search_id) == SECRETS and repo.get(a, search_id).ended
    assert repo.get(a, search_id).continue_until == clock() + 48 * 3600
    clock.advance(48 * 3600 - 60)                   # 47 h 59 min: still possible
    repo.expire_finished(today=date(2027, 1, 17))
    assert repo.secrets_for(search_id) == SECRETS
    assert repo.continuable(row(db, search_id), today=date(2027, 1, 17)) == ""
    clock.advance(60)                               # 48 h: the window is over
    assert "48 ore" in repo.continuable(row(db, search_id), today=date(2027, 1, 17))
    repo.expire_finished(today=date(2027, 1, 17))
    assert repo.secrets_for(search_id) is None and repo.get(a, search_id).status == "booked"
    with pytest.raises(LimitReached):
        repo.reopen(search_id, a, today=date(2027, 1, 17))


def test_appointment_passing_inside_the_window_wipes_too(repo, two_users, db, clock):
    a, _ = two_users
    search_id = booked(repo, db, a, "16/01/2027 - 10:00")      # tomorrow
    clock.advance(26 * 3600)                                   # 16/01 11:00
    assert "già passata" in repo.continuable(row(db, search_id), today=date(2027, 1, 16))
    repo.expire_finished(today=date(2027, 1, 16))
    assert repo.secrets_for(search_id) is None


def test_continued_then_booked_again_gets_a_new_window(repo, two_users, db, clock):
    a, _ = two_users
    search_id = booked(repo, db, a, "20/02/2027 - 10:00")
    clock.advance(40 * 3600)
    repo.reopen(search_id, a, today=date(2027, 1, 16), owner_id=a)
    assert row(db, search_id)["ended_at"] is None
    execute(db, "UPDATE searches SET current_appointment = '10/02/2027 - 09:00' WHERE id = ?", (search_id,))
    repo.finish(search_id, "booked", "Prenotato")
    clock.advance(40 * 3600)                                   # 80 h after the first booking, 40 h after the second
    repo.expire_finished(today=date(2027, 1, 18))
    assert repo.continuable(row(db, search_id), today=date(2027, 1, 18)) == ""


def test_old_database_gets_the_new_column(tmp_path):
    import sqlite3
    from rlprenota.master.db import Database
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE searches (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, status TEXT NOT NULL, "
                "settings_json TEXT NOT NULL, secrets_enc BLOB, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "next_run_at REAL NOT NULL DEFAULT 0, last_run_at REAL, last_result TEXT NOT NULL DEFAULT '', "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0, current_appointment TEXT NOT NULL DEFAULT '', "
                "effective_interval REAL)")
    old.execute("CREATE TABLE leases (search_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL UNIQUE, worker TEXT NOT NULL, "
                "started_at REAL NOT NULL, expires_at REAL NOT NULL)")
    old.execute("INSERT INTO leases VALUES ('s', 'l', 'w1', 0, 0)")
    old.execute("INSERT INTO searches(id, user_id, status, settings_json, created_at, updated_at) "
                "VALUES ('s', 1, 'booked', '{}', 0, 0)")
    old.commit()
    old.close()
    db = Database(str(path))
    assert db.query_one("SELECT ended_at FROM searches WHERE id = 's'")["ended_at"] is None
    assert db.query_one("SELECT revoked FROM leases WHERE lease_id = 'l'")["revoked"] == 0


def test_past_appointment_wipes_at_once(repo, two_users, db):
    a, _ = two_users
    assert repo.secrets_for(booked(repo, db, a, "10/01/2027 - 10:00")) is None


def test_users_cannot_resume_pause_or_edit_a_closed_search(repo, two_users, db):
    a, _ = two_users
    search_id = booked(repo, db, a, "20/02/2027 - 10:00")
    with pytest.raises(LimitReached):
        repo.resume(a, search_id)
    with pytest.raises(LimitReached):
        repo.update(a, search_id, settings())
    repo.pause(a, search_id)
    assert repo.get(a, search_id).status == "booked"


def test_admin_continues_a_booked_search_before_its_appointment(repo, two_users, db):
    a, b = two_users
    search_id = booked(repo, db, a, "20/02/2027 - 10:00")
    assert repo.continuable(row(db, search_id), today=date(2027, 1, 15)) == ""
    repo.reopen(search_id, b, today=date(2027, 1, 15))
    view = repo.get(a, search_id)
    assert view.status == "active" and view.current_appointment == "20/02/2027 - 10:00"
    assert db.query_one("SELECT user_id FROM audit WHERE action = 'search_reopened'")["user_id"] == b


@pytest.mark.parametrize("appointment, end_date, reason", [
    ("10/01/2027 - 10:00", "2027-12-31", "già passata"),      # appointment past (secrets wiped too)
    ("15/01/2027 - 07:30", "2027-12-31", "già passata"),      # earlier the same day
    ("", "2027-12-31", "non è nota"),                          # never knew the appointment
    ("20/02/2027 - 10:00", "2027-01-10", "data di fine"),     # nothing left to search
])
def test_closed_search_cannot_continue(repo, two_users, db, appointment, end_date, reason):
    a, b = two_users
    search_id = booked(repo, db, a, appointment, end_date=end_date)
    assert reason in repo.continuable(row(db, search_id), today=date(2027, 1, 15))
    with pytest.raises(LimitReached, match=reason):
        repo.reopen(search_id, b, today=date(2027, 1, 15))
    assert repo.get(a, search_id).status == "booked"


def test_continue_respects_limits_and_running_searches(repo, two_users, db):
    a, b = two_users
    search_id = booked(repo, db, a, "20/02/2027 - 10:00")
    for _ in range(3):
        repo.create(a, settings(), SECRETS)
    with pytest.raises(LimitReached, match="al massimo"):
        repo.reopen(search_id, b, today=date(2027, 1, 15))
    active = next(v.id for v in repo.list_for_user(a) if v.status == "active")
    with pytest.raises(LimitReached, match="non è conclusa"):
        repo.reopen(active, b, today=date(2027, 1, 15))
    with pytest.raises(NotFound):
        repo.reopen("missing", b)


def test_expired_by_end_date_is_wiped_but_idle_one_can_continue(repo, two_users, db, clock):
    a, _ = two_users
    past_end = repo.create(a, settings(end_date="2027-01-10"), SECRETS)
    idle = repo.create(a, settings(end_date="2027-12-31"), SECRETS)
    for search_id in (past_end, idle):
        execute(db, "UPDATE searches SET current_appointment = '20/02/2027 - 10:00' WHERE id = ?", (search_id,))
    execute(db, "UPDATE searches SET updated_at = ? WHERE id = ?", (clock() - 61 * 86400, idle))
    assert sorted(repo.expire_finished(today=date(2027, 1, 15))) == sorted([past_end, idle])
    assert repo.secrets_for(past_end) is None
    assert repo.secrets_for(idle) == SECRETS
    assert repo.continuable(row(db, idle), today=date(2027, 1, 15)) == ""


def test_expire_past_end_date_and_idle(repo, two_users, clock, db):
    a, _ = two_users
    old = repo.create(a, settings(end_date="2026-10-10"), SECRETS)
    keep = repo.create(a, settings(end_date="2026-12-31"), SECRETS)
    expired = repo.expire_finished(today=date(2026, 10, 11))
    assert expired == [old]
    assert repo.get(a, old).status == "expired" and repo.secrets_for(old) is None
    assert repo.get(a, keep).status == "active"


def test_delete_removes_everything(repo, two_users, db):
    a, _ = two_users
    search_id = repo.create(a, settings(), SECRETS)
    db_store = repo.decisions(search_id)
    from datetime import datetime
    from rlprenota.core.models import Slot
    slot = Slot(datetime(2026, 11, 5, 8, 30), "ASST", "Ospedale", "MILANO", "MILANO CITTA'")
    db_store.record_proposal(slot, 7)
    db_store.discard(slot)
    repo.delete(a, search_id)
    assert repo.get(a, search_id) is None
    assert db.query("SELECT * FROM discarded") == [] and db.query("SELECT * FROM proposals") == []


def test_db_decision_store_behaves_like_the_file_store(repo, two_users):
    from datetime import datetime
    from rlprenota.core.decisions import ACCEPTED, EXPIRED
    from rlprenota.core.models import Slot
    a, _ = two_users
    search_id = repo.create(a, settings(), SECRETS)
    store = repo.decisions(search_id)
    slot = Slot(datetime(2026, 11, 5, 8, 30), "ASST", "Ospedale", "MILANO", "MILANO CITTA'")
    assert store.should_propose(slot)
    store.record_proposal(slot, 42)
    assert not store.should_propose(slot) and store.slot_id_for_message(42) == slot.slot_id
    store.set_status(slot.slot_id, EXPIRED)
    assert not store.should_propose(slot)
    assert repo.decisions(search_id).should_propose(slot)  # a new run (restart) may propose it again
    store.set_status(slot.slot_id, ACCEPTED)
    assert repo.decisions(search_id).is_pre_accepted(slot)
    store.discard(slot)
    reloaded = repo.decisions(search_id)
    assert reloaded.is_discarded(slot) and not reloaded.should_propose(slot)
    assert [d["slot_id"] for d in repo.discarded(a, search_id)] == [slot.slot_id]
    repo.undiscard(a, search_id, slot.slot_id)
    assert not repo.decisions(search_id).is_discarded(slot)


def test_a_postpone_search_ends_with_its_booking(repo, two_users, db):
    a, b = two_users
    search_id = repo.create(a, settings(end_date="2027-12-31", posticipa="on"), SECRETS)
    execute(db, "UPDATE searches SET current_appointment = '20/02/2027 - 10:00' WHERE id = ?", (search_id,))
    repo.finish(search_id, "booked", "Prenotato")
    assert "posticipare" in repo.continuable(row(db, search_id), today=date(2027, 1, 15))
    with pytest.raises(LimitReached):
        repo.reopen(search_id, b, today=date(2027, 1, 15))
