# inbox-automation

Watches an IMAP mailbox, classifies every incoming mail against a rule file, and pulls
structured fields out of the text with named regex groups. Matches trigger pluggable
actions: write a JSON line, POST to a webhook, move the mail, flag it as read. A mail is
identified by its Message-ID and recorded in a small SQLite ledger, so a restart, a crash
or a duplicate delivery never processes the same mail twice.

Python 3.11 or newer. No runtime dependencies, standard library only. `pytest` is the one
development dependency.

## Architecture

```
  mail server                  inbox-automation                      your systems
  -----------                  ----------------                      ------------

  IMAP over TLS  -->  +-----------------------+
  IDLE or polling     |  imap_client          |  UID SEARCH, BODY.PEEK[], UID MOVE
                      |  reconnect + backoff  |  never touches sequence numbers
                      +-----------+-----------+
                                  |  raw bytes
                                  v
                      +-----------------------+
                      |  parsing              |  MIME walk, charset repair,
                      |                       |  HTML to text, quote stripping
                      +-----------+-----------+
                                  |  ParsedMail
                                  v
  rules.toml   ---->  +-----------------------+
                      |  rules                |  first match by priority wins,
                      |                       |  named groups, value converters
                      +-----------+-----------+
                                  |  Match(category, fields)
                                  v
                      +-----------------------+      +----------------------+
                      |  pipeline             |<---->|  ledger (SQLite)     |
                      |  claim, act, record   |      |  Message-ID + status |
                      +-----------+-----------+      +----------------------+
                                  |
          +-----------------+-----+------------+-----------------+
          v                 v                  v                 v
     write_json          webhook             move            mark_seen   -->  JSON lines,
                                                                              HTTP, folders
```

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# See it work with no mail server involved:
.venv/bin/inbox-automation -c rules.example.toml parse tests/fixtures/order_multipart.eml
```

Abbreviated output:

```json
{
  "category": "order.confirmation",
  "fields": {
    "amount": "1234.56",
    "currency": "EUR",
    "order_date": "2026-09-19",
    "order_id": "A-10042"
  },
  "message_id": "order-10042@shop.example.com",
  "rule": "order-confirmation",
  "would_run": "write_json, mark_seen, move"
}
```

Then point it at a real mailbox:

```bash
cp .env.example .env          # fill in host, user, password, folder
cp rules.example.toml rules.toml
set -a && source .env && set +a

inbox-automation -c rules.toml check          # validate rules, list them in order
inbox-automation -c rules.toml once --dry-run # classify one pass, change nothing
inbox-automation -c rules.toml once           # process what is there, then exit
inbox-automation -c rules.toml run            # watch until stopped
```

| Command | What it does |
| --- | --- |
| `run` | Watch the mailbox. IDLE when the server offers it, polling otherwise. Reconnects with backoff. |
| `once` | One pass over the search result, then exit. Good for cron. |
| `parse FILE.eml` | Classify local `.eml` files offline. No server, no state, no side effects. |
| `check` | Validate the rule file, print the evaluation order and the available actions. |
| `stats` | Ledger counts by status. |
| `reset MESSAGE_ID` | Forget one Message-ID so the next pass picks it up again. |

## Example configuration

Rules are TOML. Patterns are literal strings (single quotes) so that backslashes stay
backslashes. They are case-insensitive and multiline. Rules are evaluated by priority,
highest first, and the first match wins.

```toml
[settings]
state_db = "state/processed.sqlite3"
search = "UNSEEN"          # any IMAP search key, e.g. "SINCE 01-Oct-2026"
use_idle = true
idle_timeout_seconds = 840 # below the 29 minute limit in RFC 2177
max_attempts = 3           # give up on a mail that keeps failing
log_message_ids = false    # logs carry a digest instead of the raw ID

[[rules]]
name = "order-confirmation"
category = "order.confirmation"
priority = 100
scope = "full"                          # "full" reads quoted history, "new" does not
from = '@shop\.example\.com'
subject = '(bestellbest|order confirmation)'
extract = [
    'Bestellnummer:\s*(?P<order_id>[A-Z0-9][A-Z0-9-]{3,})',
    '(?:Gesamtbetrag|Summe|Total):\s*(?P<amount>[\d.,]+)\s*(?P<currency>EUR|USD)',
    '(?:Bestelldatum|Datum):\s*(?P<order_date>\d{1,2}\.\d{1,2}\.\d{4})',
]
require = ["order_id"]                  # without it the mail is left for a human

[rules.fields]                          # value converters
amount = "decimal_de"                   # "1.234,56 EUR" -> "1234.56"
order_date = "date_de"                  # "19.09.2026"   -> "2026-09-19"

[[rules.actions]]
type = "write_json"
path = "out/orders.jsonl"

[[rules.actions]]
type = "mark_seen"

[[rules.actions]]
type = "move"
folder = "Processed/Orders"
```

Conditions (`from`, `subject`, `body`) are combined with AND. Extraction runs over the
subject line plus the body, because order and invoice numbers live in either one. Amounts
stay strings so that no float rounding creeps into a number that represents money.

A webhook instead of a file:

```toml
[[rules.actions]]
type = "webhook"
url = "https://erp.example.com/hooks/orders"
attempts = 3
timeout_seconds = 10
headers = { Authorization = "Bearer ${set_this_from_your_own_proxy}" }
```

### Custom actions

Actions are a registry. Six lines are enough:

```python
from pathlib import Path
from inbox_automation.actions import ActionContext, register

