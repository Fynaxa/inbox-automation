"""Configuration parsing: the errors are the interesting part."""

from __future__ import annotations

import pytest

from inbox_automation.config import ConfigError, Credentials, load_config, parse_config

from conftest import ROOT

MINIMAL = {"name": "r", "category": "c", "subject": "x"}


def test_example_config_loads():
    config = load_config(ROOT / "rules.example.toml")
    assert len(config.rules) == 4
    assert config.settings.search == "UNSEEN"
    assert config.settings.state_db.name == "processed.sqlite3"


def test_unknown_key_is_named():
    with pytest.raises(ConfigError, match="prioritiy"):
        parse_config({"rules": [MINIMAL | {"prioritiy": 10}]})


def test_bad_regex_names_the_rule():
    with pytest.raises(ConfigError, match="broken"):
        parse_config({"rules": [{"name": "broken", "category": "c", "subject": "([a-z"}]})


def test_duplicate_rule_names_are_refused():
    with pytest.raises(ConfigError, match="duplicate"):
        parse_config({"rules": [MINIMAL, MINIMAL]})


def test_a_rule_needs_at_least_one_condition():
    with pytest.raises(ConfigError, match="from/subject/body"):
        parse_config({"rules": [{"name": "empty", "category": "c"}]})


def test_a_rule_needs_a_category():
    with pytest.raises(ConfigError, match="category"):
        parse_config({"rules": [{"name": "n", "subject": "x"}]})


def test_unknown_scope_is_refused():
    with pytest.raises(ConfigError, match="scope"):
        parse_config({"rules": [MINIMAL | {"scope": "sideways"}]})


def test_settings_must_be_sane():
    with pytest.raises(ConfigError, match="poll_seconds"):
        parse_config({"settings": {"poll_seconds": 0}, "rules": [MINIMAL]})


def test_an_empty_rule_set_is_refused():
    with pytest.raises(ConfigError, match="no \\[\\[rules\\]\\]"):
        parse_config({"rules": []})


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "nope.toml")


def test_credentials_come_from_the_environment():
    credentials = Credentials.from_env({
        "IMAP_HOST": "imap.example.com",
        "IMAP_USER": "orders@example.com",
        "IMAP_PASSWORD": "hunter2",
        "IMAP_FOLDER": "Inbox/Orders",
    })
    assert credentials.port == 993
    assert credentials.folder == "Inbox/Orders"
    assert "hunter2" not in repr(credentials)  # passwords stay out of tracebacks


def test_missing_environment_variables_are_named():
    with pytest.raises(ConfigError, match="IMAP_PASSWORD"):
        Credentials.from_env({"IMAP_HOST": "h", "IMAP_USER": "u"})
