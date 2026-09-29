"""The part that matters most: one mail is acted on exactly once."""

from __future__ import annotations

from pathlib import Path

from inbox_automation.parsing import parse_message
from inbox_automation.pipeline import Pipeline
from inbox_automation.state import DONE, Ledger

from conftest import eml, make_config


def _run(config, raw: bytes) -> str:
    """One complete process lifetime: open the ledger, handle one mail, close it."""
    with Ledger(config.settings.state_db,
                max_attempts=config.settings.max_attempts) as ledger:
        return Pipeline(config, ledger).handle(raw)


def test_the_same_mail_survives_three_restarts(tmp_path, collected):
    config = make_config(tmp_path)
    raw = eml("order_multipart.eml")
    outcomes = [_run(config, raw) for _ in range(3)]
    assert outcomes == ["processed", "skipped", "skipped"]
    assert len(collected) == 1
    assert collected[0]["fields"] == {"order_id": "A-10042"}


def test_a_crash_between_claim_and_finish_is_retried(tmp_path, collected):
    config = make_config(tmp_path)
    raw = eml("order_multipart.eml")
    message_id = parse_message(raw).message_id

    with Ledger(config.settings.state_db) as ledger:
        assert ledger.claim(message_id) is True  # the run that never came back

    assert _run(config, raw) == "processed"  # restart picks the mail up again
    assert len(collected) == 1
    with Ledger(config.settings.state_db) as ledger:
        assert ledger.status_of(message_id) == DONE


def test_a_message_that_keeps_failing_is_given_up_on(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite3", max_attempts=2)
    assert ledger.claim("stuck@example.com") is True
    assert ledger.claim("stuck@example.com") is True
    assert ledger.claim("stuck@example.com") is False
    assert ledger.counts() == {"running": 1}
    ledger.close()


def test_forget_releases_a_message(tmp_path, collected):
    config = make_config(tmp_path)
    raw = eml("order_multipart.eml")
    assert _run(config, raw) == "processed"
    with Ledger(config.settings.state_db) as ledger:
        assert ledger.forget(parse_message(raw).message_id) is True
        assert ledger.forget("never-seen@example.com") is False
    assert _run(config, raw) == "processed"
    assert len(collected) == 2


def test_unmatched_mail_is_remembered_too(tmp_path, collected):
    config = make_config(tmp_path)
    raw = eml("newsletter_unmatched.eml")
    assert _run(config, raw) == "unmatched"
    assert _run(config, raw) == "skipped"
    assert collected == []


def test_a_failing_action_leaves_the_mail_for_the_next_pass(tmp_path):
    config = make_config(tmp_path, actions=[{"type": "explode"}], settings={"max_attempts": 2})
    raw = eml("order_multipart.eml")
    assert _run(config, raw) == "failed"
    assert _run(config, raw) == "failed"  # retried, not skipped
    assert _run(config, raw) == "skipped"  # max_attempts reached, hands off


def test_the_ledger_stores_no_mail_content(tmp_path, collected):
    config = make_config(tmp_path)
    raw = eml("order_multipart.eml")
    _run(config, raw)
    db = Path(config.settings.state_db)
    blob = db.read_bytes()
    wal = Path(f"{db}-wal")
    if wal.exists():
        blob += wal.read_bytes()
    assert b"Bestellnummer" not in blob
    assert b"1.234,56" not in blob
    assert b"A-10042" not in blob
    assert parse_message(raw).message_id.encode() in blob
