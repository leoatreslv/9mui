"""Secretary feature tests.

Covers: ownership resolution, invite lifecycle, 1:1 enforcement,
permission gates, display name caching, audit log, and the migration
helper.
"""

from datetime import datetime, timedelta

import pytest

from database import (
    Invite,
    Opportunity,
    OpportunityUpdate,
    Reminder,
    Secretary,
    SecretaryEvent,
    Thought,
)
from secretary import (
    INVITE_TOKEN_PREFIX,
    InviteError,
    OwnerContext,
    accept_invite,
    can_act_on_reminder,
    create_invite,
    display_name_for,
    format_actor,
    is_owner,
    leave_workspace,
    list_secretaries,
    refresh_display_name,
    resolve_owner,
    revoke_secretary,
)


OWNER = "111"
SEC = "222"
OTHER_OWNER = "333"
STRANGER = "999"
ALLOWED = {int(OWNER), int(OTHER_OWNER)}


# ── resolve_owner precedence ──────────────────────────────────────────────


def test_resolve_owner_for_owner_returns_self(db_session):
    ctx = resolve_owner(db_session, OWNER, ALLOWED)
    assert ctx is not None
    assert ctx.owner_chat_id == OWNER
    assert ctx.actor_chat_id == OWNER
    assert ctx.is_secretary is False


def test_resolve_owner_for_stranger_returns_none(db_session):
    assert resolve_owner(db_session, STRANGER, ALLOWED) is None


def test_resolve_owner_for_active_secretary(db_session):
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC,
                             display_name="Alice"))
    db_session.commit()
    ctx = resolve_owner(db_session, SEC, ALLOWED)
    assert ctx is not None
    assert ctx.owner_chat_id == OWNER
    assert ctx.actor_chat_id == SEC
    assert ctx.is_secretary is True


