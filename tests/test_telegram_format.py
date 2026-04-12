"""Tests for the Markdown-to-Telegram-HTML converter and plain-text fallback.

These are pure-function tests — no fixtures, no mocking, no IO. They cover
every formatting feature the converter supports plus HTML-escape safety and
the identifier-underscore edge case that distinguishes Telegram-friendly
markdown from naive regex substitution.
"""

from __future__ import annotations

from artimesone.telegram.format import markdown_to_telegram_html, strip_to_plain

# ---------------------------------------------------------------------------
# markdown_to_telegram_html — inline formatting
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty() -> None:
    assert markdown_to_telegram_html("") == ""


def test_plain_text_unchanged() -> None:
    assert markdown_to_telegram_html("hello world") == "hello world"


def test_bold_double_asterisk() -> None:
    assert markdown_to_telegram_html("**bold**") == "<b>bold</b>"


def test_bold_double_underscore() -> None:
    assert markdown_to_telegram_html("__bold__") == "<b>bold</b>"


def test_italic_underscore() -> None:
    assert markdown_to_telegram_html("_italic_") == "<i>italic</i>"


def test_italic_inside_sentence() -> None:
    assert markdown_to_telegram_html("foo _italic_ bar") == "foo <i>italic</i> bar"


def test_italic_ignores_identifier_underscores() -> None:
    """some_var_name must not be mangled into italic markers."""
    assert markdown_to_telegram_html("some_var_name") == "some_var_name"


def test_strikethrough() -> None:
    assert markdown_to_telegram_html("~~gone~~") == "<s>gone</s>"


def test_link_converted_to_anchor() -> None:
    assert (
        markdown_to_telegram_html("[text](https://example.com)")
        == '<a href="https://example.com">text</a>'
    )


def test_mixed_bold_italic_code() -> None:
    md = "**bold** and _italic_ and `code`"
    expected = "<b>bold</b> and <i>italic</i> and <code>code</code>"
    assert markdown_to_telegram_html(md) == expected


# ---------------------------------------------------------------------------
# markdown_to_telegram_html — code blocks
# ---------------------------------------------------------------------------


def test_triple_backtick_code_block() -> None:
    result = markdown_to_telegram_html("```\nprint('hi')\n```")
    assert result == "<pre><code>print('hi')\n</code></pre>"


def test_triple_backtick_with_language_hint() -> None:
    result = markdown_to_telegram_html("```python\nx = 1\n```")
    assert result == "<pre><code>x = 1\n</code></pre>"


def test_code_block_escapes_html_entities() -> None:
    result = markdown_to_telegram_html("```\n<script>alert('x')</script>\n```")
    assert "&lt;script&gt;" in result
    assert "&lt;/script&gt;" in result
    assert result.startswith("<pre><code>")
    assert result.endswith("</code></pre>")


def test_inline_code() -> None:
    assert markdown_to_telegram_html("use `foo` here") == "use <code>foo</code> here"


def test_inline_code_escapes_html_entities() -> None:
    assert markdown_to_telegram_html("`<tag>`") == "<code>&lt;tag&gt;</code>"


# ---------------------------------------------------------------------------
# markdown_to_telegram_html — HTML escaping, headers, quotes, lists
# ---------------------------------------------------------------------------


def test_html_entities_escaped_in_plain_text() -> None:
    assert markdown_to_telegram_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_raw_html_tags_escaped() -> None:
    assert markdown_to_telegram_html("<script>x</script>") == "&lt;script&gt;x&lt;/script&gt;"


def test_header_stripped_keeps_text() -> None:
    assert markdown_to_telegram_html("# Title") == "Title"


def test_deep_header_stripped() -> None:
    assert markdown_to_telegram_html("### Sub") == "Sub"


def test_blockquote_stripped() -> None:
    assert markdown_to_telegram_html("> quoted") == "quoted"


def test_bullet_list_dash() -> None:
    assert markdown_to_telegram_html("- one\n- two") == "• one\n• two"


def test_bullet_list_star() -> None:
    assert markdown_to_telegram_html("* one\n* two") == "• one\n• two"


# ---------------------------------------------------------------------------
# strip_to_plain
# ---------------------------------------------------------------------------


def test_strip_to_plain_empty() -> None:
    assert strip_to_plain("") == ""


def test_strip_to_plain_removes_bold_asterisks() -> None:
    assert strip_to_plain("**bold** text") == "bold text"


def test_strip_to_plain_removes_bold_underscores() -> None:
    assert strip_to_plain("__bold__ text") == "bold text"


def test_strip_to_plain_removes_italic() -> None:
    assert strip_to_plain("this is _italic_ text") == "this is italic text"


def test_strip_to_plain_preserves_identifier_underscores() -> None:
    assert strip_to_plain("my_var_name") == "my_var_name"


def test_strip_to_plain_removes_strikethrough() -> None:
    assert strip_to_plain("~~gone~~") == "gone"


def test_strip_to_plain_removes_links_keeps_text() -> None:
    assert strip_to_plain("[click](https://example.com)") == "click"


def test_strip_to_plain_removes_code_block_fence() -> None:
    assert strip_to_plain("```\nhello\n```") == "hello\n"


def test_strip_to_plain_removes_inline_code_backticks() -> None:
    assert strip_to_plain("use `foo` here") == "use foo here"


def test_strip_to_plain_strips_headers() -> None:
    assert strip_to_plain("# Title") == "Title"


def test_strip_to_plain_strips_blockquote() -> None:
    assert strip_to_plain("> quoted") == "quoted"


def test_strip_to_plain_converts_bullets() -> None:
    assert strip_to_plain("- one\n- two") == "• one\n• two"
