"""Secretary feature: invite tokens, ownership resolution, audit trail.

A "secretary" is someone an owner has invited to operate the bot on their
behalf. The secretary's chat_id is mapped to the owner's chat_id via the
Secretary table. Everything they create lands on the owner's data and
reminder pings go to the owner.

Access model:
  resolve_owner(chat_id) -> owner_chat_id | None
    - chat_id in allowed_chat_ids  → returns chat_id itself (owner short-circuit)
    - active Secretary row         → returns owner_chat_id
    - else                         → None (silent reject)

Owner-self always wins so a misconfigured Secretary row can never demote
an owner into someone else's assistant.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from database import Invite, Secretary, SecretaryEvent

logger = logging.getLogger(__name__)

INVITE_TTL_HOURS = 24
MAX_ACTIVE_INVITES_PER_OWNER = 10
INVITE_TOKEN_PREFIX = "invite_"


@dataclass
class OwnerContext:
    """Result of resolve_owner: who's the owner, and who is acting."""

    owner_chat_id: str           # whose data this affects
    actor_chat_id: str           # the human pressing keys
    is_secretary: bool           # actor != owner
    secretary: Optional[Secretary] = None  # the Secretary row, if is_secretary


# ── Ownership resolution ─────────────────────────────────────────────────


def resolve_owner(
    session: Session,
    chat_id: str,
    allowed_chat_ids: set[int],
) -> Optional[OwnerContext]:
    """Decide whether chat_id is allowed and on whose data they operate.

    Precedence: owner self-identity wins. A chat_id in allowed_chat_ids is
    always treated as their own owner, even if a Secretary row exists for
    them (which shouldn't happen — we block that at invite-accept).
    """
    if not allowed_chat_ids or int(chat_id) in allowed_chat_ids:
        return OwnerContext(
            owner_chat_id=chat_id,
            actor_chat_id=chat_id,
            is_secretary=False,
        )

    sec = (
        session.query(Secretary)
        .filter(
            Secretary.secretary_chat_id == chat_id,
            Secretary.removed_at.is_(None),
        )
        .one_or_none()
    )
    if sec is None:
        return None

    return OwnerContext(
        owner_chat_id=sec.owner_chat_id,
        actor_chat_id=chat_id,
        is_secretary=True,
        secretary=sec,
    )


def is_owner(chat_id: str, allowed_chat_ids: set[int]) -> bool:
    return bool(allowed_chat_ids) and int(chat_id) in allowed_chat_ids


# ── Display name caching ─────────────────────────────────────────────────


def refresh_display_name(
    session: Session,
    chat_id: str,
    new_name: Optional[str],
) -> None:
    """Update the cached display name on a Secretary row when Telegram
    reports a different first_name. No-op if no row, no name, or no change."""
    if not new_name:
        return
    sec = (
        session.query(Secretary)
        .filter(
            Secretary.secretary_chat_id == chat_id,
            Secretary.removed_at.is_(None),
        )
        .one_or_none()
    )
    if sec is None or sec.display_name == new_name:
        return
    sec.display_name = new_name


def display_name_for(session: Session, chat_id: str) -> Optional[str]:
    """Latest known display name for chat_id, even from a revoked row."""
    if chat_id is None:
        return None
    sec = (
        session.query(Secretary)
        .filter(Secretary.secretary_chat_id == chat_id)
        .order_by(Secretary.invited_at.desc())
        .first()
    )
    return sec.display_name if sec else None


def format_actor(session: Session, actor_chat_id: Optional[str], owner_chat_id: str) -> str:
    """Render an actor for display in cards / lists.

    Returns:
      ""               → the owner is the actor (no "by you" noise)
      "Alice"          → active secretary
      "Alice (former)" → revoked secretary
      "unknown"        → no Secretary row anywhere (legacy data with NULL actor)
    """
    if actor_chat_id is None or actor_chat_id == owner_chat_id:
        return ""
    sec = (
        session.query(Secretary)
        .filter(Secretary.secretary_chat_id == actor_chat_id)
        .order_by(Secretary.invited_at.desc())
        .first()
    )
    if sec is None:
        return "unknown"
    name = sec.display_name or f"…{actor_chat_id[-4:]}"
    if sec.removed_at is not None:
        return f"{name} (former)"
    return name


# ── Invite lifecycle ─────────────────────────────────────────────────────


class InviteError(Exception):
    """Raised when an invite operation is rejected for a business reason."""


def _active_invites(session: Session, owner_chat_id: str) -> list[Invite]:
    now = datetime.utcnow()
    return (
        session.query(Invite)
        .filter(
            Invite.owner_chat_id == owner_chat_id,
            Invite.used_by_chat_id.is_(None),
            Invite.expires_at > now,
        )
        .all()
    )


def _active_secretary_count(session: Session, owner_chat_id: str) -> int:
    return (
        session.query(Secretary)
        .filter(
            Secretary.owner_chat_id == owner_chat_id,
            Secretary.removed_at.is_(None),
        )
        .count()
    )


