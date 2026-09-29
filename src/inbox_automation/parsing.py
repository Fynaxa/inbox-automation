"""Turning raw RFC 5322 bytes into something a regex can be pointed at.

Real mailboxes are messier than the RFCs suggest, so this module is written
around the failures rather than around the happy path: wrong charset labels,
HTML-only newsletters, quoted-printable umlauts, forwarded threads and
attachments that must never be decoded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes, utils
from email.header import decode_header
from email.message import Message
from html.parser import HTMLParser

_FALLBACKS = ("utf-8", "cp1252", "iso-8859-1")
# Labels that carry no information; they are ignored in favour of sniffing.
_WEAK_CHARSETS = {
    "", "ascii", "us-ascii", "default", "unknown", "unknown-8bit", "x-unknown",
    "x-user-defined", "binary", "8bit", "none", "charset",
}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "table", "ul", "ol", "blockquote", "section",
    "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
}
_DROP_TAGS = {"script", "style", "head", "title"}
_QUOTED_LINE = re.compile(r"^\s*>+")
_CUT_MARKERS = (
    re.compile(r"^\s*-{2,}\s*(forwarded message|weitergeleitete nachricht|"
               r"original message|urspr(?:ü|ue)ngliche nachricht)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(on|am)\s.{5,160}?(wrote|schrieb|schreibt)\s*:\s*$",
               re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*(von|from):\s.*\n(?:.*\n){0,3}?\s*(gesendet|sent|an|to|betreff|subject):",
               re.IGNORECASE | re.MULTILINE),
)
_MAX_DEPTH = 12


@dataclass(frozen=True, slots=True)
class Attachment:
    """Recorded, never decoded. `size` is the size on the wire, before decoding."""

    filename: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class ParsedMail:
    message_id: str
    subject: str
    sender: str
    sender_name: str
    date: datetime | None
    text: str
    text_new: str
    attachments: tuple[Attachment, ...] = ()
    synthetic_id: bool = False
    html_only: bool = False


def parse_message(raw: bytes) -> ParsedMail:
    """Parse raw mail bytes. Never raises for malformed input; worst case is empty text."""
    msg = message_from_bytes(raw)
    plains: list[str] = []
    htmls: list[str] = []
    attachments: list[Attachment] = []
    _walk(msg, plains, htmls, attachments, 0)

    html_only = not any(p.strip() for p in plains) and bool(htmls)
    body = "\n\n".join(p for p in plains if p.strip())
    if html_only:
        body = "\n\n".join(html_to_text(h) for h in htmls)
    text = tidy(body)

    subject = header_text(msg.get("Subject"))
    sender_name, sender = parse_address(msg.get("From"))
    message_id, synthetic = message_id_of(msg, subject, sender, text)
    return ParsedMail(
        message_id=message_id,
        subject=subject,
        sender=sender,
        sender_name=sender_name,
        date=parse_date(msg.get("Date")),
        text=text,
        text_new=strip_quotes(text),
        attachments=tuple(attachments),
        synthetic_id=synthetic,
        html_only=html_only,
    )


def _walk(part: Message, plains: list[str], htmls: list[str],
          attachments: list[Attachment], depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    content_type = part.get_content_type()
    disposition = str(part.get("Content-Disposition") or "").lower()
    filename = header_text(part.get_filename())

    if part.get_content_maintype() == "multipart":
        payload = part.get_payload()
        for child in payload if isinstance(payload, list) else []:
            if isinstance(child, Message):
                _walk(child, plains, htmls, attachments, depth + 1)
        return

    if content_type == "message/rfc822":
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(inner, Message) and "attachment" not in disposition:
            _walk(inner, plains, htmls, attachments, depth + 1)
        else:
            attachments.append(_attachment(part, filename or "message.eml", content_type))
        return

    if "attachment" in disposition or (filename and part.get_content_maintype() != "text"):
        attachments.append(_attachment(part, filename or "unnamed", content_type))
        return

    if content_type == "text/plain":
        plains.append(decode_part(part))
    elif content_type == "text/html":
        htmls.append(decode_part(part))
    else:
        attachments.append(_attachment(part, filename or "unnamed", content_type))


def _attachment(part: Message, filename: str, content_type: str) -> Attachment:
    payload = part.get_payload(decode=False)
    size = len(payload) if isinstance(payload, str) else 0
    return Attachment(filename=filename, content_type=content_type, size=size)


def decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    return decode_bytes(payload, part.get_content_charset())


def decode_bytes(data: bytes, declared: str | None = None) -> str:
    """Decode with the declared charset, but do not trust it blindly."""
    charset = (declared or "").strip().strip('"').lower()
    order = [] if charset in _WEAK_CHARSETS else [charset]
    order.extend(_FALLBACKS)
    if _looks_like_utf8(data):
        # Mail labelled latin-1 that actually carries UTF-8 bytes is common.
        order.insert(0, "utf-8")
    for candidate in dict.fromkeys(order):
        try:
            return data.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("iso-8859-1", errors="replace")


def _looks_like_utf8(data: bytes) -> bool:
    if not any(byte > 0x7F for byte in data):
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def header_text(value: str | None) -> str:
    """Decode RFC 2047 words ("=?utf-8?q?Gr=C3=BC=C3=9Fe?=") into plain text."""
    if not value:
        return ""
    try:
        chunks = decode_header(value)
    except Exception:  # a header so broken that the stdlib gives up
        return re.sub(r"\s+", " ", str(value)).strip()
    out = []
    for chunk, charset in chunks:
        out.append(decode_bytes(chunk, charset) if isinstance(chunk, bytes) else chunk)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def parse_address(value: str | None) -> tuple[str, str]:
    name, address = utils.parseaddr(header_text(value))
    return name.strip(), address.strip().lower()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def message_id_of(msg: Message, subject: str, sender: str, text: str) -> tuple[str, bool]:
    """The de-duplication key. Falls back to a content digest if the header is missing."""
    raw = re.sub(r"\s+", "", str(msg.get("Message-ID") or ""))
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    if raw:
        return raw, False
    digest = hashlib.sha256()
    for piece in (str(msg.get("Date") or ""), sender, subject, text):
        digest.update(piece.encode("utf-8", "replace"))
        digest.update(b"\x00")
    return f"sha256:{digest.hexdigest()[:40]}", True


class _HtmlToText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._muted = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._muted += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
        elif tag in ("td", "th"):
            self.parts.append(" ")  # table cells would otherwise run into each other

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._muted = max(0, self._muted - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._muted:
            self.parts.append(re.sub(r"[ \t\r\n]+", " ", data))


def html_to_text(html: str) -> str:
    """Last resort for mail without a text/plain part. Block tags become newlines."""
    parser = _HtmlToText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # unbalanced or truncated markup: keep what we got
        pass
    return tidy("".join(parser.parts))


def strip_quotes(text: str) -> str:
    """Drop forwarded and quoted history, keeping only what this sender wrote."""
    cut = len(text)
    for marker in _CUT_MARKERS:
        found = marker.search(text)
        if found:
            cut = min(cut, found.start())
    head = text[:cut]
    return tidy("\n".join(line for line in head.splitlines() if not _QUOTED_LINE.match(line)))


def tidy(text: str) -> str:
    """Normalise line endings, invisible characters and runs of blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace(" ", " ").replace("­", "").replace("​", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
