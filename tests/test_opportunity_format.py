"""Formatter tests: cards, markdown table, CSV export shape."""

import csv
import io

from opp import (
    build_csv,
    create_opp,
    append_update,
    change_stage,
    format_card,
    format_markdown_table,
    format_show,
    list_opps,
    MAX_MSG_LEN,
)


TZ = "UTC"


def test_csv_has_header_and_one_row_per_opp(db_session):
    create_opp(db_session, "u", "Acme", "Acme Corp")
    create_opp(db_session, "u", "Globex", None)
    db_session.commit()

    data = build_csv(list_opps(db_session, "u"), TZ)
    # utf-8-sig BOM
    assert data.startswith(b"\xef\xbb\xbf")

    text = data.decode("utf-8-sig")
    reader = list(csv.reader(io.StringIO(text)))
    assert reader[0] == [
        "id", "title", "customer", "stage",
        "created_at", "updated_at", "update_count", "updates",
    ]
    # Two opps + one header
    assert len(reader) == 3


def test_csv_handles_commas_quotes_and_newlines_in_notes(db_session):
    """The reviewer flagged that notes will contain commas, quotes, and
    newlines. csv.writer with QUOTE_MINIMAL must escape them correctly."""
    opp = create_opp(db_session, "u", "Tricky", "Customer, Inc.")
    db_session.flush()
    append_update(db_session, opp, 'Said "no" today.\nFollow up.')
    append_update(db_session, opp, "Comma, in note")
    db_session.commit()

    data = build_csv(list_opps(db_session, "u"), TZ)
    text = data.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) == 2  # header + one opp row
    row = rows[1]
    assert row[2] == "Customer, Inc."
    assert "Said \"no\" today.\nFollow up." in row[7]
    assert "Comma, in note" in row[7]


def test_csv_includes_iso_timestamps(db_session):
    """Timestamps should be ISO-8601, not naive Python str()."""
    create_opp(db_session, "u", "Acme", None)
    db_session.commit()

    data = build_csv(list_opps(db_session, "u"), TZ)
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    created_at = rows[1][4]
    # 2025-05-16T12:34:56+00:00 ish
    assert "T" in created_at
    assert created_at.endswith("+00:00")


def test_markdown_table_groups_by_stage(db_session):
    a = create_opp(db_session, "u", "Lead one", "Co A")
    b = create_opp(db_session, "u", "Win one", "Co B")
    db_session.flush()
    change_stage(db_session, b, "won")
    db_session.commit()

    md = format_markdown_table(list_opps(db_session, "u"), TZ)
    assert "## Lead" in md
    assert "## Won" in md
    # Lead section must come before Won section (canonical stage order)
    assert md.index("## Lead") < md.index("## Won")
    assert "Lead one" in md
    assert "Win one" in md
    assert str(a.id) in md
    assert str(b.id) in md


def test_markdown_table_escapes_pipes_in_data(db_session):
    create_opp(db_session, "u", "Has | pipe", "Cust|omer")
    db_session.commit()
    md = format_markdown_table(list_opps(db_session, "u"), TZ)
    # | inside a cell must be backslash-escaped so the table stays valid
    assert "Has \\| pipe" in md
    assert "Cust\\|omer" in md


def test_card_renders_with_no_updates(db_session):
    opp = create_opp(db_session, "u", "Fresh", "Cust")
    db_session.commit()
    card = format_card(opp, TZ)
    assert "#" + str(opp.id) in card
    assert "Fresh" in card
    assert "lead" in card
    assert "No updates yet" in card


def test_card_shows_latest_update(db_session):
    opp = create_opp(db_session, "u", "Has updates", "Cust")
    db_session.flush()
    append_update(db_session, opp, "First")
    append_update(db_session, opp, "Second is the latest")
    db_session.commit()
    db_session.refresh(opp)

    card = format_card(opp, TZ)
    assert "2 update(s)" in card
    assert "Second is the latest" in card


def test_show_truncates_when_over_telegram_limit(db_session):
    opp = create_opp(db_session, "u", "Lots", None)
    db_session.flush()
    # 200 long notes guarantees overflow of 4000-char budget
    for i in range(200):
        append_update(db_session, opp, f"Update #{i:03d}: " + "x" * 50)
    db_session.commit()
    db_session.refresh(opp)

    text = format_show(opp, TZ)
    assert len(text) <= MAX_MSG_LEN
    assert "truncated" in text.lower()
