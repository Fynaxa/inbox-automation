"""Configuration: rules come from a TOML file, credentials only from the environment.

Splitting the two is deliberate. The rule file is meant to be committed and
reviewed; anything secret stays in the process environment, so a repository can
never leak a mailbox password.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PATTERN_FLAGS = re.IGNORECASE | re.MULTILINE
SCOPES = ("full", "new")

_SETTING_KEYS = {
    "state_db", "search", "poll_seconds", "use_idle", "idle_timeout_seconds",
    "max_attempts", "log_message_ids",
}
_RULE_KEYS = {
    "name", "category", "priority", "scope", "from", "subject", "body",
    "extract", "fields", "require", "actions",
}


class ConfigError(ValueError):
    """A config file that parses as TOML but does not describe a usable setup."""


@dataclass(frozen=True, slots=True)
class Credentials:
    host: str
    user: str
    password: str
    folder: str = "INBOX"
    port: int = 993

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Credentials:
        env = os.environ if env is None else env
        missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            host=env["IMAP_HOST"],
            user=env["IMAP_USER"],
            password=env["IMAP_PASSWORD"],
            folder=env.get("IMAP_FOLDER", "INBOX"),
            port=int(env.get("IMAP_PORT", "993")),
        )

    def __repr__(self) -> str:  # keeps the password out of tracebacks and logs
        return f"Credentials(host={self.host!r}, user={self.user!r}, folder={self.folder!r})"


@dataclass(frozen=True, slots=True)
class Settings:
    state_db: Path = Path("state/processed.sqlite3")
    search: str = "UNSEEN"
    poll_seconds: int = 60
    use_idle: bool = True
    idle_timeout_seconds: int = 840
    max_attempts: int = 3
    log_message_ids: bool = False


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    category: str
    order: int = 0
    priority: int = 0
    scope: str = "full"
    sender: re.Pattern[str] | None = None
    subject: re.Pattern[str] | None = None
    body: re.Pattern[str] | None = None
    extract: tuple[re.Pattern[str], ...] = ()
    fields: Mapping[str, str] = field(default_factory=dict)
    require: tuple[str, ...] = ()
    actions: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class Config:
    settings: Settings
    rules: tuple[Rule, ...]
    source: Path | None = None


def load_config(path: str | Path) -> Config:
    """Read a rule file. Raises ConfigError with the offending rule named."""
    source = Path(path)
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {source}: {exc}") from exc
    return parse_config(raw, source=source)


def parse_config(raw: Mapping[str, object], source: Path | None = None) -> Config:
    options = raw.get("settings", {})
    if not isinstance(options, Mapping):
        raise ConfigError("[settings] must be a table")
    settings = _settings(options)
    entries = raw.get("rules", [])
    if not isinstance(entries, list) or not entries:
        raise ConfigError("no [[rules]] defined - the tool would classify nothing")
    rules = [_rule(entry, index) for index, entry in enumerate(entries)]
    names = [r.name for r in rules]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"duplicate rule names: {', '.join(duplicates)}")
    rules.sort(key=lambda r: (-r.priority, r.order))
    return Config(settings=settings, rules=tuple(rules), source=source)


def _settings(raw: Mapping[str, object]) -> Settings:
    _reject_unknown(raw, _SETTING_KEYS, "[settings]")
    return Settings(
        state_db=Path(str(raw.get("state_db", "state/processed.sqlite3"))),
        search=str(raw.get("search", "UNSEEN")),
        poll_seconds=_positive_int(raw, "poll_seconds", 60),
        use_idle=bool(raw.get("use_idle", True)),
        idle_timeout_seconds=_positive_int(raw, "idle_timeout_seconds", 840),
        max_attempts=_positive_int(raw, "max_attempts", 3),
        log_message_ids=bool(raw.get("log_message_ids", False)),
    )


def _rule(raw: object, index: int) -> Rule:
    if not isinstance(raw, Mapping):
        raise ConfigError(f"rule #{index + 1} must be a table")
    name = str(raw.get("name") or f"rule-{index + 1}")
    _reject_unknown(raw, _RULE_KEYS, f"rule {name!r}")
    category = raw.get("category")
    if not category:
        raise ConfigError(f"rule {name!r}: category is required")
    scope = str(raw.get("scope", "full"))
    if scope not in SCOPES:
        raise ConfigError(f"rule {name!r}: scope must be one of {SCOPES}, got {scope!r}")
    patterns = tuple(_compile(p, name) for p in _as_list(raw.get("extract", ()), name, "extract"))
    if not (raw.get("from") or raw.get("subject") or raw.get("body")):
        raise ConfigError(f"rule {name!r}: needs at least one of from/subject/body")
    fields = raw.get("fields", {})
    if not isinstance(fields, Mapping):
        raise ConfigError(f"rule {name!r}: [rules.fields] must be a table")
    actions = raw.get("actions", [])
    if not isinstance(actions, list) or not all(isinstance(a, Mapping) for a in actions):
        raise ConfigError(f"rule {name!r}: [[rules.actions]] must be a list of tables")
    return Rule(
        name=name,
        category=str(category),
        order=index,
        priority=int(raw.get("priority", 0)),
        scope=scope,
        sender=_compile(raw.get("from"), name),
        subject=_compile(raw.get("subject"), name),
        body=_compile(raw.get("body"), name),
        extract=patterns,
        fields={str(k): str(v) for k, v in fields.items()},
        require=tuple(str(x) for x in _as_list(raw.get("require", ()), name, "require")),
        actions=tuple(dict(a) for a in actions),
    )


def _compile(pattern: object, rule_name: str) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    try:
        return re.compile(str(pattern), PATTERN_FLAGS)
    except re.error as exc:
        raise ConfigError(f"rule {rule_name!r}: bad regex {pattern!r}: {exc}") from exc


def _as_list(value: object, rule_name: str, key: str) -> list[object]:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [value]
    raise ConfigError(f"rule {rule_name!r}: {key} must be a string or a list of strings")


def _positive_int(raw: Mapping[str, object], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"[settings] {key} must be a positive integer, got {value!r}")
    return value


def _reject_unknown(raw: Mapping[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")
