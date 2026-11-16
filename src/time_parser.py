"""Pull a reminder time out of a free-text message (English + Chinese).

parse_message() returns (content, remind_at) where remind_at is naive
UTC, or None when the message carries no time (the caller then falls
back to the default recurring interval).

How it works:
  1. A trigger phrase ("remind me [to]", "提醒我", …) is stripped out.
  2. Time expressions are found with explicit patterns and resolved in
     the user's timezone — clock times ("at 4pm", "下午3點"), days
     ("tomorrow", "next monday", "明天"), day + clock, and relative
     offsets ("in 5 mins", "1小時後").
  3. Without a trigger phrase we stay conservative: the expression must
     sit at the end of the message (or the start, for Chinese, where
     time-first is the natural word order). With a trigger it may appear
     anywhere.
  4. Only if a trigger is present and nothing matched do we fall back to
     dateparser's fuzzy search ("remind me on 5 Dec to pay rent").
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pytz

# A day named without a clock time ("tomorrow", "next monday") means 9am.
DEFAULT_HOUR = 9
TONIGHT_HOUR = 20

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# ── Trigger phrases ──────────────────────────────────────────────────────

_TRIGGER_RE = re.compile(
    r"\bremind(?:er)?\s+me(?:\s+(?:to|that)\b)?"
    r"|(?:幫我提醒|帮我提醒|提醒一下|提醒我)(?:要|去)?",
    re.IGNORECASE,
)

# ── English patterns ─────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MON = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_ORD = r"\d{1,2}(?:st|nd|rd|th)?"
# "5 Dec" / "on December 12th"
_EN_DATE = rf"(?:(?:on\s+)?(?:{_ORD}\s+{_MON}|{_MON}\s+{_ORD}))"
_EN_DATE_PARTS_RE = re.compile(
    rf"(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})|({_MON})\s+(\d{{1,2}}))", re.IGNORECASE
)

_WD = "|".join(_WEEKDAYS)
_EN_DAY = (
    rf"(?:{_EN_DATE}|today|tonight|tomorrow|tmrw?|next\s+week"
    rf"|(?:next|on|this)\s+(?:{_WD}))"
)
_EN_CLOCK = r"(?:\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2}|noon|midnight)"
_EN_REL = r"in\s+(?:\d+|an?|half\s+an?)\s*(?:minutes?|mins?|hours?|hrs?|days?|weeks?)"

_EN_TIME_RE = re.compile(
    rf"(?<![\w:])(?:"
    rf"{_EN_REL}"
    rf"|{_EN_DAY}(?:\s+(?:(?:at|by)\s+)?{_EN_CLOCK}|\s+(?:at|by)\s+\d{{1,2}})?"
    rf"|(?:(?:at|by|@)\s*)?{_EN_CLOCK}(?:\s+(?:on\s+)?{_EN_DAY})?"
    rf")(?![\w:])",
    re.IGNORECASE,
)

_EN_REL_PARTS_RE = re.compile(
    r"in\s+(\d+|an?|half\s+an?)\s*(min|hour|hr|day|week)", re.IGNORECASE
)
_EN_CLOCK_PARTS_RE = re.compile(
    r"(?:(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?|(\d{1,2}):(\d{2})|(noon|midnight))",
    re.IGNORECASE,
)
# "tomorrow at 8" — a bare hour is only accepted right after a day word.
_EN_BARE_HOUR_RE = re.compile(r"(?:at|by)\s+(\d{1,2})$", re.IGNORECASE)
_EN_WEEKDAY_RE = re.compile(rf"({_WD})", re.IGNORECASE)

_UNIT_DELTAS = {
    "min": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
}

# ── Chinese patterns (Traditional + Simplified) ──────────────────────────

_ZH_DAY_MAP = {"今天": 0, "明天": 1, "後天": 2, "后天": 2}
_ZH_DAY = "今天|明天|後天|后天"
_ZH_PERIOD = "早上|上午|中午|下午|晚上|夜晚|夜"
_ZH_PERIOD_DEFAULT_HOUR = {
    "早上": 9, "上午": 9, "中午": 12, "下午": 15, "晚上": 20, "夜晚": 20, "夜": 20,
}

# 明天下午4點 / 早上9點 / 下午3點30分 / 3點半
_ZH_CLOCK_RE = re.compile(
    rf"({_ZH_DAY})?({_ZH_PERIOD})?(\d{{1,2}})[點点時时](?:(半)|(\d{{1,2}})分?)?"
)
# 5分鐘後 / 1小時後 / 半小時後 / 2天後
_ZH_DURATION_RE = re.compile(
    r"(半|\d+)\s*(?:個|个)?(小時|小时|分鐘|分钟|分|天)\s*[後后]"
)
# 明天 / 後天下午 — a day with no clock time. Only honoured after a trigger.
_ZH_DAY_ONLY_RE = re.compile(rf"(明天|後天|后天)({_ZH_PERIOD})?(?!\s*\d)")

_EDGE_PUNCT = " \t\r\n,.;:!?，。；：！？、—–-"


# ── Timezone helpers ─────────────────────────────────────────────────────


def _to_local(now_utc: datetime, timezone: str) -> datetime:
    """Naive UTC → naive wall-clock time in the user's timezone."""
    return pytz.utc.localize(now_utc).astimezone(pytz.timezone(timezone)).replace(tzinfo=None)


