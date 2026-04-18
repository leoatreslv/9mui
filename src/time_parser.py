import re
from datetime import datetime, date, timedelta
from typing import Optional, Tuple

import dateparser
from dateparser.search import search_dates


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_DATEPARSER_SETTINGS = {
    "PREFER_DATES_FROM": "future",
    "RETURN_AS_TIMEZONE_AWARE": False,
    "TO_TIMEZONE": "UTC",
}

_LANGUAGES = ["en", "zh"]

# English: "remind me [at/in/on/by/to/...]"
# Traditional Chinese: "提醒我", "幫我提醒", "提醒一下"
_REMIND_RE = re.compile(
    r"(?:,|\s)*"
    r"(?:remind(?:er)?\s+me|提醒我|幫我提醒|提醒一下)"
    r"\s*(?:at|in|on|by|to|tomorrow|next)?\s*"
    r"(.+)",
    re.IGNORECASE | re.DOTALL,
)

# English: "... at 4pm" / "... by 3pm" at end of message
_AT_TIME_RE = re.compile(
    r"(?:,|\s)*\b(?:at|by)\s+(\d[\d:]*\s*[apmAPM]{2}|\d{1,2}:\d{2})\s*$",
    re.IGNORECASE,
)

# Traditional Chinese time at end of message: "明天下午4點", "5分鐘後", "下午3點"
_ZH_TIME_RE = re.compile(
    r"((?:今天|明天|後天)?(?:早上|上午|下午|晚上|夜)?(?:\d{1,2})[點點時时](?:\d{1,2}分)?"
    r"|(?:\d+)(?:分鐘?|小時)後)"
    r"\s*$"
)

# Trigger phrases that indicate content precedes the time in Chinese
_ZH_TRIGGER_RE = re.compile(r"提醒我|幫我提醒|提醒一下")


_ZH_DAY_MAP = {"今天": 0, "明天": 1, "後天": 2}
_ZH_PERIOD_MAP = {"早上": (6, 11), "上午": (6, 11), "下午": (12, 17), "晚上": (18, 23), "夜": (20, 23)}

_ZH_CLOCK_RE = re.compile(
    r"(今天|明天|後天)?"
    r"(早上|上午|下午|晚上|夜)?"
    r"(\d{1,2})[點點時时]"
    r"(?:(\d{1,2})分)?",
)


_ZH_DURATION_RE = re.compile(r"(\d+)\s*(?:(小時|小时)|(分鐘|分钟|分))\s*後?")


def _parse_zh_duration(text: str) -> Optional[datetime]:
    """Parse Traditional/Simplified Chinese relative durations: 1小時後, 5分鐘後."""
    m = _ZH_DURATION_RE.search(text)
    if not m:
        return None
    n = int(m.group(1))
    if m.group(2):  # 小時 / 小时
        return datetime.utcnow() + timedelta(hours=n)
    if m.group(3):  # 分鐘 / 分钟 / 分
        return datetime.utcnow() + timedelta(minutes=n)
    return None


def _parse_zh_clock(text: str) -> Optional[datetime]:
    """Parse Traditional Chinese clock expressions like 明天下午4點, 早上9點, 下午3點30分."""
    m = _ZH_CLOCK_RE.search(text)
    if not m:
        return None

    day_word, period_word, hour_str, minute_str = m.groups()
    hour = int(hour_str)
    minute = int(minute_str) if minute_str else 0

    if period_word in ("下午", "晚上", "夜") and hour < 12:
        hour += 12
    elif period_word in ("早上", "上午") and hour == 12:
        hour = 0

    day_offset = _ZH_DAY_MAP.get(day_word, 0)
    target = date.today() + timedelta(days=day_offset)
    dt = datetime.combine(target, datetime.min.time().replace(hour=hour, minute=minute))

    if day_word is None and dt <= datetime.now():
        dt += timedelta(days=1)

    return dt


def _parse_next_weekday(text: str) -> Optional[datetime]:
    m = re.search(r"\bnext\s+(" + "|".join(_WEEKDAYS) + r")\b", text, re.IGNORECASE)
    if not m:
        return None
    target = _WEEKDAYS[m.group(1).lower()]
    today = date.today()
    delta = (target - today.weekday() + 7) % 7 or 7
    return datetime.combine(today + timedelta(days=delta), datetime.min.time().replace(hour=9))


