"""Sales-opportunity feature: parser, service, and formatters.

All DB queries here are scoped to chat_id so two allowed users can't
mutate each other's opportunities. Soft-delete is used (deleted_at
column) so an accidental /opp delete is recoverable.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pytz
from sqlalchemy.orm import Session

from database import Opportunity, OpportunityUpdate

STAGES: tuple[str, ...] = ("lead", "qualified", "proposal", "won", "lost")

# What "▶ Advance" does. won/lost are terminal — no further advance.
STAGE_ADVANCE: dict[str, str] = {
    "lead": "qualified",
    "qualified": "proposal",
    "proposal": "won",
}

# Telegram message hard cap
MAX_MSG_LEN = 4000


# ── Parser ───────────────────────────────────────────────────────────────

@dataclass
class OppAction:
    kind: str  # help | new | update | stage | show | list | export | delete | invalid
    title: Optional[str] = None
    customer: Optional[str] = None
    opp_id: Optional[int] = None
    note: Optional[str] = None
    stage: Optional[str] = None
    filter_stage: Optional[str] = None       # for list
    list_format: str = "cards"               # cards | md
    export_format: str = "csv"
    error: Optional[str] = None


# Mobile keyboards (iOS especially) autocorrect "--" to an em-dash "—",
# and sometimes en-dash "–". Accept all three so /opp new ... doesn't
# silently fold the flag into the title.
_CUSTOMER_RE = re.compile(r"\s*(?:--|—|–)\s*customer\s*=\s*(.*)$", re.DOTALL)


def parse_opp_command(text: str) -> OppAction:
    """Parse a `/opp ...` command into an OppAction.

    Subcommand is the first whitespace-separated token after `/opp`.
    Everything after the subcommand's parsed positional args is treated
    verbatim — so a note can contain numbers, pipes, quotes, anything.
    """
    body = text.strip()
    if body.startswith("/opp"):
        body = body[len("/opp"):].strip()

    if not body:
        return OppAction(kind="help")

    parts = body.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    if sub in ("help", "?"):
        return OppAction(kind="help")

    if sub == "new":
        if not rest:
            return OppAction(kind="invalid", error="Usage: /opp new <title> [--customer=<name>]")
        m = _CUSTOMER_RE.search(rest)
        if m:
            customer = m.group(1).strip() or None
            title = rest[: m.start()].strip()
        else:
            customer = None
            title = rest.strip()
        if not title:
            return OppAction(kind="invalid", error="Title cannot be empty.")
        return OppAction(kind="new", title=title, customer=customer)

    if sub == "update":
        # First whitespace-separated token of rest must be the id.
        # Everything after that is the note, verbatim.
        m = re.match(r"\s*(\d+)\s+(.+)", rest, re.DOTALL)
        if not m:
            return OppAction(kind="invalid", error="Usage: /opp update <id> <note>")
        opp_id = int(m.group(1))
        note = m.group(2).strip()
        if not note:
            return OppAction(kind="invalid", error="Update note cannot be empty.")
        return OppAction(kind="update", opp_id=opp_id, note=note)

    if sub == "stage":
        m = re.match(r"\s*(\d+)\s+(\S+)\s*$", rest)
        if not m:
            return OppAction(
                kind="invalid",
                error=f"Usage: /opp stage <id> <{ '|'.join(STAGES) }>",
            )
        opp_id = int(m.group(1))
        stage = m.group(2).lower()
        if stage not in STAGES:
            return OppAction(
                kind="invalid",
                error=f"Unknown stage '{stage}'. Allowed: {', '.join(STAGES)}.",
            )
        return OppAction(kind="stage", opp_id=opp_id, stage=stage)

    if sub == "show":
        m = re.match(r"\s*(\d+)\s*$", rest)
        if not m:
            return OppAction(kind="invalid", error="Usage: /opp show <id>")
        return OppAction(kind="show", opp_id=int(m.group(1)))

    if sub == "delete":
        m = re.match(r"\s*(\d+)\s*$", rest)
        if not m:
            return OppAction(kind="invalid", error="Usage: /opp delete <id>")
        return OppAction(kind="delete", opp_id=int(m.group(1)))

    if sub == "list":
        # /opp list                → all stages, cards
        # /opp list md             → all stages, markdown table
        # /opp list won            → only "won", cards
        # /opp list md won         → only "won", markdown
        tokens = rest.split() if rest else []
        action = OppAction(kind="list")
        for tok in tokens:
            low = tok.lower()
            if low == "md":
                action.list_format = "md"
            elif low == "cards":
                action.list_format = "cards"
            elif low in STAGES:
                action.filter_stage = low
            else:
                return OppAction(kind="invalid", error=f"Unknown list filter '{tok}'.")
        return action

    if sub == "export":
        # /opp export        → csv
        # /opp export csv    → csv
        fmt = (rest or "csv").strip().lower()
        if fmt != "csv":
            return OppAction(kind="invalid", error="Only csv export is supported.")
        return OppAction(kind="export", export_format="csv")

    return OppAction(kind="invalid", error=f"Unknown subcommand '{sub}'. Try /opp help.")


# ── Service layer (chat_id-scoped) ───────────────────────────────────────

def _alive(query, chat_id: str):
    """Apply chat_id + soft-delete filters. Always use this for reads."""
    return query.filter(
        Opportunity.chat_id == chat_id,
        Opportunity.deleted_at.is_(None),
    )


def get_opp(session: Session, chat_id: str, opp_id: int) -> Optional[Opportunity]:
    """Fetch one opp, scoped to chat_id and not soft-deleted.

    Use this for every mutation — never `session.get(Opportunity, id)`
    directly, or you bypass the chat_id check.
    """
    return _alive(session.query(Opportunity), chat_id).filter(Opportunity.id == opp_id).one_or_none()


def list_opps(
    session: Session,
    chat_id: str,
    filter_stage: Optional[str] = None,
) -> list[Opportunity]:
    q = _alive(session.query(Opportunity), chat_id)
    if filter_stage:
        q = q.filter(Opportunity.stage == filter_stage)
    return q.order_by(Opportunity.updated_at.desc()).all()


def create_opp(session: Session, chat_id: str, title: str, customer: Optional[str]) -> Opportunity:
    opp = Opportunity(chat_id=chat_id, title=title, customer=customer, stage="lead")
    session.add(opp)
    session.flush()
    return opp


def append_update(
    session: Session,
    opp: Opportunity,
    note: str,
    by_chat_id: Optional[str] = None,
) -> OpportunityUpdate:
    upd = OpportunityUpdate(
        opportunity_id=opp.id,
        note=note,
        created_by_chat_id=by_chat_id,
    )
    session.add(upd)
    opp.updated_at = datetime.utcnow()
    session.flush()
    return upd


def change_stage(
    session: Session,
    opp: Opportunity,
    new_stage: str,
    by_chat_id: Optional[str] = None,
) -> OpportunityUpdate:
    """Change stage and write a real OpportunityUpdate row so history is complete."""
    if new_stage not in STAGES:
        raise ValueError(f"Invalid stage '{new_stage}'.")
    old = opp.stage
    opp.stage = new_stage
    opp.updated_at = datetime.utcnow()
    return append_update(session, opp, f"stage: {old} → {new_stage}", by_chat_id)


def soft_delete(session: Session, opp: Opportunity) -> None:
    opp.deleted_at = datetime.utcnow()


# ── Formatters ───────────────────────────────────────────────────────────

def _local(dt: datetime, tz_name: str) -> str:
    """Format a naive-UTC datetime in the configured timezone."""
    if dt is None:
        return "—"
    tz = pytz.timezone(tz_name)
    return pytz.utc.localize(dt).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _iso_local(dt: datetime, tz_name: str) -> str:
    """ISO-8601 in configured tz, for CSV."""
    if dt is None:
        return ""
    tz = pytz.timezone(tz_name)
    return pytz.utc.localize(dt).astimezone(tz).isoformat(timespec="seconds")


def format_card(opp: Opportunity, tz_name: str) -> str:
    """Telegram HTML card for one opportunity."""
    from html import escape as e
    n = len(opp.updates)
    latest = opp.updates[-1].note if n else None
    parts = [
        f"📊 <b>#{opp.id}</b> · {e(opp.title)}",
        f"👤 {e(opp.customer or '—')}  ·  ⚡ <b>{e(opp.stage)}</b>",
        f"📅 Updated {e(_local(opp.updated_at, tz_name))}",
    ]
    if n:
        latest_short = (latest[:120] + "…") if latest and len(latest) > 120 else (latest or "")
        parts.append(f"📝 {n} update(s) · Latest: <i>{e(latest_short)}</i>")
    else:
        parts.append("📝 No updates yet")
    return "\n".join(parts)


def format_show(opp: Opportunity, tz_name: str) -> str:
    """Full detail view with all updates. Truncates if it would exceed Telegram's limit."""
    from html import escape as e
    header = [
        f"📊 <b>#{opp.id}</b> · {e(opp.title)}",
        f"👤 {e(opp.customer or '—')}  ·  ⚡ <b>{e(opp.stage)}</b>",
        f"📅 Created {e(_local(opp.created_at, tz_name))}",
        f"📅 Updated {e(_local(opp.updated_at, tz_name))}",
        "",
        f"<b>Updates ({len(opp.updates)})</b>",
    ]
    body_lines = []
    for upd in opp.updates:
        body_lines.append(f"• <i>{e(_local(upd.created_at, tz_name))}</i> — {e(upd.note)}")

    text = "\n".join(header + body_lines)
    if len(text) <= MAX_MSG_LEN:
        return text

    # Truncate body lines until we fit.
    truncated_notice = "\n\n…(truncated — run /opp export csv for the full history)"
    budget = MAX_MSG_LEN - len("\n".join(header)) - len(truncated_notice) - 1
    kept = []
    used = 0
    for line in body_lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(header + kept) + truncated_notice