def _to_utc(local_dt: datetime, timezone: str) -> datetime:
    """Naive wall-clock time in `timezone` → naive UTC (what the DB stores)."""
    return pytz.timezone(timezone).localize(local_dt).astimezone(pytz.utc).replace(tzinfo=None)


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


# ── Resolving a matched phrase to a datetime ─────────────────────────────


def _resolve_en(phrase: str, now_utc: datetime, timezone: str) -> Optional[datetime]:
    p = phrase.lower()

    rel = _EN_REL_PARTS_RE.search(p)
    if rel:
        amount, unit = rel.groups()
        if amount.startswith("half"):
            n = 0.5
        elif amount in ("a", "an"):
            n = 1
        else:
            n = int(amount)
        return now_utc + n * _UNIT_DELTAS[unit]

    now = _to_local(now_utc, timezone)

    # Which day?
    day = None
    default_hour = DEFAULT_HOUR
    date_m = _EN_DATE_PARTS_RE.search(p)
    if date_m:
        d1, mon1, mon2, d2 = date_m.groups()
        try:
            day = now.replace(month=_MONTHS[(mon1 or mon2)[:3]], day=int(d1 or d2))
            if day.date() < now.date():   # already past this year → next year
                day = day.replace(year=day.year + 1)
        except ValueError:            # "31 feb", or 29 feb in a non-leap year
            return None
        p = p[:date_m.start()] + p[date_m.end():]   # don't re-read the day as an hour
    elif "tomorrow" in p or "tmr" in p:
        day = now + timedelta(days=1)
    elif "tonight" in p:
        day, default_hour = now, TONIGHT_HOUR
    elif "today" in p:
        day, default_hour = now, None
    elif "week" in p:
        day = now + timedelta(days=7)
    else:
        wd = _EN_WEEKDAY_RE.search(p)
        if wd:
            delta = (_WEEKDAYS[wd.group(1)] - now.weekday()) % 7 or 7
            day = now + timedelta(days=delta)

    # Which time of day?
    clock = None
    m = _EN_CLOCK_PARTS_RE.search(p)
    if m:
        h12, m12, ampm, h24, m24, word = m.groups()
        if word:
            clock = (12, 0) if word == "noon" else (0, 0)
        elif ampm:
            hour, minute = int(h12), int(m12 or 0)
            if not 1 <= hour <= 12 or minute > 59:
                return None
            hour = hour % 12 + (12 if ampm == "p" else 0)
            clock = (hour, minute)
        else:
            hour, minute = int(h24), int(m24)
            if hour > 23 or minute > 59:
                return None
            if "tonight" in p and hour < 12:
                hour += 12
            clock = (hour, minute)

    elif day is not None:
        bare = _EN_BARE_HOUR_RE.search(p)
        if bare:
            hour = int(bare.group(1))
            if hour > 23:
                return None
            if "tonight" in p and hour < 12:
                hour += 12
            clock = (hour, 0)

    if day is not None and clock is not None:
        local = _at(day, *clock)
    elif day is not None:
        if default_hour is None:      # bare "today" says nothing about when
            return None
        local = _at(day, default_hour)
    elif clock is not None:
        local = _at(now, *clock)
        if local <= now:              # "at 4pm" said at 6pm → tomorrow
            local += timedelta(days=1)
    else:
        return None

    return _to_utc(local, timezone)


def _resolve_zh_clock(m: re.Match, now_utc: datetime, timezone: str) -> Optional[datetime]:
    day_word, period, hour_str, half, minute_str = m.groups()
    hour = int(hour_str)
    minute = 30 if half else int(minute_str or 0)
    extra_days = 0

    if period in ("下午", "晚上", "夜晚", "夜") and hour < 12:
        hour += 12
    elif period in ("晚上", "夜晚", "夜") and hour == 12:   # 晚上12點 = midnight
        hour, extra_days = 0, 1
    elif period == "中午" and hour < 6:
        hour += 12
    elif period in ("早上", "上午") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    now = _to_local(now_utc, timezone)
    local = _at(now + timedelta(days=_ZH_DAY_MAP.get(day_word, 0) + extra_days), hour, minute)
    if day_word is None and local <= now:
        local += timedelta(days=1)
    return _to_utc(local, timezone)


def _resolve_zh_duration(m: re.Match, now_utc: datetime) -> datetime:
    amount, unit = m.groups()
    n = 0.5 if amount == "半" else int(amount)
    if unit in ("小時", "小时"):
        return now_utc + timedelta(hours=n)
    if unit == "天":
        return now_utc + timedelta(days=n)
    return now_utc + timedelta(minutes=n)


