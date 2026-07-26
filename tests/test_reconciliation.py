"""Startup reconciliation of requests orphaned by a crash or a restart.

Both the resolution pool and the job manager start empty on every launch, so
a row still sitting in `approved` or `downloading` at startup has definitely
lost whatever in-memory worker was handling it — nothing else ever notices
that on its own, and until this runs such a row blocks the same content from
being requested again forever.
"""

import pytest

from app.auth.permissions import Permission
from app.requests import models, notify, service
from tests.conftest import do_setup, make_user


def _request(user_id: int, status: str, **overrides) -> models.Request:
    """Same helper as test_transitions.py: create pending, then force a status
    directly — bypassing the state machine, the same way a crash would leave a
    row mid-flight without going through a legal transition to get there."""
    request, _ = models.create(
        source="streamingcommunity",
        media_type=overrides.pop("media_type", "film"),
        external_id=overrides.pop("external_id", "1"),
        title=overrides.pop("title", "Film"),
        audio_languages=["ita"],
        subtitle_languages=[],
        requested_by=user_id,
        **overrides,
    )
    if status != models.PENDING:
        models.update_fields(request.id, status=status)
        request = models.get(request.id)
    return request


@pytest.fixture
def bob(client, admin_credentials):
    do_setup(client, admin_credentials)
    return make_user("bob", "jf-bob-id", int(Permission.REQUEST))


@pytest.fixture
def approver(client, bob):
    from app.auth.permissions import ALL_PERMISSIONS
    return make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))


def test_an_orphaned_approved_request_is_parked_for_a_human(bob):
    request = _request(bob.id, models.APPROVED, external_id="1")

    recovered = service.reconcile_orphaned_requests()

    assert recovered == 1
    updated = models.get(request.id)
    assert updated.status == models.NEEDS_ATTENTION
    assert updated.problem.startswith("interrotta")


def test_an_orphaned_downloading_request_becomes_failed_not_needs_attention(bob):
    """The state machine has no path from downloading into needs_attention —
    there is no in-memory job left to resume, so failed is the honest state."""
    request = _request(bob.id, models.DOWNLOADING, external_id="1")

    service.reconcile_orphaned_requests()

    updated = models.get(request.id)
    assert updated.status == models.FAILED
    assert updated.problem.startswith("interrotta")


def test_reconciliation_leaves_pending_requests_alone(bob):
    """Nothing was ever in memory for a request nobody has approved yet."""
    request = _request(bob.id, models.PENDING, external_id="1")

    recovered = service.reconcile_orphaned_requests()

    assert recovered == 0
    assert models.get(request.id).status == models.PENDING


def test_reconciliation_leaves_needs_attention_requests_alone(bob):
    """Already parked for a human — nothing to recover."""
    request = _request(bob.id, models.NEEDS_ATTENTION, external_id="1")

    recovered = service.reconcile_orphaned_requests()

    assert recovered == 0
    assert models.get(request.id).status == models.NEEDS_ATTENTION


@pytest.mark.parametrize("status", [
    models.COMPLETED, models.DENIED, models.FAILED, models.CANCELLED, models.AVAILABLE,
])
def test_reconciliation_leaves_terminal_requests_alone(bob, status):
    request = _request(bob.id, status, external_id="1")

    recovered = service.reconcile_orphaned_requests()

    assert recovered == 0
    assert models.get(request.id).status == status


def test_recovery_unblocks_a_fresh_identical_request(bob):
    """This is the actual user-facing bug: without reconciliation, an orphaned
    approved/downloading row holds the content key forever and a person can
    never ask for the same thing again."""
    stuck = _request(bob.id, models.DOWNLOADING, external_id="1")

    service.reconcile_orphaned_requests()

    again, created = models.create(
        source="streamingcommunity", media_type="film", external_id="1", title="Film",
        audio_languages=["ita"], subtitle_languages=[], requested_by=bob.id,
    )
    assert created is True
    assert again.id != stuck.id


def test_reconciliation_notifies_approvers_and_subscribers(bob, approver):
    request = _request(bob.id, models.APPROVED, external_id="1")

    service.reconcile_orphaned_requests()

    assert any(
        n["event"] == notify.REQUEST_NEEDS_ATTENTION for n in notify.list_for_user(bob.id)
    )
    assert any(
        n["event"] == notify.REQUEST_NEEDS_ATTENTION for n in notify.list_for_user(approver.id)
    )


def test_reconciliation_handles_several_orphans_of_both_kinds(bob):
    approved_one = _request(bob.id, models.APPROVED, external_id="1")
    approved_two = _request(bob.id, models.APPROVED, external_id="2")
    downloading_one = _request(bob.id, models.DOWNLOADING, external_id="3")

    recovered = service.reconcile_orphaned_requests()

    assert recovered == 3
    assert models.get(approved_one.id).status == models.NEEDS_ATTENTION
    assert models.get(approved_two.id).status == models.NEEDS_ATTENTION
    assert models.get(downloading_one.id).status == models.FAILED


def test_lifespan_calls_reconciliation_before_serving_requests(monkeypatch):
    """Wired into app.main so a real restart is covered, not just the unit
    under test — checked by monkeypatching the call rather than running the
    real lifespan, which also starts the schedule loop and job manager."""
    import app.main as main_module

    calls = []
    monkeypatch.setattr(
        main_module.requests_service, "reconcile_orphaned_requests", lambda: calls.append(1)
    )
    monkeypatch.setattr(main_module.db, "run_migrations", lambda: None)
    monkeypatch.setattr(main_module.auth_session, "purge_expired", lambda: None)
    monkeypatch.setattr(main_module.requests_service, "register_job_listener", lambda: None)
    monkeypatch.setattr(main_module, "ScheduleStore", lambda path: type(
        "S", (), {"list_all": lambda self: []}
    )())
    monkeypatch.setattr(main_module.job_manager, "set_schedule_store", lambda store: None)
    monkeypatch.setattr(main_module.job_manager, "load_scheduled_from_store", lambda: None)
    monkeypatch.setattr(main_module.job_manager, "set_loop", lambda loop: None)

    import asyncio

    async def _run():
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(_run())

    assert calls == [1]
