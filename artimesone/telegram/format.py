"""Markdown-to-Telegram-HTML converter and plain-text fallback.

Converts the chat agent's CommonMark markdown output into the HTML subset
that Telegram accepts (``<b>``, ``<i>``, ``<s>``, ``<code>``, ``<pre>``,
``<a href>``).  Uses the placeholder-based approach from nanobot's
``_markdown_to_telegram_html`` — extract code blocks first, HTML-escape the
body, convert inline formatting, then restore code blocks with their own
escaping.

Design decision: HTML parse mode (not MarkdownV2) — HTML requires escaping
only ``&``, ``<``, ``>`` vs MarkdownV2's 18 special characters.  This
matches nanobot's production Telegram channel.
"""

from __future__ import annotations

import re


def markdown_to_telegram_html(text: str) -> str:
    """Convert CommonMark markdown to Telegram-safe HTML.

    The conversion order matters:

    1. Extract code blocks into placeholders (protect from further processing).
    2. Extract inline code into placeholders.
    3. Strip markdown headers (keep text).
    4. Strip blockquotes (keep text).
    5. HTML-escape the remaining text (``&``, ``<``, ``>``).
    6. Convert links, bold, italic, strikethrough, bullet lists.
    7. Restore inline code with ``<code>`` tags.
    8. Restore code blocks with ``<pre><code>`` tags.
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks.
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match[str]) -> str:
        code_blocks.append(m.group(1))
        return f"\x00CB{len(code_blocks) - 1}\x00"

    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", _save_code_block, text)

    # 2. Extract and protect inline code.
    inline_codes: list[str] = []

    def _save_inline_code(m: re.Match[str]) -> str:
        inline_codes.append(m.group(1))
        return f"\x00IC{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", _save_inline_code, text)

    # 3. Strip headers — keep only the text.
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # 4. Strip blockquotes — keep only the text.
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # 5. HTML-escape special characters.
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 6a. Links [text](url) — before bold/italic to handle nested formatting.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # 6b. Bold **text** or __text__.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 6c. Italic _text_ — word-boundary checks to avoid matching identifiers
    # like ``some_var_name``.
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)

    # 6d. Strikethrough ~~text~~.
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 6e. Bullet lists — ``- item`` or ``* item`` → ``• item``.
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    # 7. Restore inline code with HTML tags (content gets its own escaping).
    for i, code in enumerate(inline_codes):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00IC{i}\x00", f"<code>{escaped}</code>")

    # 8. Restore code blocks with HTML tags (content gets its own escaping).
    for i, code in enumerate(code_blocks):
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CB{i}\x00", f"<pre><code>{escaped}</code></pre>")

    return text


def strip_to_plain(text: str) -> str:
    """Strip all markdown formatting, returning plain text.

    Used as a fallback when Telegram rejects the HTML-formatted message.
    """
    if not text:
        return ""

    # Remove code blocks — keep content.
    text = re.sub(r"```[\w]*\n?([\s\S]*?)```", r"\1", text)

    # Remove inline code backticks.
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Strip headers.
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)

    # Strip blockquotes.
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    # Remove links — keep text.
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove bold markers.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)

    # Remove italic markers (word-boundary aware).
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"\1", text)

    # Remove strikethrough markers.
    text = re.sub(r"~~(.+?)~~", r"\1", text)

    # Convert bullet markers to plain bullets.
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    return text
