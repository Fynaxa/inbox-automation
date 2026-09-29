"""Actions are plug-ins: a factory registered under a name, called per match.

Two operational notes that matter more than the code:

* Actions run in the order they appear in the rule. Put the irreversible ones
  (`move`) last, so a failing webhook does not leave a mail that has already
  been filed away.
* A failing action raises, the mail is not marked done and is retried on the
  next pass. Actions should therefore tolerate being run twice.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class ActionError(RuntimeError):
    """Raised when an action failed in a way that should be retried later."""


class MailboxOps(Protocol):
    """The slice of the IMAP client that actions are allowed to touch."""

    def move(self, uid: str, folder: str) -> None: ...

    def mark_seen(self, uid: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ActionContext:
    message_id: str
    record: dict[str, object]
    uid: str | None = None
    mailbox: MailboxOps | None = None
    dry_run: bool = False


Action = Callable[[ActionContext], None]
Factory = Callable[[Mapping[str, object]], Action]
_REGISTRY: dict[str, Factory] = {}


def register(name: str) -> Callable[[Factory], Factory]:
    """Decorator for custom actions. See the README for a six-line example."""

    def decorate(factory: Factory) -> Factory:
        _REGISTRY[name] = factory
        return factory

    return decorate


def available() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def build(spec: Mapping[str, object]) -> Action:
    name = str(spec.get("type", "")).strip()
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ActionError(f"unknown action type {name!r}; available: {', '.join(available())}")
    return factory(spec)


def build_all(specs: Iterable[Mapping[str, object]]) -> tuple[Action, ...]:
    return tuple(build(spec) for spec in specs)


def _required(spec: Mapping[str, object], key: str) -> str:
    value = spec.get(key)
    if not value:
        raise ActionError(f"action {spec.get('type')!r} needs a {key!r}")
    return str(value)


@register("move")
def _move(spec: Mapping[str, object]) -> Action:
    folder = _required(spec, "folder")

    def action(ctx: ActionContext) -> None:
        if ctx.dry_run or ctx.mailbox is None or ctx.uid is None:
            log.info("would move message to %s", folder)
            return
        ctx.mailbox.move(ctx.uid, folder)

    return action


@register("mark_seen")
def _mark_seen(_spec: Mapping[str, object]) -> Action:
    def action(ctx: ActionContext) -> None:
        if ctx.dry_run or ctx.mailbox is None or ctx.uid is None:
            log.info("would mark message as seen")
            return
        ctx.mailbox.mark_seen(ctx.uid)

    return action


@register("write_json")
def _write_json(spec: Mapping[str, object]) -> Action:
    path = Path(_required(spec, "path"))

    def action(ctx: ActionContext) -> None:
        line = json.dumps(ctx.record, ensure_ascii=False, sort_keys=True)
        if ctx.dry_run:
            log.info("would append one record to %s", path)
            return
        try:
            if path.parent.name:
                path.parent.mkdir(parents=True, exist_ok=True)
            # Append mode: a short line is written in one go, so a crash cannot
            # interleave two records.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise ActionError(f"cannot write {path}: {exc}") from exc

    return action


@register("webhook")
def _webhook(spec: Mapping[str, object]) -> Action:
    url = _required(spec, "url")
    if not url.startswith(("http://", "https://")):
        raise ActionError(f"webhook url must be http(s), got {url!r}")
    timeout = float(spec.get("timeout_seconds", 10))
    attempts = int(spec.get("attempts", 3))
    extra = spec.get("headers", {})
    headers = {"Content-Type": "application/json"}
    if isinstance(extra, Mapping):
        headers.update({str(k): str(v) for k, v in extra.items()})

    def action(ctx: ActionContext) -> None:
        payload = json.dumps(ctx.record, ensure_ascii=False).encode("utf-8")
        if ctx.dry_run:
            log.info("would POST %d bytes to %s", len(payload), url)
            return
        last = ""
        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    log.info("webhook %s -> %s", url, response.status)
                    return
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code < 500 and exc.code != 429:
                    raise ActionError(f"webhook rejected the record: {last}") from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = str(exc)
            log.warning("webhook attempt %d/%d failed: %s", attempt, attempts, last)
        raise ActionError(f"webhook failed after {attempts} attempts: {last}")

    return action


def describe(specs: Sequence[Mapping[str, object]]) -> str:
    return ", ".join(str(spec.get("type", "?")) for spec in specs) or "none"
