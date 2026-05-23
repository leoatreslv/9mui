"""HTML → Slack mrkdwn conversion.

main.py emits HTML (because Telegram is the historical primary platform).
Slack uses its own "mrkdwn" markup. This module bridges the two so the
core message router stays platform-neutral.

Slack mrkdwn meta characters that must be escaped in literal text:
  &  →  &amp;
  <  →  &lt;
  >  →  &gt;

Conversion strategy:
  1. Replace HTML formatting tags (<b>, <i>, <code>, <pre>, <a>) with
     placeholder tokens whose substitution values already contain the
     correct mrkdwn (including any literal < > used inside <URL|label>).
  2. HTML-unescape the remaining text.
  3. Escape Slack meta chars (& < >) in the remaining text only.
  4. Substitute placeholders back in.

This order means meta-char escaping happens BEFORE we restore the
mrkdwn link syntax, so a payload like `<a href="https://x">x</a>`
ends up as `<https://x|x>` rather than `&lt;https://x|x&gt;`.
"""

from __future__ import annotations

import re
from html import unescape as _html_unescape

_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.S)
_CODE_RE = re.compile(r"<code>(.*?)</code>", re.S)
_B_RE = re.compile(r"<b>(.*?)</b>", re.S)
_I_RE = re.compile(r"<i>(.*?)</i>", re.S)
_A_RE = re.compile(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def html_to_mrkdwn(html: str) -> str:
    if not html:
        return ""

    placeholders: dict[str, str] = {}
    counter = [0]

    def stash(value: str) -> str:
        token = f"\x00PH{counter[0]}\x00"
        counter[0] += 1
        placeholders[token] = value
        return token

    s = html
    s = _PRE_RE.sub(lambda m: stash("```\n" + _html_unescape(m.group(1)) + "\n```"), s)
    s = _CODE_RE.sub(lambda m: stash("`" + _html_unescape(m.group(1)) + "`"), s)
    s = _B_RE.sub(lambda m: stash("*" + _html_unescape(m.group(1)) + "*"), s)
    s = _I_RE.sub(lambda m: stash("_" + _html_unescape(m.group(1)) + "_"), s)
    s = _A_RE.sub(
        lambda m: stash(f"<{_html_unescape(m.group(1))}|{_html_unescape(m.group(2))}>"),
        s,
    )

    s = _html_unescape(s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    for token, value in placeholders.items():
        s = s.replace(token, value)

    return s


def section(text_mrkdwn: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text_mrkdwn}}


def actions(buttons: list[dict]) -> dict:
    return {"type": "actions", "elements": buttons}


def button(label: str, action_id: str, style: str | None = None) -> dict:
    el = {
        "type": "button",
        "text": {"type": "plain_text", "text": label, "emoji": True},
        "action_id": action_id,
    }
    if style:
        el["style"] = style
    return el
