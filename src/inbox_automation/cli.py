"""Command line: run, once, parse, check, reset, stats."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .actions import available, describe
from .config import Config, ConfigError, Credentials, load_config
from .imap_client import Mailbox, MailboxError
from .parsing import parse_message
from .pipeline import Pipeline, build_record, validate
from .rules import classify
from .state import Ledger

log = logging.getLogger("inbox_automation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inbox-automation", description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=Path("rules.toml"))
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG level")
    sub = parser.add_subparsers(dest="command", required=True)

    watch = sub.add_parser("run", help="watch the mailbox until stopped")
    once = sub.add_parser("once", help="process what is there now, then exit")
    for command in (watch, once):
        command.add_argument("--dry-run", action="store_true",
                             help="classify and report, change nothing")

    offline = sub.add_parser("parse", help="classify .eml files offline, no server needed")
    offline.add_argument("files", nargs="+", type=Path)

    sub.add_parser("check", help="validate the rule file and list the rules")
    sub.add_parser("stats", help="show the ledger counts")
    released = sub.add_parser("reset", help="forget a Message-ID so it is processed again")
    released.add_argument("message_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        return _dispatch(args, config)
    except (ConfigError, MailboxError) as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.command == "check":
        return _check(config)
    if args.command == "parse":
        return _parse(args, config)
    if args.command == "stats":
        with _ledger(config) as ledger:
            print(json.dumps(ledger.counts(), indent=2, sort_keys=True))
        return 0
    if args.command == "reset":
        with _ledger(config) as ledger:
            found = ledger.forget(args.message_id)
        print("released" if found else "not in the ledger")
        return 0 if found else 1
    return _run(args, config)


def _ledger(config: Config) -> Ledger:
    return Ledger(config.settings.state_db, max_attempts=config.settings.max_attempts)


def _check(config: Config) -> int:
    validate(config)
    print(f"{len(config.rules)} rule(s), evaluated in this order:")
    for rule in config.rules:
        print(f"  [{rule.priority:>4}] {rule.name:<28} -> {rule.category:<24} "
              f"scope={rule.scope} fields={len(rule.extract)} actions={describe(rule.actions)}")
    print(f"available action types: {', '.join(available())}")
    print(f"ledger: {config.settings.state_db}")
    return 0


def _parse(args: argparse.Namespace, config: Config) -> int:
    for path in args.files:
        mail = parse_message(path.read_bytes())
        match = classify(mail, config.rules)
        report: dict[str, object] = {
            "file": str(path),
            "message_id": mail.message_id,
            "synthetic_id": mail.synthetic_id,
            "html_only": mail.html_only,
            "attachments": [a.filename for a in mail.attachments],
            "category": match.rule.category if match else None,
            "rule": match.rule.name if match else None,
            "fields": dict(match.fields) if match else {},
            "missing": list(match.missing) if match else [],
            "would_run": describe(match.rule.actions) if match else "none",
        }
        if match:
            report["record"] = build_record(mail, match)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run(args: argparse.Namespace, config: Config) -> int:
    credentials = Credentials.from_env()
    mailbox = Mailbox(credentials, config.settings)
    with _ledger(config) as ledger:
        pipeline = Pipeline(config, ledger, mailbox=mailbox, dry_run=args.dry_run)
        if args.command == "run":
            pipeline.run_forever()
            return 0
        try:
            mailbox.connect()
            counts = pipeline.run_once()
        finally:
            mailbox.close()
        print(json.dumps(dict(counts), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