def format_markdown_table(opps: list[Opportunity], tz_name: str) -> str:
    """Plain-text markdown table grouped by stage. Sent as Telegram code block."""
    if not opps:
        return "_No opportunities._"

    by_stage: dict[str, list[Opportunity]] = {}
    for o in opps:
        by_stage.setdefault(o.stage, []).append(o)

    out = []
    for stage in STAGES:
        rows = by_stage.get(stage, [])
        if not rows:
            continue
        out.append(f"## {stage.title()} ({len(rows)})")
        out.append("| ID | Title | Customer | Updates | Updated |")
        out.append("|----|-------|----------|---------|---------|")
        for o in rows:
            title = (o.title or "").replace("|", "\\|")
            cust = (o.customer or "—").replace("|", "\\|")
            out.append(f"| {o.id} | {title} | {cust} | {len(o.updates)} | {_local(o.updated_at, tz_name)} |")
        out.append("")

    return "\n".join(out).rstrip()


def build_csv(opps: list[Opportunity], tz_name: str) -> bytes:
    """One row per opportunity. Updates joined with newlines in a quoted cell.

    Encoded as utf-8-sig so Excel on Windows reads non-ASCII correctly.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow([
        "id", "title", "customer", "stage",
        "created_at", "updated_at", "update_count", "updates",
    ])
    for o in opps:
        updates_blob = "\n".join(
            f"[{_iso_local(u.created_at, tz_name)}] {u.note}" for u in o.updates
        )
        writer.writerow([
            o.id,
            o.title,
            o.customer or "",
            o.stage,
            _iso_local(o.created_at, tz_name),
            _iso_local(o.updated_at, tz_name),
            len(o.updates),
            updates_blob,
        ])
    return buf.getvalue().encode("utf-8-sig")
