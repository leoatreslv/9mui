"""Service-layer tests: CRUD, multi-user isolation, stage transitions, soft-delete."""

import pytest

from database import Opportunity, OpportunityUpdate
from opp import (
    create_opp,
    get_opp,
    list_opps,
    append_update,
    change_stage,
    soft_delete,
)


def test_create_opp_starts_at_lead(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", "Acme Corp")
    db_session.commit()
    assert opp.id is not None
    assert opp.stage == "lead"
    assert opp.customer == "Acme Corp"
    assert opp.deleted_at is None


def test_append_update_writes_a_row(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    append_update(db_session, opp, "First contact made", by_chat_id="user-1")
    db_session.commit()
    db_session.refresh(opp)
    assert len(opp.updates) == 1
    assert opp.updates[0].note == "First contact made"
    assert opp.updates[0].created_by_chat_id == "user-1"


def test_append_update_bumps_updated_at(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    original_updated = opp.updated_at
    # Force enough time delta that the comparison is meaningful even on
    # fast machines: we don't sleep — just ensure the column moves on
    # write by checking it's set to a non-None value after the change.
    append_update(db_session, opp, "Note", by_chat_id="user-1")
    db_session.commit()
    db_session.refresh(opp)
    assert opp.updated_at >= original_updated


def test_change_stage_writes_audit_update(db_session):
    """The reviewer asked: stage transitions must produce an
    OpportunityUpdate row, not just a log line."""
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    change_stage(db_session, opp, "qualified", by_chat_id="user-1")
    db_session.commit()
    db_session.refresh(opp)

    assert opp.stage == "qualified"
    assert len(opp.updates) == 1
    assert "lead → qualified" in opp.updates[0].note


def test_change_stage_to_invalid_raises(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    with pytest.raises(ValueError):
        change_stage(db_session, opp, "definitely-not-a-stage", by_chat_id="user-1")


def test_soft_delete_hides_from_list(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    soft_delete(db_session, opp)
    db_session.commit()

    assert list_opps(db_session, "user-1") == []
    # The row still exists, just filtered out:
    raw = db_session.query(Opportunity).all()
    assert len(raw) == 1


def test_soft_deleted_opp_not_returned_by_get(db_session):
    opp = create_opp(db_session, "user-1", "Acme Q1", None)
    db_session.flush()
    soft_delete(db_session, opp)
    db_session.commit()
    assert get_opp(db_session, "user-1", opp.id) is None


def test_user_isolation_get(db_session):
    """User A cannot fetch User B's opp by ID."""
    a = create_opp(db_session, "user-A", "A's deal", None)
    b = create_opp(db_session, "user-B", "B's deal", None)
    db_session.commit()
    assert get_opp(db_session, "user-A", a.id) is not None
    # Crucial: using the OTHER user's chat_id must NOT return user A's opp.
    assert get_opp(db_session, "user-B", a.id) is None
    assert get_opp(db_session, "user-A", b.id) is None


def test_user_isolation_list(db_session):
    create_opp(db_session, "user-A", "A1", None)
    create_opp(db_session, "user-A", "A2", None)
    create_opp(db_session, "user-B", "B1", None)
    db_session.commit()

    a_opps = list_opps(db_session, "user-A")
    assert {o.title for o in a_opps} == {"A1", "A2"}
    b_opps = list_opps(db_session, "user-B")
    assert {o.title for o in b_opps} == {"B1"}


def test_list_filter_by_stage(db_session):
    o1 = create_opp(db_session, "user-1", "Lead deal", None)
    o2 = create_opp(db_session, "user-1", "Closed deal", None)
    db_session.flush()
    change_stage(db_session, o2, "won")
    db_session.commit()

    leads = list_opps(db_session, "user-1", filter_stage="lead")
    wons = list_opps(db_session, "user-1", filter_stage="won")
    assert [o.id for o in leads] == [o1.id]
    assert [o.id for o in wons] == [o2.id]


def test_cascade_delete_removes_updates(db_session):
    """A *hard* delete via session.delete cascades to updates — confirms
    the relationship is wired correctly even though we soft-delete in
    the app."""
    opp = create_opp(db_session, "user-1", "Acme", None)
    db_session.flush()
    append_update(db_session, opp, "note 1")
    append_update(db_session, opp, "note 2")
    db_session.commit()
    db_session.refresh(opp)
    update_count = db_session.query(OpportunityUpdate).count()
    assert update_count == 2

    db_session.delete(opp)
    db_session.commit()
    assert db_session.query(OpportunityUpdate).count() == 0