def create_invite(
    session: Session,
    owner_chat_id: str,
    *,
    max_secretaries: int,
    max_active_invites: int = MAX_ACTIVE_INVITES_PER_OWNER,
    ttl_hours: int = INVITE_TTL_HOURS,
) -> Invite:
    """Generate a new invite. Caller is responsible for is-owner check and
    for committing the session. Raises InviteError on caps."""
    if _active_secretary_count(session, owner_chat_id) >= max_secretaries:
        raise InviteError(
            f"You already have {max_secretaries} active secretaries. "
            "Revoke one before inviting another."
        )
    active = _active_invites(session, owner_chat_id)
    if len(active) >= max_active_invites:
        raise InviteError(
            f"You already have {len(active)} pending invites. "
            "Wait for one to expire or be used."
        )

    token = INVITE_TOKEN_PREFIX + secrets.token_urlsafe(16)
    expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
    invite = Invite(
        token=token,
        owner_chat_id=owner_chat_id,
        expires_at=expires_at,
    )
    session.add(invite)
    log_event(session, "invite_created", owner_chat_id, owner_chat_id,
              payload={"expires_at": expires_at.isoformat()})
    return invite


def accept_invite(
    session: Session,
    token: str,
    secretary_chat_id: str,
    secretary_display_name: Optional[str],
    allowed_chat_ids: set[int],
) -> Secretary:
    """Redeem a token. Raises InviteError on any failure (unknown / expired /
    used / self-invite / would-violate-1:1). Caller commits the session."""
    invite = session.query(Invite).filter(Invite.token == token).one_or_none()
    if invite is None:
        raise InviteError("Unknown or invalid invite link.")
    if invite.used_by_chat_id is not None:
        raise InviteError("This invite has already been used.")
    if invite.expires_at <= datetime.utcnow():
        raise InviteError("This invite link has expired.")

    if is_owner(secretary_chat_id, allowed_chat_ids):
        raise InviteError("Owners cannot accept invites.")

    if secretary_chat_id == invite.owner_chat_id:
        raise InviteError("You cannot invite yourself.")

    # 1:1 — reject if this user is already an active secretary somewhere.
    existing = (
        session.query(Secretary)
        .filter(
            Secretary.secretary_chat_id == secretary_chat_id,
            Secretary.removed_at.is_(None),
        )
        .one_or_none()
    )
    if existing is not None:
        if existing.owner_chat_id == invite.owner_chat_id:
            raise InviteError("You're already this owner's secretary.")
        raise InviteError(
            f"You're already a secretary for chat {existing.owner_chat_id}. "
            "Run /leave first, then accept this invite."
        )

    sec = Secretary(
        owner_chat_id=invite.owner_chat_id,
        secretary_chat_id=secretary_chat_id,
        display_name=secretary_display_name,
    )
    session.add(sec)
    invite.used_by_chat_id = secretary_chat_id
    invite.used_at = datetime.utcnow()

    log_event(session, "invite_accepted", invite.owner_chat_id, secretary_chat_id,
              payload={"display_name": secretary_display_name})
    return sec


def revoke_secretary(
    session: Session,
    owner_chat_id: str,
    secretary_chat_id: str,
) -> Secretary:
    """Owner removes a secretary. Soft-delete: row stays so audit lookups
    work, but the partial unique index frees up the slot for a fresh
    invitation. Caller commits."""
    sec = (
        session.query(Secretary)
        .filter(
            Secretary.owner_chat_id == owner_chat_id,
            Secretary.secretary_chat_id == secretary_chat_id,
            Secretary.removed_at.is_(None),
        )
        .one_or_none()
    )
    if sec is None:
        raise InviteError("That secretary is not active under your account.")
    sec.removed_at = datetime.utcnow()
    sec.removed_by_chat_id = owner_chat_id
    log_event(session, "revoked", owner_chat_id, owner_chat_id,
              payload={"secretary_chat_id": secretary_chat_id})
    return sec


def leave_workspace(session: Session, secretary_chat_id: str) -> Secretary:
    """Secretary leaves voluntarily. Soft-delete, owner notified by caller."""
    sec = (
        session.query(Secretary)
        .filter(
            Secretary.secretary_chat_id == secretary_chat_id,
            Secretary.removed_at.is_(None),
        )
        .one_or_none()
    )
    if sec is None:
        raise InviteError("You're not currently a secretary.")
    sec.removed_at = datetime.utcnow()
    sec.removed_by_chat_id = secretary_chat_id
    log_event(session, "left", sec.owner_chat_id, secretary_chat_id, payload=None)
    return sec


def list_secretaries(session: Session, owner_chat_id: str) -> list[Secretary]:
    return (
        session.query(Secretary)
        .filter(
            Secretary.owner_chat_id == owner_chat_id,
            Secretary.removed_at.is_(None),
        )
        .order_by(Secretary.invited_at)
        .all()
    )


# ── Permission gates ─────────────────────────────────────────────────────


def can_act_on_reminder(
    ctx: OwnerContext,
    thought_created_by: Optional[str],
) -> bool:
    """Whether ctx.actor may mark-done / snooze / delete this reminder.

    Rules:
      - Owner acting on their own data: always yes.
      - Secretary: only if they created this reminder (and they're still
        an active secretary — which is implied by ctx existing at all).
    """
    if not ctx.is_secretary:
        return True
    return thought_created_by == ctx.actor_chat_id


# ── Audit log ────────────────────────────────────────────────────────────


def log_event(
    session: Session,
    kind: str,
    owner_chat_id: str,
    actor_chat_id: str,
    payload: Optional[dict] = None,
) -> SecretaryEvent:
    ev = SecretaryEvent(
        kind=kind,
        owner_chat_id=owner_chat_id,
        actor_chat_id=actor_chat_id,
        payload=json.dumps(payload) if payload else None,
    )
    session.add(ev)
    return ev