def test_resolve_owner_ignores_removed_secretary(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER,
        secretary_chat_id=SEC,
        removed_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert resolve_owner(db_session, SEC, ALLOWED) is None


def test_resolve_owner_self_wins_over_secretary_row(db_session):
    """An owner who also happens to have a Secretary row is always
    treated as themselves — owner precedence."""
    db_session.add(Secretary(
        owner_chat_id=OTHER_OWNER,
        secretary_chat_id=OWNER,
    ))
    db_session.commit()
    ctx = resolve_owner(db_session, OWNER, ALLOWED)
    assert ctx.owner_chat_id == OWNER
    assert ctx.is_secretary is False


# ── Invite lifecycle ──────────────────────────────────────────────────────


def test_create_invite_basic(db_session):
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()
    assert inv.token.startswith(INVITE_TOKEN_PREFIX)
    assert inv.owner_chat_id == OWNER
    assert inv.used_by_chat_id is None
    assert inv.expires_at > datetime.utcnow()


def test_create_invite_blocks_when_at_secretary_cap(db_session):
    """Cap on existing active secretaries blocks new invite."""
    for i in range(5):
        db_session.add(Secretary(
            owner_chat_id=OWNER, secretary_chat_id=f"40{i}",
        ))
    db_session.commit()
    with pytest.raises(InviteError, match="already have"):
        create_invite(db_session, OWNER, max_secretaries=5)


def test_create_invite_blocks_when_pending_cap_hit(db_session):
    for _ in range(10):
        create_invite(db_session, OWNER, max_secretaries=5, max_active_invites=10)
    db_session.commit()
    with pytest.raises(InviteError, match="pending invites"):
        create_invite(db_session, OWNER, max_secretaries=5, max_active_invites=10)


def test_invite_tokens_are_unique_across_invocations(db_session):
    tokens = set()
    for _ in range(20):
        inv = create_invite(db_session, OWNER, max_secretaries=100, max_active_invites=100)
        tokens.add(inv.token)
    db_session.commit()
    assert len(tokens) == 20


def test_accept_invite_happy_path(db_session):
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()

    sec = accept_invite(
        session=db_session,
        token=inv.token,
        secretary_chat_id=SEC,
        secretary_display_name="Alice",
        allowed_chat_ids=ALLOWED,
    )
    db_session.commit()

    assert sec.owner_chat_id == OWNER
    assert sec.secretary_chat_id == SEC
    assert sec.display_name == "Alice"
    # Invite is marked used
    refreshed = db_session.get(Invite, inv.token)
    assert refreshed.used_by_chat_id == SEC
    assert refreshed.used_at is not None


def test_accept_invite_rejects_second_use(db_session):
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()
    accept_invite(db_session, inv.token, SEC, "Alice", ALLOWED)
    db_session.commit()

    with pytest.raises(InviteError, match="already been used"):
        accept_invite(db_session, inv.token, "444", "Bob", ALLOWED)


def test_accept_invite_rejects_expired_token(db_session):
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    # Force-expire
    inv.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(InviteError, match="expired"):
        accept_invite(db_session, inv.token, SEC, "Alice", ALLOWED)


def test_accept_invite_rejects_unknown_token(db_session):
    with pytest.raises(InviteError, match="Unknown"):
        accept_invite(db_session, "invite_garbage", SEC, "Alice", ALLOWED)


def test_accept_invite_rejects_owner_self_invite(db_session):
    """Owners cannot accept invites at all (their own or anyone else's) —
    the owner gate fires before the self-invite check."""
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()
    with pytest.raises(InviteError, match="Owners cannot"):
        accept_invite(db_session, inv.token, OWNER, "Self", ALLOWED)


def test_accept_invite_rejects_self_invite_for_non_owner_acceptor(db_session):
    """Self-invite check fires when the acceptor isn't an owner but
    matches invite.owner_chat_id. Edge case — covers the explicit branch."""
    # Construct a manual invite from a non-allowed chat_id
    inv = Invite(
        token="invite_xyz",
        owner_chat_id="888",   # not in ALLOWED
        expires_at=datetime.utcnow() + timedelta(hours=1),
    )
    db_session.add(inv)
    db_session.commit()
    with pytest.raises(InviteError, match="cannot invite yourself"):
        accept_invite(db_session, "invite_xyz", "888", "Self", ALLOWED)


def test_accept_invite_rejects_when_acceptor_is_another_owner(db_session):
    """OTHER_OWNER is in allowed_chat_ids — they can't be a secretary."""
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()
    with pytest.raises(InviteError, match="Owners cannot"):
        accept_invite(db_session, inv.token, OTHER_OWNER, "Owner2", ALLOWED)


def test_accept_invite_enforces_one_workspace_per_secretary(db_session):
    """If Alice is already a secretary for OWNER, she can't accept
    another owner's invite without leaving first."""
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC))
    db_session.commit()

    inv = create_invite(db_session, OTHER_OWNER, max_secretaries=5)
    db_session.commit()
    with pytest.raises(InviteError, match="/leave"):
        accept_invite(db_session, inv.token, SEC, "Alice", ALLOWED)


def test_re_accept_after_revoke_allowed_via_partial_unique_index(db_session):
    """After a secretary is revoked, the same chat_id can be invited again."""
    db_session.add(Secretary(
        owner_chat_id=OWNER,
        secretary_chat_id=SEC,
        removed_at=datetime.utcnow(),
    ))
    db_session.commit()
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()

    sec = accept_invite(db_session, inv.token, SEC, "Alice", ALLOWED)
    db_session.commit()
    assert sec.removed_at is None


# ── Revoke / leave ────────────────────────────────────────────────────────


def test_revoke_soft_deletes(db_session):
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC))
    db_session.commit()
    sec = revoke_secretary(db_session, OWNER, SEC)
    db_session.commit()
    assert sec.removed_at is not None
    assert sec.removed_by_chat_id == OWNER


def test_revoke_only_matches_own_secretary(db_session):
    db_session.add(Secretary(owner_chat_id=OTHER_OWNER, secretary_chat_id=SEC))
    db_session.commit()
    with pytest.raises(InviteError, match="not active"):
        revoke_secretary(db_session, OWNER, SEC)


def test_leave_soft_deletes(db_session):
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC))
    db_session.commit()
    sec = leave_workspace(db_session, SEC)
    db_session.commit()
    assert sec.removed_at is not None
    assert sec.removed_by_chat_id == SEC


# ── Permission gate ──────────────────────────────────────────────────────


def test_owner_can_act_on_any_reminder():
    ctx = OwnerContext(owner_chat_id=OWNER, actor_chat_id=OWNER, is_secretary=False)
    assert can_act_on_reminder(ctx, None) is True
    assert can_act_on_reminder(ctx, SEC) is True
    assert can_act_on_reminder(ctx, OWNER) is True


