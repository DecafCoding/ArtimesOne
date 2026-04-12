"""Telegram chat surface — webhook route, streaming helper, and format converter.

Implements plan section 8: Telegram as a secondary surface for the same
``chat_agent``. The package contains three modules:

- ``format`` — markdown-to-Telegram-HTML converter
- ``stream`` — throttled edit-in-place streaming helper
- ``webhook`` — FastAPI router for ``/telegram/webhook``
"""