@register("append_csv")
def _append_csv(spec):
    path = Path(spec["path"])
    def action(ctx: ActionContext) -> None:
        path.open("a", encoding="utf-8").write(
            ";".join(str(v) for v in ctx.record["fields"].values()) + "\n")
    return action
```

Import that module before the pipeline is built and `type = "append_csv"` works in the
rule file. An action that raises `ActionError` marks the mail as failed rather than done,
so it is retried on the next pass.

## Edge cases handled

Every item below has a test and an `.eml` fixture in `tests/fixtures/`. These are the
things that turn a two-hour script into a tool that survives a real mailbox.

- **Same mail twice.** Messages are claimed by Message-ID in a SQLite ledger before the
  actions run and marked done afterwards. A restart, a second delivery or an overlapping
  cron run skips them.
- **Missing Message-ID.** Some senders omit the header. A SHA-256 digest over date,
  sender, subject and body text stands in, and it is stable across runs.
- **Crash halfway through.** A claim that was never completed is retried, at most
  `max_attempts` times, then left alone so one poisonous mail cannot spin forever.
  `reset` releases it once the cause is fixed.
- **MIME multipart.** `multipart/alternative` prefers the plain text part,
  `multipart/mixed` reads the body and skips the rest.
- **Wrong or missing charset.** Labels like `unknown-8bit` or `us-ascii` on bytes that are
  anything but are ignored in favour of a decoding ladder (UTF-8, cp1252, latin-1). Mail
  labelled latin-1 that actually carries UTF-8 is detected and decoded correctly.
- **Umlauts in headers and body.** RFC 2047 encoded words are decoded, including words
  that declare a charset that does not exist.
- **Quoted-printable and base64.** Both transfer encodings are decoded before matching, so
  `Gr=C3=BC=C3=9Fe` never reaches a regex.
- **HTML mail without a text part.** Converted with the standard library parser. `script`,
  `style` and `title` content is dropped, block tags become line breaks, list items get a
  dash, entities are resolved.
- **Forwards and nested quotes.** Every mail carries two views: the full text, and the
  text with quoted history and forward headers removed (German and English markers,
  including the Outlook `Von:/Gesendet:` block). Rules choose per rule with `scope`, so a
  forwarded order confirmation still yields its order number while a support rule does not
  fire on last month's thread.
- **Attachments.** Recorded by name, type and size on the wire, never decoded, never part
  of the text a regex sees.
- **CRLF, non-breaking spaces, soft hyphens, zero-width spaces.** Normalised before
  matching, because they break regexes in ways that are hard to see in a diff.
- **German number and date formats.** `1.234,56 EUR` becomes `1234.56` and `19. September
  2026` becomes `2026-09-19`. Input that is not a number is refused rather than silently
  mangled.
- **Rule collisions.** Priority decides, then declaration order, and the first match wins.
  A dunning notice quoting an invoice number is filed as dunning, not as an invoice.
- **Reading a mail must not flag it.** Fetches use `BODY.PEEK[]`, so `\Seen` is only ever
  set by an explicit `mark_seen` action.
- **Mailbox changes under you.** Every command is a UID command. Sequence numbers shift
  whenever anything else touches the mailbox, which is the bug that only ever appears in
  production.
- **Connection drops.** Reconnect with exponential backoff capped at five minutes. A
  rejected login stops the process instead of retrying forever.
- **No IDLE on the server.** Capabilities are checked and polling takes over.
- **Malformed mail.** Parsing never raises. Worst case is an empty body, which simply
  matches no rule.

## Data protection by design

Mail content is personal data more often than not. The ledger therefore stores the
Message-ID, a status, the matched rule and a timestamp, and nothing else: no subject, no
sender, no body. What leaves the process is the extracted record, the fields your rules
asked for, plus sender and timestamps, and it goes only where the configured action sends
it. Logs are written the same way: they name rules, categories and field names, never
values, and the Message-ID appears as a truncated digest unless `log_message_ids` is
turned on. Credentials are read from the environment only, so the rule file stays
reviewable and committable. That keeps deletion requests cheap, since the only place mail
content ever lands is the output you chose.

## Tests

```bash
.venv/bin/python -m pytest
```

65 tests, no network access, no mail server. Mails are `.eml` fixtures in
`tests/fixtures/`, written for this repository, containing no real personal data. The
suite covers multipart parsing, broken charset headers, umlauts in subject and body,
regex extraction and converters, idempotency across simulated restarts, the retry limit,
rule priority, action behaviour including dry runs, config validation and the CLI.

## Known limitations

Password authentication only, no OAuth2 XOAUTH2 flow yet. One mailbox per process, which
is usually what you want for a supervisor like systemd or Docker. Attachments are
recognised but not stored. Actions run in order and are not transactional, so put the
irreversible ones (`move`) last.

## License

MIT
