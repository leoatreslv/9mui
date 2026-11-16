"""Time-expression parsing. `now` is pinned so results are deterministic.

NOW is Monday 2026-11-16 02:00 UTC = 10:00 in Asia/Singapore (UTC+8).
"""

from datetime import datetime

import pytest

from time_parser import parse_message

TZ = "Asia/Singapore"
NOW = datetime(2026, 11, 16, 2, 0)


def sgt(month, day, hour, minute=0, year=2026):
    """Singapore wall-clock time → the naive UTC the parser returns."""
    from datetime import timedelta
    return datetime(year, month, day, hour, minute) - timedelta(hours=8)


@pytest.mark.parametrize("text, content, expected", [
    # README examples
    ("Call dentist — remind me at 4pm", "Call dentist", sgt(11, 16, 16)),
    ("remind me to call dentist at 4pm", "call dentist", sgt(11, 16, 16)),
    ("Meeting notes by 3pm", "Meeting notes", sgt(11, 16, 15)),
    ("standup at 10:30", "standup", sgt(11, 16, 10, 30)),
    ("提醒我明天下午4點打電話給醫生", "打電話給醫生", sgt(11, 17, 16)),
    ("叫老婆買菜 明天下午3點", "叫老婆買菜", sgt(11, 17, 15)),
    ("今天早上11點 開會", "開會", sgt(11, 16, 11)),
    ("提醒我下午3點開會", "開會", sgt(11, 16, 15)),
    ("3點半開會", "開會", sgt(11, 17, 3, 30)),
    # A clock time already past today rolls to tomorrow
    ("coffee at 9am", "coffee", sgt(11, 17, 9)),
    # Day words, with and without a clock time
    ("call bob tomorrow 9am", "call bob", sgt(11, 17, 9)),
    ("call bob tomorrow at 9am", "call bob", sgt(11, 17, 9)),
    ("remind me to call bob tomorrow 9am", "call bob", sgt(11, 17, 9)),
    ("meet at 4pm tomorrow", "meet", sgt(11, 17, 16)),
    ("call bob tomorrow", "call bob", sgt(11, 17, 9)),
    ("dinner tonight at 8", "dinner", sgt(11, 16, 20)),
    ("remind me to ship next monday", "ship", sgt(11, 23, 9)),
    ("remind me to call mum at 4pm on friday", "call mum", sgt(11, 20, 16)),
    ("remind me to renew passport next week", "renew passport", sgt(11, 23, 9)),
    ("提醒我明天交報告", "交報告", sgt(11, 17, 9)),
    # Calendar dates
    ("remind me on 5 Dec at 3pm to pay rent", "pay rent", sgt(12, 5, 15)),
    ("remind me to pay rent on december 12", "pay rent", sgt(12, 12, 9)),
    ("pay rent Jan 3rd 10am", "pay rent", sgt(1, 3, 10, year=2027)),
    # Time before the task
    ("remind me at 4:30pm to stand up", "stand up", sgt(11, 16, 16, 30)),
])
def test_absolute_times(text, content, expected):
    assert parse_message(text, TZ, now=NOW) == (content, expected)


@pytest.mark.parametrize("text, content, minutes", [
    ("remind me to get off plane in 5 mins", "get off plane", 5),
    ("call bob in 30 minutes", "call bob", 30),
    ("check oven in half an hour", "check oven", 30),
    ("remind me to stretch in an hour", "stretch", 60),
    ("提醒我5分鐘後下飛機", "下飛機", 5),
    ("開會 1小時後", "開會", 60),
])
def test_relative_times(text, content, minutes):
    got_content, got = parse_message(text, TZ, now=NOW)
    assert got_content == content
    assert (got - NOW).total_seconds() == minutes * 60


@pytest.mark.parametrize("text", [
    "Buy groceries",
    "tomorrow never dies",          # English time must be at the end
    "read 1984 by Orwell",
    "pay bill at 25:00",
    "pay rent 31 feb",
    "I may 5 times",
])
def test_no_time_keeps_text_verbatim(text):
    assert parse_message(text, TZ, now=NOW) == (text, None)


def test_trigger_without_time_strips_trigger():
    assert parse_message("remind me to breathe", TZ, now=NOW) == ("breathe", None)


def test_time_only_message_keeps_original_as_content():
    content, when = parse_message("remind me tomorrow 9am", TZ, now=NOW)
    assert content == "remind me tomorrow 9am"
    assert when == sgt(11, 17, 9)


def test_out_of_range_chinese_hour_does_not_crash():
    assert parse_message("提醒我25點睡覺", TZ, now=NOW) == ("25點睡覺", None)


def test_clock_times_follow_the_configured_timezone():
    """Same message, different timezone → different UTC instant."""
    _, utc = parse_message("call at 4pm", "UTC", now=NOW)
    _, sg = parse_message("call at 4pm", TZ, now=NOW)
    assert utc == datetime(2026, 11, 16, 16, 0)
    assert sg == datetime(2026, 11, 16, 8, 0)


def test_fuzzy_fallback_handles_dates_the_patterns_do_not_know():
    content, when = parse_message("remind me to pay rent on 12/25", TZ, now=NOW)
    assert content == "pay rent"
    assert when == sgt(12, 25, 9)


def test_feb_29_in_a_non_leap_year_does_not_crash():
    assert parse_message("party on 29 feb", TZ, now=NOW) == ("party on 29 feb", None)
