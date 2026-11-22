from concurrent.futures import ThreadPoolExecutor

import pytest
from test_activity import case

from milo_home.automatic_budget import AutomaticPolicy
from milo_home.database import Database
from milo_home.errors import ApiError
from milo_home.execution_pulse import identity_hash
from milo_home.management_lease import ManagementLease, current_boot, observe_management

POLICY = AutomaticPolicy(daily_launches=2, daily_seconds=120)


def lease_case(tmp_path):
    database = Database(tmp_path / "home.sqlite3")
    database.initialize()
    now = [100]
    lease = ManagementLease(database, POLICY, clock=lambda: now[0], monotonic=lambda: now[0])
    return database, lease, now


def view(database, now, boot=None):
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM management_lease").fetchone()
    return observe_management(row, now, current_boot() if boot is None else boot)


def test_claim_renew_expire_without_resurrection(tmp_path):
    database, lease, now = lease_case(tmp_path)
    assert view(database, now[0]) == ("unknown", "management_not_configured", None)
    lease.claim()
    assert view(database, now[0]) == ("enabled", "management_lease_current", 145)
    now[0] = 114
    assert lease.progress()
    assert view(database, now[0])[2] == 145
    now[0] = 115
    assert lease.progress()
    assert view(database, now[0])[2] == 160
    now[0] = 160
    assert view(database, now[0]) == ("unknown", "management_lease_expired", None)
    assert not lease.progress()
    now[0] = 116
    assert not lease.renew()
    assert not lease.progress()
    lease.close()
    assert view(database, now[0])[0] == "unknown"
    with pytest.raises(ValueError, match="only claim once"):
        lease.claim()
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT last_scan_at FROM wake_event_cursor").fetchone()[0] is None
        )


def test_clock_rewind_latches_and_close_cannot_hide_it(tmp_path):
    database, lease, now = lease_case(tmp_path)
    lease.claim()
    now[0] = 99
    assert view(database, now[0])[0] == "unknown"
    lease.close()
    now[0] = 101
    assert not lease.renew()
    assert view(database, now[0]) == ("unknown", "management_clock_or_boot_invalid", None)


def test_boot_change_invalidates_old_evidence_and_allows_new_identity(tmp_path):
    database, lease, now = lease_case(tmp_path)
    lease.claim()
    assert view(database, now[0], "synthetic-next-boot")[0] == "unknown"
    replacement = ManagementLease(
        database, POLICY, clock=lambda: now[0], boot="synthetic-next-boot"
    )
    replacement.claim()
    assert not lease.renew()
    lease.close()
    assert view(database, now[0], "synthetic-next-boot")[0] == "enabled"


def test_concurrent_claim_and_old_identity_cannot_renew_or_close_replacement(tmp_path):
    database, lease, now = lease_case(tmp_path)
    contenders = [lease] + [
        ManagementLease(database, POLICY, clock=lambda: now[0]) for _ in range(3)
    ]

    def claim(contender):
        try:
            contender.claim()
            return contender
        except ApiError as error:
            assert error.code == "management_busy"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = [item for item in pool.map(claim, contenders) if item is not None]
    assert len(claimed) == 1
    first = claimed[0]
    now[0] += 45
    replacement = ManagementLease(database, POLICY, clock=lambda: now[0])
    replacement.claim()
    assert not first.renew()
    first.close()
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM management_lease").fetchone()
    assert row["identity_hash"] == identity_hash(replacement.identity)
    assert row["invalid_reason"] is None and row["ended_at"] is None


def test_normal_close_is_bounded_and_new_session_may_start_immediately(tmp_path):
    database, lease, now = lease_case(tmp_path)
    lease.claim()
    now[0] += 2
    lease.close()
    assert view(database, now[0]) == ("stopped", "management_stopped", now[0] + 45)
    assert not lease.renew()
    assert view(database, now[0] + 45)[0] == "unknown"
    replacement = ManagementLease(database, POLICY, clock=lambda: now[0])
    replacement.claim()
    lease.close()
    assert view(database, now[0])[0] == "enabled"


@pytest.mark.parametrize("ending", [False, True])
def test_future_time_refuses_new_claim_even_after_close(tmp_path, ending):
    database, lease, now = lease_case(tmp_path)
    lease.claim()
    if ending:
        lease.close()
    now[0] -= 1
    with pytest.raises(ApiError, match="management_busy"):
        ManagementLease(database, POLICY, clock=lambda: now[0]).claim()


def test_owner_activity_exposes_only_status_and_bounds_validity(client):
    _, store, now, endpoint = case(client)
    lease = ManagementLease(store.database, POLICY, clock=store.clock)
    lease.claim()
    now[0] += 40
    response = client.get(endpoint)
    enabled = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert enabled["automatic_processing"] == "enabled"
    assert enabled["automatic_reason"] == "management_lease_current"
    assert enabled["processing"] == "idle"
    assert enabled["valid_until"] == now[0] + 5
    assert not {"policy_hash", "identity_hash", "boot_id", "started_at"} & enabled.keys()
    assert lease.identity not in str(enabled) and lease.policy_hash not in str(enabled)
    now[0] += 5
    assert client.get(endpoint).json()["automatic_reason"] == "management_lease_expired"
    assert not lease.renew()


def test_observation_is_readonly_and_cannot_renew_the_lease(client):
    _, store, now, endpoint = case(client)
    lease = ManagementLease(store.database, POLICY, clock=store.clock)
    lease.claim()
    with store.database.connect() as connection:
        before = dict(connection.execute("SELECT * FROM management_lease").fetchone())
    now[0] += 10
    for _ in range(3):
        assert client.get(endpoint).json()["automatic_processing"] == "enabled"
    with store.database.connect() as connection:
        assert dict(connection.execute("SELECT * FROM management_lease").fetchone()) == before
