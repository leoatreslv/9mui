"""Parser tests for /opp commands.

These exercise delimiter rules, edge cases the reviewer flagged, and the
"note can contain anything" guarantee.
"""

import pytest

from opp import parse_opp_command, STAGES


class TestHelp:
    def test_bare_opp_returns_help(self):
        assert parse_opp_command("/opp").kind == "help"

    def test_opp_help(self):
        assert parse_opp_command("/opp help").kind == "help"

    def test_opp_question(self):
        assert parse_opp_command("/opp ?").kind == "help"


class TestNew:
    def test_title_only(self):
        a = parse_opp_command("/opp new Acme Q1")
        assert a.kind == "new"
        assert a.title == "Acme Q1"
        assert a.customer is None

    def test_title_with_customer_flag(self):
        a = parse_opp_command("/opp new Acme Q1 renewal --customer=Acme Corp")
        assert a.kind == "new"
        assert a.title == "Acme Q1 renewal"
        assert a.customer == "Acme Corp"

    def test_customer_with_multiple_words(self):
        a = parse_opp_command("/opp new Big deal --customer=Big Corp Inc.")
        assert a.customer == "Big Corp Inc."
        assert a.title == "Big deal"

    def test_customer_can_contain_special_chars(self):
        a = parse_opp_command("/opp new Deal --customer=Foo & Bar, Inc.")
        assert a.customer == "Foo & Bar, Inc."

    def test_em_dash_from_ios_autocorrect_works(self):
        """iOS autocorrects -- to em-dash. The parser should still match."""
        a = parse_opp_command("/opp new Acme Q1 —customer=Acme Corp")
        assert a.kind == "new"
        assert a.title == "Acme Q1"
        assert a.customer == "Acme Corp"

    def test_en_dash_also_works(self):
        a = parse_opp_command("/opp new Acme Q1 –customer=Acme Corp")
        assert a.customer == "Acme Corp"
        assert a.title == "Acme Q1"

    def test_spaces_around_equals_are_tolerated(self):
        a = parse_opp_command("/opp new Acme Q1 --customer = Acme Corp")
        assert a.customer == "Acme Corp"
        assert a.title == "Acme Q1"

    def test_empty_title_is_invalid(self):
        a = parse_opp_command("/opp new")
        assert a.kind == "invalid"

    def test_empty_title_with_only_customer_flag_is_invalid(self):
        a = parse_opp_command("/opp new --customer=Foo")
        assert a.kind == "invalid"


class TestUpdate:
    def test_update_with_simple_note(self):
        a = parse_opp_command("/opp update 5 Met with Bob, looking good")
        assert a.kind == "update"
        assert a.opp_id == 5
        assert a.note == "Met with Bob, looking good"

    def test_note_can_start_with_a_number(self):
        # The reviewer's "2 more weeks" case — id is the first integer,
        # everything else is the note verbatim.
        a = parse_opp_command("/opp update 5 2 more weeks until close")
        assert a.opp_id == 5
        assert a.note == "2 more weeks until close"

    def test_note_can_contain_pipes_and_quotes(self):
        a = parse_opp_command('/opp update 12 He said "no" | call back next week')
        assert a.opp_id == 12
        assert a.note == 'He said "no" | call back next week'

    def test_note_can_be_multiline(self):
        a = parse_opp_command("/opp update 3 line one\nline two")
        assert a.kind == "update"
        assert a.note == "line one\nline two"

    def test_missing_note_is_invalid(self):
        a = parse_opp_command("/opp update 5")
        assert a.kind == "invalid"

    def test_non_numeric_id_is_invalid(self):
        a = parse_opp_command("/opp update abc some note")
        assert a.kind == "invalid"


class TestStage:
    @pytest.mark.parametrize("stage", STAGES)
    def test_all_valid_stages_accepted(self, stage):
        a = parse_opp_command(f"/opp stage 5 {stage}")
        assert a.kind == "stage"
        assert a.stage == stage

    def test_unknown_stage_rejected(self):
        a = parse_opp_command("/opp stage 5 wun")
        assert a.kind == "invalid"
        assert "wun" in (a.error or "")

    def test_stage_case_insensitive(self):
        a = parse_opp_command("/opp stage 5 WON")
        assert a.kind == "stage"
        assert a.stage == "won"

    def test_missing_stage_arg_invalid(self):
        a = parse_opp_command("/opp stage 5")
        assert a.kind == "invalid"


class TestList:
    def test_bare_list(self):
        a = parse_opp_command("/opp list")
        assert a.kind == "list"
        assert a.list_format == "cards"
        assert a.filter_stage is None

    def test_markdown(self):
        a = parse_opp_command("/opp list md")
        assert a.list_format == "md"

    def test_filter_by_stage(self):
        a = parse_opp_command("/opp list won")
        assert a.filter_stage == "won"
        assert a.list_format == "cards"

    def test_markdown_with_stage_filter(self):
        a = parse_opp_command("/opp list md proposal")
        assert a.list_format == "md"
        assert a.filter_stage == "proposal"

    def test_unknown_filter_rejected(self):
        a = parse_opp_command("/opp list garbage")
        assert a.kind == "invalid"


class TestShowDelete:
    def test_show(self):
        a = parse_opp_command("/opp show 7")
        assert a.kind == "show"
        assert a.opp_id == 7

    def test_show_requires_id(self):
        assert parse_opp_command("/opp show").kind == "invalid"

    def test_delete(self):
        a = parse_opp_command("/opp delete 42")
        assert a.kind == "delete"
        assert a.opp_id == 42


class TestExport:
    def test_export_default_csv(self):
        a = parse_opp_command("/opp export")
        assert a.kind == "export"
        assert a.export_format == "csv"

    def test_export_csv_explicit(self):
        a = parse_opp_command("/opp export csv")
        assert a.kind == "export"

    def test_export_other_format_invalid(self):
        a = parse_opp_command("/opp export xlsx")
        assert a.kind == "invalid"


class TestUnknownSubcommand:
    def test_returns_invalid_with_helpful_error(self):
        a = parse_opp_command("/opp frobnicate 5")
        assert a.kind == "invalid"
        assert "frobnicate" in (a.error or "")
