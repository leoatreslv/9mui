"""HTML → Slack mrkdwn conversion."""

from platforms._slack_format import html_to_mrkdwn


def test_plain_text_passes_through():
    assert html_to_mrkdwn("hello world") == "hello world"


def test_bold():
    assert html_to_mrkdwn("<b>important</b>") == "*important*"


def test_italic():
    assert html_to_mrkdwn("<i>maybe</i>") == "_maybe_"


def test_code():
    assert html_to_mrkdwn("<code>x = 1</code>") == "`x = 1`"


def test_pre_becomes_code_block():
    out = html_to_mrkdwn("<pre>line1\nline2</pre>")
    assert out == "```\nline1\nline2\n```"


def test_link():
    assert html_to_mrkdwn('<a href="https://t.me/foo">link</a>') == "<https://t.me/foo|link>"


def test_html_entities_unescaped_in_text():
    # Outside markup tags, &lt; / &gt; / &amp; must end up rendered, but
    # then re-escaped as Slack meta chars to display literally.
    assert html_to_mrkdwn("a &amp; b") == "a &amp; b"
    assert html_to_mrkdwn("&lt;x&gt;") == "&lt;x&gt;"


def test_inside_bold_unescapes():
    # &amp; inside <b> ends up as literal & inside the *...* markers.
    # Slack mrkdwn does not treat & specially inside bold, so no need to re-escape.
    assert html_to_mrkdwn("<b>a &amp; b</b>") == "*a & b*"


def test_link_preserves_url_with_special_chars():
    out = html_to_mrkdwn('<a href="https://example.com?x=1&amp;y=2">click</a>')
    assert out == "<https://example.com?x=1&y=2|click>"


def test_mixed_content():
    html = "Hello <b>Alice</b>, your <code>id</code> is &lt;42&gt;."
    assert html_to_mrkdwn(html) == "Hello *Alice*, your `id` is &lt;42&gt;."


def test_empty_string():
    assert html_to_mrkdwn("") == ""


def test_invite_message():
    # Real example from _handle_invite.
    html = (
        "🔗 <b>Invite link:</b>\n"
        '<a href="https://t.me/foo?start=invite_abc">https://t.me/foo?start=invite_abc</a>'
    )
    out = html_to_mrkdwn(html)
    assert "*Invite link:*" in out
    assert "<https://t.me/foo?start=invite_abc|https://t.me/foo?start=invite_abc>" in out
