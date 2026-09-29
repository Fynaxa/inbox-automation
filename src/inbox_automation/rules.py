"""Matching a mail against the rule set and pulling named fields out of it."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .config import Rule
from .parsing import ParsedMail

log = logging.getLogger(__name__)

_MONTHS_DE = {
    "januar": 1, "februar": 2, "maerz": 3, "märz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
}
_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")
# Without this guard, "A-10042" would quietly become the number -10042.
_AMOUNT = re.compile(r"(?:[€$]\s*)?[-+]?[\d.,\s ]+(?:\s*(?:EUR|USD|CHF|[€$]))?",
                     re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Match:
    """A rule that fired, with whatever it managed to extract."""

    rule: Rule
    fields: dict[str, str]
    missing: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.missing


def classify(mail: ParsedMail, rules: Iterable[Rule]) -> Match | None:
    """First rule wins. `rules` is expected pre-sorted by priority (load_config does that)."""
    for rule in rules:
        if _matches(mail, rule):
            fields, missing = extract(mail, rule)
            return Match(rule=rule, fields=fields, missing=missing)
    return None


def _matches(mail: ParsedMail, rule: Rule) -> bool:
    if rule.sender and not rule.sender.search(mail.sender):
        return False
    if rule.subject and not rule.subject.search(mail.subject):
        return False
    if rule.body and not rule.body.search(body_for(mail, rule.scope)):
        return False
    return True


def body_for(mail: ParsedMail, scope: str) -> str:
    """"full" sees quoted and forwarded text, "new" only what this sender wrote."""
    return mail.text if scope == "full" else mail.text_new


def haystack(mail: ParsedMail, scope: str) -> str:
    """Extraction runs over subject plus body: order numbers live in either one."""
    return f"Subject: {mail.subject}\n\n{body_for(mail, scope)}"


def extract(mail: ParsedMail, rule: Rule) -> tuple[dict[str, str], tuple[str, ...]]:
    text = haystack(mail, rule.scope)
    fields: dict[str, str] = {}
    for pattern in rule.extract:
        match = pattern.search(text)
        if match is None:
            continue
        for key, value in (match.groupdict() or {}).items():
            if value is not None and key not in fields:
                fields[key] = _convert(key, value.strip(), rule.fields)
    missing = tuple(name for name in rule.require if not fields.get(name))
    return fields, missing


def _convert(key: str, value: str, conversions: Mapping[str, str]) -> str:
    name = conversions.get(key)
    if not name:
        return value
    converter = CONVERTERS.get(name)
    if converter is None:
        return value
    try:
        return converter(value)
    except ValueError:
        # The field name is safe to log, the value is mail content and is not.
        log.warning("converter %s failed for field %s, keeping raw value", name, key)
        return value


def decimal_de(value: str) -> str:
    """"1.234,56 EUR" -> "1234.56". German grouping wins over the English reading."""
    if not _AMOUNT.fullmatch(value.strip()):
        raise ValueError(f"not a number: {value!r}")
    cleaned = re.sub(r"[^\d,.\-]", "", value.replace(" ", ""))
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
        cleaned = cleaned.replace("," if decimal_sep == "." else ".", "")
        cleaned = cleaned.replace(decimal_sep, ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        return str(Decimal(cleaned))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc


def date_de(value: str) -> str:
    """"19.09.2026", "19. September 2026" and ISO input all become "2026-09-19"."""
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    spelled = re.fullmatch(r"(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜ]+)\s+(\d{4})", text)
    if spelled:
        month = _MONTHS_DE.get(spelled.group(2).lower())
        if month:
            return date(int(spelled.group(3)), month, int(spelled.group(1))).isoformat()
    raise ValueError(f"not a date: {value!r}")


CONVERTERS = {
    "decimal_de": decimal_de,
    "date_de": date_de,
    "strip": lambda v: v.strip(),
    "upper": lambda v: v.strip().upper(),
    "digits": lambda v: re.sub(r"\D", "", v),
    "collapse_space": lambda v: re.sub(r"\s+", " ", v).strip(),
}
