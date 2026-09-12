"""CLI.

`python -m family_ea [serve]` runs bot + web; `web` runs the web alone on the local db and
prints a login link; `chat --as oleh` is a REPL; `pull` downloads a snapshot of the deployed
database; `backup` zips a snapshot with the notes as Markdown; `log` prints messages with
what the LLM did.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_settings
from .main import backup, chat, files_next_to, pull, serve, setup_logging, show_log, web


def main() -> None:
    parser = argparse.ArgumentParser(prog="family_ea")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run Telegram bot and web view (default)")
    web_parser = sub.add_parser("web", help="run the web view alone on the local db, no bot")
    web_parser.add_argument("--as", dest="as_user", help="log in as this member (default: admin)")
    chat_parser = sub.add_parser("chat", help="talk to the pipeline from the terminal")
    chat_parser.add_argument("--as", dest="as_user", required=True, help="family member id")
    chat_parser.add_argument("--name", help="display name; creates the member if it is new")
    pull_parser = sub.add_parser("pull", help="download a snapshot of the deployed database")
    pull_parser.add_argument("--url", help="web view URL (default: WEB_URL)")
    pull_parser.add_argument(
        "--to", type=Path, default=Path("data/prod.db"), help="where to save it"
    )
    backup_parser = sub.add_parser(
        "backup", help="zip a database snapshot, the notes as .md and the files"
    )
    backup_parser.add_argument("--db", type=Path, help="database file (default: DATABASE_PATH)")
    backup_parser.add_argument(
        "--files",
        type=Path,
        help="its files (default: FILES_DIR, or <db>-files/ next to a --db, where pull puts them)",
    )
    backup_parser.add_argument(
        "--to", type=Path, default=Path("data/backups"), help="directory for the archive"
    )
    log_parser = sub.add_parser("log", help="print messages with what the LLM did")
    log_parser.add_argument("--db", type=Path, help="database file (default: DATABASE_PATH)")
    log_parser.add_argument("--last", type=int, default=50, help="how many messages")
    args = parser.parse_args()

    setup_logging()
    settings = load_settings()
    if args.command == "web":
        asyncio.run(web(settings, args.as_user))
    elif args.command == "chat":
        asyncio.run(chat(settings, args.as_user, args.name))
    elif args.command == "pull":
        pull(settings, args.to, args.url)
    elif args.command == "backup":
        files = args.files or (files_next_to(args.db) if args.db else settings.files_dir)
        backup(settings, args.db or settings.database_path, files, args.to)
    elif args.command == "log":
        show_log(settings, args.db or settings.database_path, args.last)
    else:
        asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
