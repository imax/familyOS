"""CLI.

`python -m family_ea [serve]` runs bot + web; `python -m family_ea chat --as oleh` is a REPL.
"""

from __future__ import annotations

import argparse
import asyncio

from .config import load_settings
from .main import chat, serve, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(prog="family_ea")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run Telegram bot and web view (default)")
    chat_parser = sub.add_parser("chat", help="talk to the pipeline from the terminal")
    chat_parser.add_argument("--as", dest="as_user", required=True, help="family member id")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    if args.command == "chat":
        asyncio.run(chat(settings, args.as_user))
    else:
        asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
