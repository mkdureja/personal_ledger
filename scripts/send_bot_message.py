"""Send one plain-text Telegram message using the Ledger bot token."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
MAX_TELEGRAM_TEXT_UNITS = 4096


def _positive_chat_id(value: str) -> int:
    try:
        chat_id = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("chat ID must be an integer") from exc
    if chat_id <= 0:
        raise argparse.ArgumentTypeError("chat ID must be positive")
    return chat_id


def _allowed_user_ids(raw: str) -> frozenset[int]:
    try:
        return frozenset(
            int(value.strip()) for value in raw.split(",") if value.strip()
        )
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_IDS contains a non-integer value") from exc


def _telegram_text_units(text: str) -> int:
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


async def _send(token: str, chat_id: int, text: str) -> int:
    async with Bot(token=token) as bot:
        sent = await bot.send_message(chat_id=chat_id, text=text)
        return sent.message_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send one plain-text message from the Ledger bot."
    )
    parser.add_argument(
        "--chat-id",
        required=True,
        type=_positive_chat_id,
        help="Ratika's numeric Telegram user ID",
    )
    parser.add_argument(
        "--message",
        help="Message text. If omitted, the script prompts without storing it in shell history.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the final confirmation prompt.",
    )
    args = parser.parse_args()

    load_dotenv(ENV_PATH, override=False)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        print(f"BOT_TOKEN is missing from {ENV_PATH}", file=sys.stderr)
        return 2

    try:
        allowed_ids = _allowed_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.chat_id not in allowed_ids:
        print(
            "Refusing to send: --chat-id is not present in ALLOWED_USER_IDS.",
            file=sys.stderr,
        )
        return 2

    text = args.message if args.message is not None else input("Message (plain text): ")
    if not text.strip():
        print("Refusing to send an empty message.", file=sys.stderr)
        return 2
    if _telegram_text_units(text) > MAX_TELEGRAM_TEXT_UNITS:
        print("Message exceeds Telegram's 4096-unit text limit.", file=sys.stderr)
        return 2

    if not args.yes:
        confirmation = input(f"Send this message to chat {args.chat_id}? [y/N]: ")
        if confirmation.strip().lower() not in {"y", "yes"}:
            print("Cancelled; nothing was sent.")
            return 0

    try:
        message_id = asyncio.run(_send(token, args.chat_id, text))
    except TelegramError as exc:
        print(f"Telegram rejected the send: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Ratika may need to open the bot and send /start first.",
            file=sys.stderr,
        )
        return 1

    print(f"Message sent successfully (Telegram message ID {message_id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
