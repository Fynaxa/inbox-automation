"""The messy-mail tests: charsets, MIME layout, quoting, attachments."""

from __future__ import annotations

from inbox_automation.parsing import decode_bytes, html_to_text, parse_message, tidy

from conftest import eml


def test_multipart_prefers_plain_text_and_decodes_umlauts():
    mail = parse_message(eml("order_multipart.eml"))
    assert mail.subject == "Bestellbestätigung 10042"
    assert "Müller" in mail.text
    assert "Grüßen" in mail.text
    assert "<p>" not in mail.text  # the HTML alternative is not used
    assert mail.message_id == "order-10042@shop.example.com"
    assert mail.sender == "bestellungen@shop.example.com"
    assert mail.sender_name == "Shop Beispiel GmbH"
    assert mail.date is not None and mail.date.year == 2026


def test_broken_charset_label_is_repaired():
    mail = parse_message(eml("order_broken_charset.eml"))
    assert mail.subject == "Bestellbestätigung 10043"  # header says x-unknown
    assert "Schöne" in mail.text  # body says unknown-8bit, bytes are cp1252
    assert "Grüße" in mail.text


def test_charset_label_is_not_trusted_when_the_bytes_say_utf8():
    mail = parse_message(eml("order_mislabeled_utf8.eml"))
    assert "Überall" in mail.text
    assert mail.subject == "Bestellbestätigung 10077"


def test_html_only_mail_is_converted_to_text():
    mail = parse_message(eml("order_html_only.eml"))
    assert mail.html_only is True
    assert "trackOpen" not in mail.text
    assert "font-family" not in mail.text
    assert "Bestellbestätigung" in mail.text
    assert "Weiß" in mail.text and "Grüßen" in mail.text
    assert "A-10088" in mail.text


def test_attachments_are_recorded_but_never_decoded():
    mail = parse_message(eml("order_with_attachment.eml"))
    assert [a.filename for a in mail.attachments] == ["Rechnung.pdf"]
    assert mail.attachments[0].content_type == "application/pdf"
    assert mail.attachments[0].size > 0
    assert "JVBERi" not in mail.text  # the base64 payload never reaches the text
    assert "A-10050" in mail.text


def test_forward_keeps_history_in_full_text_but_not_in_new_text():
    mail = parse_message(eml("order_forwarded.eml"))
    assert "A-10099" in mail.text
    assert "A-10099" not in mail.text_new
    assert mail.text_new == "Hallo, kannst du das bitte pruefen?"


def test_missing_message_id_gets_a_stable_synthetic_one():
    raw = eml("support_no_message_id.eml")
    first, second = parse_message(raw), parse_message(raw)
    assert first.synthetic_id is True
    assert first.message_id.startswith("sha256:")
    assert first.message_id == second.message_id


def test_crlf_and_invisible_characters_are_normalised():
    assert "\r" not in parse_message(eml("order_multipart.eml")).text
    assert tidy("a b\r\n\r\n\r\n\r\nc­ ") == "a b\n\nc"


def test_decode_bytes_survives_a_nonsense_charset():
    assert decode_bytes("Grüße".encode(), "totally-made-up") == "Grüße"
    assert decode_bytes(b"Gr\xfc\xdfe", "iso-8859-1") == "Grüße"
    assert decode_bytes(b"\xff\xfe\xfa", "utf-8")  # never raises


def test_html_to_text_keeps_structure():
    text = html_to_text("<p>eins</p><ul><li>zwei</li><li>drei</li></ul><script>x()</script>")
    assert "x()" not in text
    assert [line for line in text.splitlines() if line] == ["eins", "- zwei", "- drei"]


def test_unparseable_input_does_not_raise():
    mail = parse_message(b"this is not a mail at all")
    assert mail.text == "this is not a mail at all"  # read as a body without headers
    assert mail.subject == "" and mail.sender == ""
    assert mail.synthetic_id is True
