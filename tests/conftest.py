"""Shared test setup. Everything here is offline: no sockets, no real mailboxes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from inbox_automation.actions import ActionContext, ActionError, register
from inbox_automation.config import Config, load_config, parse_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

COLLECTED: list[dict[str, object]] = []


@register("collect")
def _collect(_spec: Mapping[str, object]):
    """A plug-in action that only remembers what it was handed."""

    def action(ctx: ActionContext) -> None:
        COLLECTED.append(dict(ctx.record))

    return action


@register("explode")
def _explode(_spec: Mapping[str, object]):
    """A plug-in action that always fails, to exercise the retry path."""

    def action(_ctx: ActionContext) -> None:
        raise ActionError("this action always fails")

    return action


def eml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_config(tmp_path: Path, actions: Sequence[Mapping[str, object]] | None = None,
                settings: Mapping[str, object] | None = None, **rule_overrides: object) -> Config:
    rule: dict[str, object] = {
        "name": "collect-orders",
        "category": "order.confirmation",
        "subject": "bestellbest",
        "extract": [r"Bestellnummer:\s*(?P<order_id>[A-Z0-9-]{4,})"],
        "actions": list(actions) if actions is not None else [{"type": "collect"}],
    }
    rule.update(rule_overrides)
    options: dict[str, object] = {"state_db": str(tmp_path / "ledger.sqlite3")}
    options.update(settings or {})
    return parse_config({"settings": options, "rules": [rule]})


@pytest.fixture
def example_config() -> Config:
    return load_config(ROOT / "rules.example.toml")


@pytest.fixture
def collected() -> list[dict[str, object]]:
    COLLECTED.clear()
    yield COLLECTED
    COLLECTED.clear()
