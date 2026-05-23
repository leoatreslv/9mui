"""Slack-specific config validation.

The v1 invariant is that every canonical chat_id is a numeric string
(Telegram's identity space). user_map values that aren't numeric
must be rejected at config load — otherwise int(chat_id) calls in
secretary.py / main.py would raise ValueError at message time,
which is the 2-week-bug landmine called out in the plan review.
"""

import textwrap
from pathlib import Path

import pytest

from config import load_config


def _write(tmp_path: Path, yaml_body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(yaml_body))
    return p


def test_slack_user_map_accepts_numeric_strings(tmp_path):
    p = _write(tmp_path, """
        platforms: [telegram, slack]
        timezone: UTC
        reminder: {}
        telegram:
          token: T
          allowed_chat_ids: [111]
        slack:
          bot_token: xoxb-x
          app_token: xapp-x
          user_map:
            U01ABC: "111"
            U02DEF: 222
    """)
    cfg = load_config(str(p))
    assert cfg.slack.user_map == {"U01ABC": "111", "U02DEF": "222"}
    assert cfg.platforms == ["telegram", "slack"]


def test_slack_user_map_rejects_non_numeric_canonical(tmp_path):
    p = _write(tmp_path, """
        platforms: [slack]
        timezone: UTC
        reminder: {}
        slack:
          bot_token: xoxb-x
          app_token: xapp-x
          user_map:
            U01ABC: "not-a-number"
    """)
    with pytest.raises(ValueError, match="numeric"):
        load_config(str(p))


def test_legacy_single_platform_form_still_works(tmp_path):
    p = _write(tmp_path, """
        platform: telegram
        timezone: UTC
        reminder: {}
        telegram:
          token: T
    """)
    cfg = load_config(str(p))
    assert cfg.platforms == ["telegram"]


def test_platforms_overrides_legacy_platform_field(tmp_path):
    p = _write(tmp_path, """
        platform: telegram
        platforms: [telegram, slack]
        timezone: UTC
        reminder: {}
        telegram:
          token: T
        slack:
          bot_token: xoxb
          app_token: xapp
          user_map: {}
    """)
    cfg = load_config(str(p))
    assert cfg.platforms == ["telegram", "slack"]


def test_slack_user_map_empty_is_valid(tmp_path):
    p = _write(tmp_path, """
        platforms: [slack]
        timezone: UTC
        reminder: {}
        slack:
          bot_token: xoxb
          app_token: xapp
          user_map: {}
    """)
    cfg = load_config(str(p))
    assert cfg.slack.user_map == {}
