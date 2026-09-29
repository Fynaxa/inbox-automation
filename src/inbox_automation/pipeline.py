"""Wiring: fetch, parse, classify, extract, act, record. In that order."""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from .actions import Action, ActionContext, ActionError, build_all
from .config import Config
from .imap_client import Mailbox, MailboxError, TransientError, backoff
from .parsing import ParsedMail, parse_message
from .rules import Match, classify
from .state import DONE, FAILED, INCOMPLETE, Ledger

log = logging.getLogger(__name__)

SKIPPED = "skipped"
UNMATCHED = "unmatched"
PROCESSED = "processed"


def validate(config: Config) -> None:
    """Build every action once, so a typo in the rule file fails at startup."""
    for rule in config.rules:
        build_all(rule.actions)


def build_record(mail: ParsedMail, match: Match) -> dict[str, object]:
    """What leaves the process. Extracted fields only, never subject or body."""
    return {
        "message_id": mail.message_id,
        "category": match.rule.category,
        "rule": match.rule.name,
        "from": mail.sender,
        "received_at": mail.date.isoformat() if mail.date else None,
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": dict(match.fields),
    }


class Pipeline:
    def __init__(self, config: Config, ledger: Ledger, mailbox: Mailbox | None = None,
                 dry_run: bool = False) -> None:
        self.config = config
        self.ledger = ledger
        self.mailbox = mailbox
        self.dry_run = dry_run
        self.stats: Counter[str] = Counter()
        validate(config)
        self._actions: dict[str, tuple[Action, ...]] = {
            rule.name: build_all(rule.actions) for rule in config.rules
        }

    def handle(self, raw: bytes, uid: str | None = None) -> str:
        """Process one raw message. Returns the outcome; never raises for bad mail."""
        mail = parse_message(raw)
        message_id = mail.message_id
        label = self._label(message_id)

        if not self.dry_run and not self.ledger.claim(message_id):
            log.debug("%s already handled, skipping", label)
            return self._count(SKIPPED)

        match = classify(mail, self.config.rules)
        if match is None:
            log.info("%s no rule matched", label)
            self._finish(message_id, DONE, reason=UNMATCHED)
            return self._count(UNMATCHED)

        if match.missing:
            log.warning("%s rule %s matched but required field(s) missing: %s",
                        label, match.rule.name, ", ".join(match.missing))
            self._finish(message_id, INCOMPLETE, match, reason="missing_fields")
            return self._count(INCOMPLETE)

        context = ActionContext(
            message_id=message_id,
            record=build_record(mail, match),
            uid=uid,
            mailbox=self.mailbox,
            dry_run=self.dry_run,
        )
        try:
            for action in self._actions[match.rule.name]:
                action(context)
        except (ActionError, MailboxError) as exc:
            log.error("%s action failed, will retry: %s", label, exc)
            self._finish(message_id, FAILED, match, reason="action_failed")
            return self._count(FAILED)

        log.info("%s -> %s via %s, %d field(s)",
                 label, match.rule.category, match.rule.name, len(match.fields))
        self._finish(message_id, DONE, match)
        return self._count(PROCESSED)

    def run_once(self) -> Counter[str]:
        """One pass over the search result. Returns the counts of this pass only."""
        if self.mailbox is None:
            raise MailboxError("run_once needs a mailbox")
        before = Counter(self.stats)
        uids = self.mailbox.search(self.config.settings.search)
        log.info("%d message(s) match %s", len(uids), self.config.settings.search)
        for uid in uids:
            raw = self.mailbox.fetch(uid)
            if raw is None:
                log.debug("uid %s disappeared before it could be fetched", uid)
                continue
            self.handle(raw, uid=uid)
        return self.stats - before

    def run_forever(self) -> None:
        """Watch loop. Reconnects with backoff; a rejected login still aborts."""
        if self.mailbox is None:
            raise MailboxError("run_forever needs a mailbox")
        attempt = 0
        while True:
            try:
                self.mailbox.connect()
                attempt = 0
                while True:
                    self.run_once()
                    self.mailbox.wait(self.config.settings.idle_timeout_seconds)
            except TransientError as exc:
                delay = backoff(attempt)
                attempt += 1
                log.warning("connection lost (%s), reconnecting in %.0fs", exc, delay)
                self.mailbox.close()
                time.sleep(delay)
            except KeyboardInterrupt:
                log.info("stopped after %s", dict(self.stats))
                self.mailbox.close()
                return

    def _finish(self, message_id: str, status: str, match: Match | None = None,
                reason: str | None = None) -> None:
        if self.dry_run:
            return
        self.ledger.finish(
            message_id,
            status,
            category=match.rule.category if match else None,
            rule=match.rule.name if match else None,
            reason=reason,
        )

    def _count(self, outcome: str) -> str:
        self.stats[outcome] += 1
        return outcome

    def _label(self, message_id: str) -> str:
        if self.config.settings.log_message_ids:
            return message_id
        # A Message-ID can carry the sender's domain, so logs get a stable digest.
        return "msg:" + hashlib.sha256(message_id.encode()).hexdigest()[:10]
