"""Burner layer — Phase 1 session bootstrap (local / SSH only).

Mint a reusable Telethon ``StringSession`` once, interactively, over SSH by the
account owner. The resulting string is a **full-account credential**: it is
printed exactly once for the operator to copy into secret storage (env/secret
manager, the way ``BOT_TOKEN`` is stored) and is **never logged**.

This must never be driven through Telegram — Telegram invalidates login codes
that are shared in a chat (see ``docs/burner-layer.md``).

All Telethon imports are lazy so the optional ``burner`` extra is only required
when actually bootstrapping or validating a session.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


class TelethonNotInstalled(RuntimeError):
    """Raised when the optional ``burner`` extra (telethon) is missing."""


def _import_telethon() -> tuple[Any, Any]:
    try:
        from telethon.sessions import StringSession
        from telethon.sync import TelegramClient
    except ImportError as error:  # pragma: no cover - exercised via monkeypatch
        raise TelethonNotInstalled(
            "Telethon is not installed. Install the burner extra: "
            "pip install 'miki-a-friendly-sorter-bot[burner]'"
        ) from error
    return TelegramClient, StringSession


def create_session_interactive(api_id: int, api_hash: str) -> str:
    """Run Telethon's interactive login and return a fresh ``StringSession``.

    Prompts for phone, login code, and (if set) 2FA password on the terminal.
    The session string is returned, not printed, so the caller controls output.
    """

    TelegramClient, StringSession = _import_telethon()
    client = TelegramClient(StringSession(), api_id, api_hash)
    with client:
        client.start()  # type: ignore[attr-defined]
        if not client.is_user_authorized():  # type: ignore[attr-defined]
            raise RuntimeError("Login did not complete; account is not authorized.")
        return client.session.save()  # type: ignore[attr-defined]


def validate_session(api_id: int, api_hash: str, session: str) -> bool:
    """Return True if ``session`` is a live, authorized user session.

    Connects read-only, checks authorization, and disconnects. Raises
    :class:`TelethonNotInstalled` if the extra is missing; other connection
    errors propagate so callers can record them.
    """

    TelegramClient, StringSession = _import_telethon()
    client = TelegramClient(StringSession(session), api_id, api_hash)
    with client:
        return bool(client.is_user_authorized())  # type: ignore[attr-defined]


def main() -> None:
    # Console-script entry points run from the installed package; load the
    # caller's .env explicitly so TELETHON_API_ID/HASH are picked up.
    load_dotenv(dotenv_path=Path.cwd() / ".env")

    api_id_raw = os.getenv("TELETHON_API_ID")
    api_hash = (os.getenv("TELETHON_API_HASH") or "").strip()
    if not api_id_raw or not api_hash:
        raise SystemExit(
            "Set TELETHON_API_ID and TELETHON_API_HASH (from https://my.telegram.org) "
            "in .env or the environment before bootstrapping a session."
        )
    try:
        api_id = int(api_id_raw)
    except ValueError as error:
        raise SystemExit("TELETHON_API_ID must be an integer.") from error

    print(
        "Bootstrapping a Telegram user session for the burner.\n"
        "This logs in a real account — run it only on the droplet over SSH,\n"
        "never via a Telegram chat. You will be asked for the phone number,\n"
        "the login code Telegram sends, and your 2FA password if set.\n"
    )

    try:
        session = create_session_interactive(api_id, api_hash)
    except TelethonNotInstalled as error:
        raise SystemExit(str(error)) from error

    print("\n" + "=" * 70)
    print("Session created. Store this string the way you store BOT_TOKEN —")
    print("it grants FULL access to the account. Never log it or paste it in chat.")
    print("Set it as TELETHON_SESSION in your secret storage:\n")
    print(session)
    print("=" * 70)


if __name__ == "__main__":
    main()
