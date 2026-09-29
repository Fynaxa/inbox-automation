"""Actions, including the plug-in registry and dry-run behaviour."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inbox_automation.actions import (ActionContext, ActionError, available, build,
                                      build_all, describe)


class FakeMailbox:
    """Stands in for the IMAP connection; records instead of talking to a server."""

    def __init__(self) -> None:
        self.moved: list[tuple[str, str]] = []
        self.seen: list[str] = []

    def move(self, uid: str, folder: str) -> None:
        self.moved.append((uid, folder))

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)


def context(**overrides: object) -> ActionContext:
    values: dict[str, object] = {
        "message_id": "order-1@example.com",
        "record": {"message_id": "order-1@example.com", "fields": {"order_id": "A-1"}},
        "uid": "42",
    }
    values.update(overrides)
    return ActionContext(**values)  # type: ignore[arg-type]


def test_write_json_appends_one_line_per_record(tmp_path):
    target = tmp_path / "nested" / "orders.jsonl"
    action = build({"type": "write_json", "path": str(target)})
    action(context())
    action(context())
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["fields"] == {"order_id": "A-1"}


def test_write_json_turns_an_io_error_into_a_retryable_failure(tmp_path):
    action = build({"type": "write_json", "path": str(tmp_path)})  # a directory
    with pytest.raises(ActionError):
        action(context())


def test_move_and_mark_seen_use_the_mailbox():
    mailbox = FakeMailbox()
    for action in build_all([{"type": "mark_seen"}, {"type": "move", "folder": "Done/Orders"}]):
        action(context(mailbox=mailbox))
    assert mailbox.seen == ["42"]
    assert mailbox.moved == [("42", "Done/Orders")]


def test_dry_run_changes_nothing(tmp_path):
    mailbox = FakeMailbox()
    target = tmp_path / "orders.jsonl"
    specs = [{"type": "write_json", "path": str(target)}, {"type": "move", "folder": "Done"}]
    for action in build_all(specs):
        action(context(mailbox=mailbox, dry_run=True))
    assert not target.exists()
    assert mailbox.moved == []


def test_actions_without_a_mailbox_do_not_explode():
    build({"type": "move", "folder": "Done"})(context(uid=None))


def test_unknown_action_type_is_rejected():
    with pytest.raises(ActionError, match="unknown action type"):
        build({"type": "teleport"})


def test_move_without_a_folder_is_rejected():
    with pytest.raises(ActionError, match="folder"):
        build({"type": "move"})


def test_webhook_url_must_be_http():
    with pytest.raises(ActionError, match="http"):
        build({"type": "webhook", "url": "file:///etc/passwd"})


def test_registry_and_describe():
    assert {"move", "mark_seen", "write_json", "webhook"} <= set(available())
    assert describe([{"type": "move"}, {"type": "mark_seen"}]) == "move, mark_seen"
    assert describe([]) == "none"
