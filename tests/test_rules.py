"""Classification, priority and field extraction."""

from __future__ import annotations

from dataclasses import replace

import pytest

from inbox_automation.config import parse_config
from inbox_automation.parsing import parse_message
from inbox_automation.rules import classify, date_de, decimal_de, extract

from conftest import eml


def test_extracts_and_converts_every_field(example_config):
    mail = parse_message(eml("order_multipart.eml"))
    match = classify(mail, example_config.rules)
    assert match is not None
    assert match.rule.name == "order-confirmation"
    assert match.complete
    assert match.fields == {
        "order_id": "A-10042",
        "amount": "1234.56",
        "currency": "EUR",
        "order_date": "2026-09-19",
    }


def test_priority_decides_when_two_rules_match(example_config):
    mail = parse_message(eml("invoice_dunning.eml"))
    match = classify(mail, example_config.rules)
    assert match is not None
    assert match.rule.name == "invoice-dunning"  # beats invoice-incoming on priority
    assert match.fields["invoice_no"] == "2026-0815"  # taken from the subject line
    assert match.fields["dunning_level"] == "2"
    assert match.fields["amount"] == "1499.00"


def test_rules_are_ordered_by_priority_then_declaration(example_config):
    assert [r.name for r in example_config.rules] == [
        "order-confirmation", "invoice-dunning", "support-request", "invoice-incoming",
    ]


def test_support_mail_is_matched_on_the_body(example_config):
    match = classify(parse_message(eml("support_no_message_id.eml")), example_config.rules)
    assert match is not None
    assert match.rule.category == "support.request"
    assert match.fields["customer_no"] == "884412"


def test_newsletter_matches_nothing(example_config):
    assert classify(parse_message(eml("newsletter_unmatched.eml")), example_config.rules) is None


def test_forward_is_not_filed_as_an_order(example_config):
    # The order data sits in the quoted part, but the sender is not the shop.
    assert classify(parse_message(eml("order_forwarded.eml")), example_config.rules) is None


def test_scope_decides_whether_quoted_history_is_read(example_config):
    mail = parse_message(eml("order_forwarded.eml"))
    rule = next(r for r in example_config.rules if r.name == "order-confirmation")
    wide, _ = extract(mail, rule)
    narrow, _ = extract(mail, replace(rule, scope="new"))
    assert wide["order_id"] == "A-10099"
    assert "order_id" not in narrow


def test_required_fields_are_reported_as_missing():
    config = parse_config({"rules": [{
        "name": "strict",
        "category": "order.confirmation",
        "subject": "bestellbest",
        "extract": [r"Lieferschein:\s*(?P<delivery_note>\d+)"],
        "require": ["delivery_note"],
    }]})
    match = classify(parse_message(eml("order_multipart.eml")), config.rules)
    assert match is not None
    assert match.missing == ("delivery_note",)
    assert not match.complete


def test_a_failing_converter_keeps_the_raw_value():
    config = parse_config({"rules": [{
        "name": "broken-converter",
        "category": "order.confirmation",
        "subject": "bestellbest",
        "extract": [r"Bestellnummer:\s*(?P<order_id>[A-Z0-9-]+)"],
        "fields": {"order_id": "decimal_de"},
    }]})
    match = classify(parse_message(eml("order_multipart.eml")), config.rules)
    assert match is not None and match.fields["order_id"] == "A-10042"


@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", "1234.56"),
    ("89,90", "89.90"),
    ("1.499", "1499"),
    ("12.34", "12.34"),
    ("2 000,00", "2000.00"),
    ("1,234.56", "1234.56"),
    ("1.234,56 EUR", "1234.56"),
])
def test_decimal_de(raw, expected):
    assert decimal_de(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("19.09.2026", "2026-09-19"),
    ("5.1.26", "2026-01-05"),
    ("19. September 2026", "2026-09-19"),
    ("2026-09-19", "2026-09-19"),
])
def test_date_de(raw, expected):
    assert date_de(raw) == expected


def test_converters_reject_nonsense():
    with pytest.raises(ValueError):
        decimal_de("keine Zahl")
    with pytest.raises(ValueError):
        date_de("32.13.2026")
