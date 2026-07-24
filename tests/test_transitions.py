"""State machine: only the declared transitions are possible, and they are atomic."""

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import models, service
from tests.conftest import do_setup, make_user, session_for


def _request(user_id: int, status: str = models.PENDING, **overrides) -> models.Request:
    request, _ = models.create(
        source="streamingcommunity",
        media_type="film",
        external_id=overrides.pop("external_id", "1"),
        title="Film",
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


ILLEGAL = [
    (models.COMPLETED, models.APPROVED),
    (models.COMPLETED, models.PENDING),
    (models.DENIED, models.APPROVED),
    (models.DENIED, models.DOWNLOADING),
    (models.CANCELLED, models.APPROVED),
    (models.FAILED, models.DOWNLOADING),
    (models.AVAILABLE, models.APPROVED),
    (models.PENDING, models.DOWNLOADING),
    (models.PENDING, models.COMPLETED),
    (models.NEEDS_ATTENTION, models.DOWNLOADING),
    (models.DOWNLOADING, models.APPROVED),
]


@pytest.mark.parametrize("start,target", ILLEGAL, ids=[f"{a}->{b}" for a, b in ILLEGAL])
def test_illegal_transitions_are_refused(bob, start, target):
    request = _request(bob.id, status=start, external_id=f"{start}-{target}")

    assert models.transition(request.id, target) is None
    assert models.get(request.id).status == start


LEGAL = [
    (models.PENDING, models.APPROVED),
    (models.PENDING, models.DENIED),
    (models.PENDING, models.CANCELLED),
    (models.PENDING, models.NEEDS_ATTENTION),
    (models.PENDING, models.AVAILABLE),
    (models.APPROVED, models.DOWNLOADING),
    (models.APPROVED, models.NEEDS_ATTENTION),
    (models.APPROVED, models.COMPLETED),
    (models.DOWNLOADING, models.COMPLETED),
    (models.DOWNLOADING, models.FAILED),
    (models.NEEDS_ATTENTION, models.APPROVED),
    (models.NEEDS_ATTENTION, models.DENIED),
]


@pytest.mark.parametrize("start,target", LEGAL, ids=[f"{a}->{b}" for a, b in LEGAL])
def test_legal_transitions_are_allowed(bob, start, target):
    request = _request(bob.id, status=start, external_id=f"{start}-{target}")

    assert models.transition(request.id, target) is not None
    assert models.get(request.id).status == target


def test_terminal_states_have_no_way_out(bob):
    for state in models.TERMINAL_STATUSES:
        assert models.ALLOWED_TRANSITIONS[state] == ()


def test_approving_twice_only_wins_once(client, admin_credentials, source, stub_jobs):
    """Two approvers clicking together must not start two downloads."""
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    boss = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    request = _request(bob.id)

    service.approve(request.id, boss.id)
    with pytest.raises(models.InvalidTransition):
        service.approve(request.id, boss.id)

    assert len(stub_jobs) == 1


def test_a_request_no_longer_approved_does_not_start_downloading(
    client, admin_credentials, source, stub_jobs
):
    """The claim from approved to downloading is what a second worker loses."""
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    request = _request(bob.id, status=models.CANCELLED)

    service._execute(request.id)

    assert models.get(request.id).status == models.CANCELLED
    assert stub_jobs == []


def test_denying_an_approved_request_is_refused(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    boss = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    request = _request(bob.id)
    service.approve(request.id, boss.id)

    with pytest.raises(models.InvalidTransition):
        service.deny(request.id, boss.id, "troppo tardi")


def test_transition_reports_the_state_it_refused_from(bob):
    request = _request(bob.id, status=models.COMPLETED)

    with pytest.raises(models.InvalidTransition) as caught:
        models.require_transition(request.id, models.APPROVED)

    assert caught.value.current == models.COMPLETED
    assert caught.value.target == models.APPROVED