def _try_parse(text: str, timezone: str) -> Optional[datetime]:
    settings = {**_DATEPARSER_SETTINGS, "TIMEZONE": timezone}
    return (
        dateparser.parse(text, languages=_LANGUAGES, settings=settings)
        or _parse_zh_duration(text)
        or _parse_zh_clock(text)
        or _parse_next_weekday(text)
    )


_TIME_WORD_RE = re.compile(
    r"\d|min|hour|sec|am|pm|noon|midnight|tomorrow|tonight|today|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|next|later|from now|"
    r"點|時|分鐘|小時|明天|後天|今天|下午|上午|晚上|早上|夜",
    re.IGNORECASE,
)


def _search_for_time(text: str, timezone: str) -> Optional[datetime]:
    settings = {**_DATEPARSER_SETTINGS, "TIMEZONE": timezone}
    results = search_dates(text, languages=_LANGUAGES, settings=settings)
    if not results:
        return None
    real = [(s, dt) for s, dt in results if _TIME_WORD_RE.search(s)]
    return real[-1][1] if real else None


def parse_message(text: str, timezone: str = "UTC") -> Tuple[str, Optional[datetime]]:
    """
    Returns (thought_content, remind_at).
    remind_at is None → caller uses the default recurring interval.
    Supports English and Traditional Chinese input.
    """
    settings = {**_DATEPARSER_SETTINGS, "TIMEZONE": timezone}

    # ── Case 1: explicit trigger phrase (EN or ZH) ────────────────────────
    m = _REMIND_RE.search(text)
    if m:
        suffix = m.group(1).strip()
        content = text[: m.start()].strip().rstrip(",，—–-").strip()

        remind_at = _try_parse(suffix, timezone)

        # Strip time expression from suffix to get clean content
        if remind_at and not content:
            # "X分鐘後content" or "X小時後content" — content follows 後
            dur_m = re.search(r"\d+(?:小時|小时|分鐘|分钟|分)後?", suffix)
            if dur_m:
                content = suffix[dur_m.end():].strip()
            else:
                zh_m = _ZH_CLOCK_RE.search(suffix)
                if zh_m:
                    before = suffix[:zh_m.start()].strip().rstrip("，。—–- ")
                    after = suffix[zh_m.end():].strip().lstrip("，。—–- ")
                    content = before if len(before) >= len(after) else after
            if not content:
                content = suffix

        # "remind me to X [time]" or "提醒我去X [time]" — content inside suffix
        has_content_before_time = bool(
            re.search(r"remind(?:er)?\s+me\s+to\b", text, re.IGNORECASE)
            or _ZH_TRIGGER_RE.search(text)
        )
        if not remind_at and has_content_before_time:
            remind_at = _search_for_time(suffix, timezone)
            if remind_at and not content:
                # Try full duration pattern first ("5分鐘後", "1小時後")
                dur_m = re.search(r"\d+(?:小時|小时|分鐘|分钟|分)後?", suffix)
                if dur_m:
                    content = suffix[dur_m.end():].strip()
                else:
                    results = search_dates(suffix, languages=_LANGUAGES, settings=settings)
                    if results:
                        time_str = results[-1][0]
                        content = re.sub(re.escape(time_str) + r"後?", "", suffix, flags=re.IGNORECASE)
                        content = re.sub(r"^(?:to\s+|去|做|幫)", "", content, flags=re.IGNORECASE)
                        content = re.sub(r"\b(later|from now|ago)\b", "", content, flags=re.IGNORECASE)
                        content = content.strip().rstrip(".,，。—–-").strip()

        if remind_at:
            return content or suffix, remind_at
        return content or suffix, None

    # ── Case 2: Traditional Chinese time at end — "叫老婆買菜 明天下午3點" ──
    m = _ZH_TIME_RE.search(text)
    if m:
        time_str = m.group(1).strip()
        content = text[: m.start()].strip().rstrip("，。—–-").strip()
        remind_at = _try_parse(time_str, timezone)
        if remind_at:
            return content or text.strip(), remind_at

    # ── Case 3: English "at 4pm" / "by 3pm" at end ───────────────────────
    m = _AT_TIME_RE.search(text)
    if m:
        time_str = m.group(1).strip()
        content = text[: m.start()].strip().rstrip(",—–-").strip()
        remind_at = _try_parse(time_str, timezone)
        if remind_at:
            return content or text.strip(), remind_at

    return text.strip(), None