def test_secretary_can_act_only_on_own_reminders():
    ctx = OwnerContext(owner_chat_id=OWNER, actor_chat_id=SEC, is_secretary=True)
    assert can_act_on_reminder(ctx, SEC) is True   # their creation
    assert can_act_on_reminder(ctx, OWNER) is False  # the owner's
    assert can_act_on_reminder(ctx, "444") is False  # someone else's
    assert can_act_on_reminder(ctx, None) is False   # legacy / unknown


# ── Display name caching ─────────────────────────────────────────────────


def test_refresh_display_name_updates_when_changed(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER, secretary_chat_id=SEC, display_name="Alice",
    ))
    db_session.commit()
    refresh_display_name(db_session, SEC, "Alicia")
    db_session.commit()
    db_session.expire_all()
    sec = db_session.query(Secretary).filter(
        Secretary.secretary_chat_id == SEC
    ).first()
    assert sec.display_name == "Alicia"


def test_refresh_display_name_noop_when_unchanged(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER, secretary_chat_id=SEC, display_name="Alice",
    ))
    db_session.commit()
    refresh_display_name(db_session, SEC, "Alice")
    # No exception; the row remains unchanged
    sec = db_session.query(Secretary).filter(
        Secretary.secretary_chat_id == SEC
    ).first()
    assert sec.display_name == "Alice"


def test_format_actor_owner_returns_empty(db_session):
    assert format_actor(db_session, OWNER, OWNER) == ""


def test_format_actor_active_secretary(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER, secretary_chat_id=SEC, display_name="Alice",
    ))
    db_session.commit()
    assert format_actor(db_session, SEC, OWNER) == "Alice"


def test_format_actor_revoked_secretary(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER,
        secretary_chat_id=SEC,
        display_name="Alice",
        removed_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert format_actor(db_session, SEC, OWNER) == "Alice (former)"


def test_format_actor_unknown(db_session):
    assert format_actor(db_session, "55555", OWNER) == "unknown"


# ── Audit log ────────────────────────────────────────────────────────────


def test_invite_lifecycle_emits_audit_events(db_session):
    inv = create_invite(db_session, OWNER, max_secretaries=5)
    db_session.commit()
    accept_invite(db_session, inv.token, SEC, "Alice", ALLOWED)
    db_session.commit()
    revoke_secretary(db_session, OWNER, SEC)
    db_session.commit()

    kinds = [e.kind for e in db_session.query(SecretaryEvent).order_by(SecretaryEvent.id).all()]
    assert kinds == ["invite_created", "invite_accepted", "revoked"]


def test_leave_emits_left_event(db_session):
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id=SEC))
    db_session.commit()
    leave_workspace(db_session, SEC)
    db_session.commit()

    events = db_session.query(SecretaryEvent).filter(
        SecretaryEvent.kind == "left"
    ).all()
    assert len(events) == 1
    assert events[0].actor_chat_id == SEC
    assert events[0].owner_chat_id == OWNER


# ── Helpers ──────────────────────────────────────────────────────────────


def test_is_owner_helper():
    assert is_owner(OWNER, ALLOWED) is True
    assert is_owner(SEC, ALLOWED) is False
    assert is_owner(STRANGER, ALLOWED) is False


def test_list_secretaries_returns_only_active(db_session):
    db_session.add(Secretary(owner_chat_id=OWNER, secretary_chat_id="A"))
    db_session.add(Secretary(
        owner_chat_id=OWNER, secretary_chat_id="B",
        removed_at=datetime.utcnow(),
    ))
    db_session.add(Secretary(owner_chat_id=OTHER_OWNER, secretary_chat_id="C"))
    db_session.commit()

    secs = list_secretaries(db_session, OWNER)
    chat_ids = {s.secretary_chat_id for s in secs}
    assert chat_ids == {"A"}


def test_display_name_for_returns_latest_even_if_removed(db_session):
    db_session.add(Secretary(
        owner_chat_id=OWNER,
        secretary_chat_id=SEC,
        display_name="Alice",
        removed_at=datetime.utcnow(),
    ))
    db_session.commit()
    assert display_name_for(db_session, SEC) == "Alice"
