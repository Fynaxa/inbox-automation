"""A small IMAP client: TLS, UID commands, IDLE with a polling fallback.

Everything addresses messages by UID rather than by sequence number. Sequence
numbers shift whenever anything else touches the mailbox, which is exactly the
bug that shows up in production and never in testing.
"""

from __future__ import annotations

import imaplib
import logging
import select
import socket
import ssl
import time

from .config import Credentials, Settings

log = logging.getLogger(__name__)


class MailboxError(RuntimeError):
    """The server said no and retrying will not help."""


class TransientError(MailboxError):
    """Connection trouble. The caller should reconnect and carry on."""


class Mailbox:
    """Thin wrapper around imaplib with the reconnect logic the stdlib leaves out."""

    def __init__(self, credentials: Credentials, settings: Settings) -> None:
        self.credentials = credentials
        self.settings = settings
        self.capabilities: frozenset[str] = frozenset()
        self._imap: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        self.close()
        context = ssl.create_default_context()
        try:
            imap = imaplib.IMAP4_SSL(
                self.credentials.host, self.credentials.port, ssl_context=context, timeout=30
            )
            imap.login(self.credentials.user, self.credentials.password)
            self._check(imap.select(_quote(self.credentials.folder)))
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"IMAP login or SELECT failed: {exc}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise TransientError(f"cannot reach {self.credentials.host}: {exc}") from exc
        self._imap = imap
        self.capabilities = frozenset(c.decode().upper() for c in imap.capabilities)
        log.info("connected to %s as %s, folder %s",
                 self.credentials.host, self.credentials.user, self.credentials.folder)

    def close(self) -> None:
        imap, self._imap = self._imap, None
        if imap is None:
            return
        try:
            imap.close()
            imap.logout()
        except (imaplib.IMAP4.error, OSError, ssl.SSLError):
            pass  # the connection is being dropped anyway

    def search(self, criteria: str) -> list[str]:
        imap = self._require()
        try:
            status, data = imap.uid("SEARCH", None, *criteria.split())
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as exc:
            raise TransientError(f"SEARCH failed: {exc}") from exc
        self._check((status, data))
        return [uid.decode() for uid in (data[0] or b"").split()]

    def fetch(self, uid: str) -> bytes | None:
        """BODY.PEEK[] instead of RFC822, so reading a mail does not flag it as seen."""
        imap = self._require()
        try:
            status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as exc:
            raise TransientError(f"FETCH {uid} failed: {exc}") from exc
        self._check((status, data))
        for item in data:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                return item[1]
        return None  # the mail vanished between SEARCH and FETCH

    def mark_seen(self, uid: str) -> None:
        imap = self._require()
        self._check(imap.uid("STORE", uid, "+FLAGS", r"(\Seen)"))

    def move(self, uid: str, folder: str) -> None:
        imap = self._require()
        target = _quote(folder)
        imap.create(target)  # a no-op if it exists; servers differ on the error
        if "MOVE" in self.capabilities:
            self._check(imap.uid("MOVE", uid, target))
            return
        self._check(imap.uid("COPY", uid, target))
        self._check(imap.uid("STORE", uid, "+FLAGS", r"(\Deleted)"))
        if "UIDPLUS" in self.capabilities:
            self._check(imap.uid("EXPUNGE", uid))
        else:
            self._check(imap.expunge())

    def wait(self, timeout: int) -> bool:
        """Block until the mailbox changes. True if it did, False on timeout."""
        if self.settings.use_idle and "IDLE" in self.capabilities:
            return self._idle(timeout)
        time.sleep(min(timeout, self.settings.poll_seconds))
        return True

    def _idle(self, timeout: int) -> bool:
        # imaplib grew an idle() helper in Python 3.13; this keeps 3.11 working.
        imap = self._require()
        tag = imap._new_tag()
        try:
            imap.send(tag + b" IDLE\r\n")
            if not imap.readline().startswith(b"+"):
                raise TransientError("server refused IDLE")
            try:
                return self._wait_for_data(imap, timeout)
            finally:
                imap.send(b"DONE\r\n")
                self._drain(imap, tag)
        except (imaplib.IMAP4.abort, OSError, ssl.SSLError) as exc:
            raise TransientError(f"IDLE broke: {exc}") from exc

    @staticmethod
    def _wait_for_data(imap: imaplib.IMAP4_SSL, timeout: int) -> bool:
        sock: socket.socket = imap.sock
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if isinstance(sock, ssl.SSLSocket) and sock.pending():
                return True  # already decrypted and buffered, select would not see it
            readable, _, _ = select.select([sock], [], [], min(remaining, 5.0))
            if readable:
                return True

    @staticmethod
    def _drain(imap: imaplib.IMAP4_SSL, tag: bytes) -> None:
        while True:
            line = imap.readline()
            if not line:
                raise TransientError("connection closed while leaving IDLE")
            if line.startswith(tag):
                return

    def _require(self) -> imaplib.IMAP4_SSL:
        if self._imap is None:
            raise TransientError("not connected")
        return self._imap

    @staticmethod
    def _check(result: tuple[str, object]) -> None:
        status, payload = result
        if status != "OK":
            raise MailboxError(f"server replied {status}: {payload!r}")

    def __enter__(self) -> Mailbox:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _quote(folder: str) -> str:
    """Folder names with spaces need quoting; embedded quotes need escaping."""
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def backoff(attempt: int, base: float = 5.0, cap: float = 300.0) -> float:
    """Exponential with a deterministic cap, so a dead server is not hammered."""
    return min(cap, base * (2 ** min(attempt, 8)))
