"""Miki, a friendly Telegram forum-topic sorter bot."""

import os

# Opt into python-telegram-bot's future timedelta semantics now (avoids a
# deprecation warning today and a breaking change later). Our reliability layer
# already accepts both timedelta and numeric retry_after values. Must be set
# before any ``telegram`` import reads it, so it lives here in the package root.
os.environ.setdefault("PTB_TIMEDELTA", "1")
