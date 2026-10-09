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
    repo.finish(search_id, "booked", "Prenotato il 05/11/2026 08:30")
    assert repo.secrets_for(search_id) is None
    view = repo.get(a, search_id)
    assert view.status == "booked" and "05/11/2026" in view.last_result
    with pytest.raises(LimitReached):  # can't resume without data
        repo.resume(a, search_id)


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