def _resolve_zh_day_only(m: re.Match, now_utc: datetime, timezone: str) -> datetime:
    day_word, period = m.groups()
    now = _to_local(now_utc, timezone)
    hour = _ZH_PERIOD_DEFAULT_HOUR.get(period, DEFAULT_HOUR)
    return _to_utc(_at(now + timedelta(days=_ZH_DAY_MAP[day_word]), hour), timezone)


# ── Finding the time expression ──────────────────────────────────────────


def _candidates(text: str, now_utc: datetime, timezone: str, has_trigger: bool):
    """Yield (start, end, remind_at, is_zh) for each time expression."""
    for m in _EN_TIME_RE.finditer(text):
        dt = _resolve_en(m.group(0), now_utc, timezone)
        if dt:
            yield m.start(), m.end(), dt, False
    for m in _ZH_CLOCK_RE.finditer(text):
        dt = _resolve_zh_clock(m, now_utc, timezone)
        if dt:
            yield m.start(), m.end(), dt, True
    for m in _ZH_DURATION_RE.finditer(text):
        yield m.start(), m.end(), _resolve_zh_duration(m, now_utc), True
    if has_trigger:
        for m in _ZH_DAY_ONLY_RE.finditer(text):
            yield m.start(), m.end(), _resolve_zh_day_only(m, now_utc, timezone), True


def _pick(text: str, cands: list, has_trigger: bool):
    """Choose the expression to honour: one at the end of the message
    wins, then one at the start, then (trigger only) the last one."""
    at_end = [c for c in cands if not text[c[1]:].strip(_EDGE_PUNCT)]
    if at_end:
        return min(at_end, key=lambda c: c[0])           # longest match at the end
    at_start = [
        c for c in cands
        if not text[:c[0]].strip(_EDGE_PUNCT) and (has_trigger or c[3])
    ]
    if at_start:
        return max(at_start, key=lambda c: c[1])         # longest match at the start
    if has_trigger and cands:
        return max(cands, key=lambda c: c[0])
    return None


def _clean(content: str) -> str:
    content = re.sub(r"\s+", " ", content).strip(_EDGE_PUNCT)
    content = re.sub(r"^(?:to|that)\s+", "", content, flags=re.IGNORECASE)
    return content.strip(_EDGE_PUNCT)


# ── dateparser fallback (trigger phrase only) ────────────────────────────

_TIME_WORD_RE = re.compile(
    r"\d|noon|midnight|tomorrow|tonight|today|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday|next|later|from now",
    re.IGNORECASE,
)


def _fuzzy_search(text: str, now_utc: datetime, timezone: str):
    """Last resort for dates the patterns don't know ("on 5 Dec").
    Returns (matched_substring, remind_at) or None."""
    # Imported lazily: dateparser is slow to load and rarely needed.
    from dateparser.search import search_dates

    settings = {
        "PREFER_DATES_FROM": "future",
        "RETURN_AS_TIMEZONE_AWARE": False,
        "TIMEZONE": timezone,
        "TO_TIMEZONE": "UTC",
        "RELATIVE_BASE": _to_local(now_utc, timezone),
    }
    results = search_dates(text, languages=["en", "zh"], settings=settings) or []
    real = [(s, dt) for s, dt in results if _TIME_WORD_RE.search(s) and dt > now_utc]
    if not real:
        return None
    matched, dt = real[-1]
    # A bare date ("5 Dec") comes back as local midnight — use the same
    # 9am default as "tomorrow" instead of pinging in the small hours.
    local = _to_local(dt, timezone)
    explicit_time = re.search(r"midnight|:|\d\s*[ap]\.?m", matched, re.IGNORECASE)
    if (local.hour, local.minute) == (0, 0) and not explicit_time:
        dt = _to_utc(_at(local, DEFAULT_HOUR), timezone)
    return matched, dt


# ── Public entry point ───────────────────────────────────────────────────


def parse_message(
    text: str,
    timezone: str = "UTC",
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[datetime]]:
    """
    Returns (thought_content, remind_at).
    remind_at is naive UTC; None → caller uses the default recurring interval.
    `now` (naive UTC) is injectable for tests.
    """
    original = text.strip()
    now_utc = now or datetime.utcnow()

    trigger = _TRIGGER_RE.search(original)
    if trigger:
        body = f"{original[:trigger.start()]} {original[trigger.end():]}".strip()
    else:
        body = original

    cands = list(_candidates(body, now_utc, timezone, bool(trigger)))
    chosen = _pick(body, cands, bool(trigger))

    if chosen:
        start, end, remind_at, _ = chosen
        content = _clean(f"{body[:start]} {body[end:]}")
        return content or original, remind_at

    if trigger and body:
        found = _fuzzy_search(body, now_utc, timezone)
        if found:
            matched, remind_at = found
            content = _clean(body.replace(matched, " ", 1))
            return content or original, remind_at

    return (_clean(body) or original) if trigger else original, None
