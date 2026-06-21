from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


async def show_ids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat:
        return

    settings = context.bot_data.get("settings")
    if settings is not None and (user is None or user.id not in settings.admin_user_ids):
        await message.reply_text("You are not authorized to inspect Miki's Telegram IDs.")
        return

    details = format_update_ids(update)
    print()
    print(details)
    print("-" * 40, flush=True)
    await message.reply_text(details)


def format_update_ids(update: Update) -> str:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None:
        return "No Telegram message context is available."

    chat_name = (
        getattr(chat, "title", None)
        or getattr(chat, "full_name", None)
        or getattr(chat, "username", None)
        or ""
    )
    topic_id = getattr(message, "message_thread_id", None)
    return "\n".join(
        (
            f"chat_id: {chat.id}",
            f"chat_type: {getattr(chat, 'type', 'unknown')}",
            f"chat_name: {chat_name}",
            f"topic_id: {topic_id if topic_id is not None else 'none'}",
            f"message_id: {message.message_id}",
            f"user_id: {user.id if user is not None else 'unknown'}",
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print Telegram chat IDs and forum topic IDs from incoming messages."
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Telegram bot token. Defaults to BOT_TOKEN from .env or environment.",
    )
    return parser.parse_args()


def main() -> None:
    # Console-script entry points execute from the installed package, so
    # python-dotenv's implicit stack-based search may miss the caller's .env.
    # The listener is intentionally launched from the project directory.
    load_dotenv(dotenv_path=Path.cwd() / ".env")
    args = parse_args()
    token = args.token or os.getenv("BOT_TOKEN")

    if not token:
        raise SystemExit("Missing bot token. Set BOT_TOKEN in .env or pass --token.")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("show_ids", show_ids))
    application.add_handler(CommandHandler("where", show_ids))

    print(
        "Listening for /show_ids or /where. "
        "Send either command in the Telegram topic you want to identify."
    )
    print("Press Ctrl+C to stop.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
