"""The offline commands: they are what a reviewer runs first."""

from __future__ import annotations

import json

from inbox_automation.cli import main

from conftest import FIXTURES, ROOT

CONFIG = ["-c", str(ROOT / "rules.example.toml")]


def test_parse_reports_the_classification(capsys):
    assert main(CONFIG + ["parse", str(FIXTURES / "order_multipart.eml")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["category"] == "order.confirmation"
    assert report["fields"]["order_id"] == "A-10042"
    assert report["missing"] == []
    assert report["record"]["from"] == "bestellungen@shop.example.com"


def test_parse_shows_attachments_and_synthetic_ids(capsys):
    files = [str(FIXTURES / "order_with_attachment.eml"), str(FIXTURES / "support_no_message_id.eml")]
    assert main(CONFIG + ["parse", *files]) == 0
    first, second = capsys.readouterr().out.split("}\n{")
    assert "Rechnung.pdf" in first
    assert '"synthetic_id": true' in second


def test_check_lists_the_rules_in_order(capsys):
    assert main(CONFIG + ["check"]) == 0
    out = capsys.readouterr().out
    assert out.index("order-confirmation") < out.index("invoice-incoming")
    assert "write_json" in out


def test_a_broken_config_exits_with_code_two(capsys, tmp_path):
    broken = tmp_path / "rules.toml"
    broken.write_text("[[rules]]\nname = 'x'\n", encoding="utf-8")
    assert main(["-c", str(broken), "check"]) == 2


def test_stats_and_reset_speak_json(capsys, tmp_path):
    config = tmp_path / "rules.toml"
    config.write_text(
        f"[settings]\nstate_db = '{tmp_path / 'ledger.sqlite3'}'\n\n"
        "[[rules]]\nname = 'x'\ncategory = 'c'\nsubject = 'x'\n", encoding="utf-8")
    assert main(["-c", str(config), "stats"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert main(["-c", str(config), "reset", "unknown@example.com"]) == 1
