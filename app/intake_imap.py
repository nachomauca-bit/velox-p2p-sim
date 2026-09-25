"""Real intake (brief section 17): poll IMAP mailboxes and post every new message to POST /intake/webhook.

For every UNSEEN message of every configured mailbox (fetched with BODY.PEEK[], which does not mark it read):
- one POST per invoice attachment: a PDF (application/pdf or *.pdf) or a UBL e-invoice (XML that ubl.is_ubl
  accepts). The email text goes along as email_body (e.g. a store manager's forwarding comment). Attachments of
  a message forwarded as an attachment (message/rfc822) count too; anything else (images, other XML) is ignored,
  and so is an invoice file larger than the webhook's 10 MB limit (a note in email_body tells the reviewer).
  One unreadable part never fails the whole message: it is ignored and logged.
- no invoice attachment: one POST with the email text only (the plain-text part, else the HTML part as text),
  followed by the text of every message forwarded as an attachment under "---- Forwarded message ----" with its
  From, Date and Subject; the webhook registers it as "unknown" and routes it to human review.
The message is marked \\Seen when every POST returned 2xx. A transient failure (network, 5xx, 408, 425, 429, or
401 / 403 / 404 / 405 / 407, which mean the poller's setup is wrong) leaves it unseen, and the next pass retries it.
Any other 4xx (400 bad sender or file, 413 too large, 422 ...) is permanent: the message is logged REJECTED, marked
\\Seen and counted apart, without failing the pass. The webhook deduplicates on message_id + file name (or body),
so a retry never registers a document twice. A From header without a usable address is sent as
unknown-sender@invalid.example (the original header is quoted in email_body), a Message-ID without "@" (or one the
email package cannot parse) is replaced by a stable hash id, and a header that cannot be parsed never crashes a
message. Credentials are never logged. IMAP over TLS only (IMAP4_SSL, certificate verified); a non-ASCII password
is sent with AUTHENTICATE PLAIN (UTF-8) and a non-ASCII folder name in IMAP modified UTF-7. Each mailbox is polled
on its own: a mailbox that cannot be opened or read is logged and counted, and the other one is still polled.

Configuration (environment or .env):
  INTAKE_WEBHOOK_URL   default http://127.0.0.1:8010/intake/webhook
  APP_USERNAME / APP_PASSWORD   basic auth for the webhook (sent only when APP_PASSWORD is set)
  INTAKE_SCENARIO      asis | tobe (default tobe)
  IMAP_HOST, IMAP_PORT (993), IMAP_USER, IMAP_PASSWORD, IMAP_FOLDER (INBOX), IMAP_CHANNEL (ap_mailbox)
  IMAP_SINCE           optional YYYY-MM-DD: only messages received on or after that day (SEARCH UNSEEN SINCE ...)
  IMAP_ALLOWED_SENDERS optional, comma-separated addresses or @domains (billing@nordwind.de, @velox.com): a message
                       from anyone else is not fetched or posted and stays unseen (logged once per process)
  IMAP2_HOST, ... IMAP2_CHANNEL (store_mailbox), IMAP2_SINCE, IMAP2_ALLOWED_SENDERS   optional second mailbox

CLI:  python -m app.intake_imap --once       one pass over all mailboxes; exit code 0 (all ok) or 1
      python -m app.intake_imap --loop 60    a pass every 60 seconds until interrupted
"""
from __future__ import annotations

import argparse
import base64
import email
import hashlib
import imaplib
import os
import re
import ssl
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Optional

import httpx

from app import config, ubl  # noqa: F401  (config loads .env into the environment)

Log = Callable[[str], None]

DEFAULT_WEBHOOK_URL = "http://127.0.0.1:8010/intake/webhook"
CHANNELS = ("ap_mailbox", "store_mailbox")
SCENARIOS = ("asis", "tobe")
IMAP_TIMEOUT_S = 60
WEBHOOK_TIMEOUT_S = 180.0  # the webhook extracts (a model call) and runs the gate before it answers
MAX_BODY_CHARS = 20_000
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # the webhook's limit (app/main.py MAX_UPLOAD_BYTES): larger is a 413
EMPTY_BODY = "(the email has no text)"  # the webhook needs a body when there is no attachment
UNKNOWN_SENDER = "unknown-sender@invalid.example"  # From has no usable address: the email still reaches review
FORWARDED = "---- Forwarded message ----"
XML_TYPES = ("application/xml", "text/xml")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")  # the webhook's rule for the sender (app/main.py)
_ALLOWED_RE = re.compile(r"^[^@\s,]*@[^@\s,]+\.[^@\s,]+$")  # an address, or "@domain.tld"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")  # RFC 3501 dates

# Outcome of one message
OK, FAILED, REJECTED = "ok", "failed", "rejected"
TRANSIENT_4XX = frozenset({408, 425, 429})  # retry later
SETUP_4XX = frozenset({401, 403, 404, 405, 407})  # the poller's URL or password is wrong: every message would fail


class ConfigError(ValueError):
    """The environment does not describe a usable configuration."""


class WebhookError(RuntimeError):
    """The webhook answered with a non-2xx status."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status

    @property
    def permanent(self) -> bool:
        """A 4xx about this message (bad sender or file, too large, ...): sending it again cannot help. A 5xx, a
        transient 4xx and a setup 4xx (marking the message read would lose it) are not."""
        return 400 <= self.status < 500 and self.status not in TRANSIENT_4XX | SETUP_4XX


# --------------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MailboxConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    folder: str
    channel: str
    since: Optional[date] = None  # SEARCH UNSEEN SINCE <date>
    allowed_senders: tuple[str, ...] = ()  # lower-case addresses and "@domain"s; empty: every sender

    @property
    def label(self) -> str:
        return f"{self.user}/{self.folder}"


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    scenario: str
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    webhook: WebhookConfig
    mailboxes: tuple[MailboxConfig, ...]


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or "").strip() or default


def _since(env: Mapping[str, str], prefix: str) -> Optional[date]:
    text = _env(env, f"{prefix}_SINCE")
    if not text:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError:
        raise ConfigError(f"{prefix}_SINCE must be a date YYYY-MM-DD") from None


def _allowed_senders(env: Mapping[str, str], prefix: str) -> tuple[str, ...]:
    entries = [entry.strip().lower() for entry in _env(env, f"{prefix}_ALLOWED_SENDERS").split(",")]
    for entry in entries:
        if entry and not _ALLOWED_RE.match(entry):
            raise ConfigError(f"{prefix}_ALLOWED_SENDERS: {entry!r} is not an email address or an @domain")
    return tuple(dict.fromkeys(entry for entry in entries if entry))


def _mailbox(env: Mapping[str, str], prefix: str, default_channel: str) -> Optional[MailboxConfig]:
    """The mailbox described by <prefix>_* variables, or None when <prefix>_HOST is not set."""
    host = _env(env, f"{prefix}_HOST")
    if not host:
        return None
    user, password = _env(env, f"{prefix}_USER"), env.get(f"{prefix}_PASSWORD") or ""
    if not user or not password:
        raise ConfigError(f"{prefix}_HOST is set but {prefix}_USER or {prefix}_PASSWORD is missing")
    port = _env(env, f"{prefix}_PORT", "993")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"{prefix}_PORT must be a port number")
    channel = _env(env, f"{prefix}_CHANNEL", default_channel)
    if channel not in CHANNELS:
        raise ConfigError(f"{prefix}_CHANNEL must be ap_mailbox or store_mailbox")
    return MailboxConfig(host=host, port=int(port), user=user, password=password,
                         folder=_env(env, f"{prefix}_FOLDER", "INBOX"), channel=channel,
                         since=_since(env, prefix), allowed_senders=_allowed_senders(env, prefix))


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    """Read the webhook and up to two mailboxes from the environment. Raises ConfigError."""
    scenario = _env(env, "INTAKE_SCENARIO", "tobe")
    if scenario not in SCENARIOS:
        raise ConfigError("INTAKE_SCENARIO must be asis or tobe")
    webhook = WebhookConfig(url=_env(env, "INTAKE_WEBHOOK_URL", DEFAULT_WEBHOOK_URL), scenario=scenario,
                            username=_env(env, "APP_USERNAME", "velox"), password=_env(env, "APP_PASSWORD"))
    boxes = (_mailbox(env, "IMAP", "ap_mailbox"), _mailbox(env, "IMAP2", "store_mailbox"))
    return Settings(webhook=webhook, mailboxes=tuple(box for box in boxes if box is not None))


# --------------------------------------------------------------------------------------------
# Reading an email
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attachment:
    filename: str
    content_type: str  # application/pdf | application/xml
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    sender: str
    subject: str
    body: str
    attachments: tuple[Attachment, ...]
    ignored: tuple[str, ...]  # parts that are not posted: images, other XML, too large ... (with the reason)


class _HTMLText(HTMLParser):
    """Visible text of an HTML body: scripts and styles dropped, block elements on their own lines."""

    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"br", "p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in self.SKIP:
            self._skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def _tidy(text: str) -> str:
    """Trim every line, collapse runs of spaces and of blank lines, cap the length."""
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    tidy = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return tidy if len(tidy) <= MAX_BODY_CHARS else tidy[:MAX_BODY_CHARS] + "\n[... truncated]"


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _clean(text: str) -> str:
    """Text that encodes as UTF-8: raw 8-bit header bytes (surrogate escapes) decoded as UTF-8 where possible."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # a lone surrogate that is not an escaped byte
        return text.encode("utf-8", "replace").decode("utf-8")


def _raw_header(msg: EmailMessage, name: str) -> str:
    """The first such header exactly as received, on one line ("" when missing)."""
    for key, value in msg.raw_items():
        if key.lower() == name.lower():
            return _clean(_one_line(value))
    return ""


def _raw_decoded(msg: EmailMessage, name: str) -> str:
    """The raw header with its RFC 2047 words decoded where possible (as received, not normalised)."""
    raw = _raw_header(msg, name)
    with suppress(Exception):
        return _clean(_one_line(str(make_header(decode_header(raw)))))
    return raw


def _header(msg: EmailMessage, name: str) -> str:
    """The decoded header on one line ("" when missing). A header the email package cannot parse (policy.default
    raises IndexError on "Message-ID: <>") falls back to its raw text."""
    try:
        return _clean(_one_line(msg.get(name)))
    except Exception:  # noqa: BLE001  a malformed header must not fail the message
        return _raw_decoded(msg, name)


def _content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001  unknown charset or a broken encoding: best effort
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def _raw_body(msg: EmailMessage) -> str:
    """The email text, not tidied: the plain-text body, else the HTML body as text ("" if there is none)."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return _content(part)
        part = msg.get_body(preferencelist=("html",))
        return html_to_text(_content(part)) if part is not None else ""
    except Exception:  # noqa: BLE001  a malformed MIME structure: no text rather than a failed message
        return ""


def body_text(msg: EmailMessage) -> str:
    """The email text: the plain-text body, else the HTML body as text ("" if there is none)."""
    return _tidy(_raw_body(msg))


def _forwarded(msg: EmailMessage) -> list[EmailMessage]:
    """Messages attached as message/rfc822 (a forward as attachment), in order, nested forwards included."""
    found: list[EmailMessage] = []
    for part in msg.walk():
        if part is msg or part.get_content_type() != "message/rfc822":
            continue
        payload = part.get_payload()
        if isinstance(payload, list) and payload and isinstance(payload[0], EmailMessage):
            found.append(payload[0])
    return found


def _forwarded_text(inner: EmailMessage) -> str:
    """A forwarded message as the reviewer would read it: a separator, From / Date / Subject, then its text."""
    lines = [FORWARDED] + [f"{name}: {value}" for name in ("From", "Date", "Subject")
                           if (value := _header(inner, name))]
    return "\n".join(lines) + "\n\n" + _raw_body(inner)


def _invoice_kind(content_type: str, filename: str) -> Optional[str]:
    name = filename.lower()
    if content_type == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    if content_type in XML_TYPES or name.endswith(".xml"):
        return "xml"
    return None


def _file_name(filename: str, kind: str, index: int, taken: set[str]) -> str:
    """Base name with the right suffix, unique within the message (the webhook dedups on it)."""
    suffix = f".{kind}"
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip() or f"attachment-{index}{suffix}"
    if not name.lower().endswith(suffix):
        name += suffix
    stem, n, unique = name[: -len(suffix)], 2, name
    while unique.lower() in taken:
        unique, n = f"{stem}-{n}{suffix}", n + 1
    taken.add(unique.lower())
    return unique


def _attachments(msg: EmailMessage) -> tuple[list[Attachment], list[str], list[str]]:
    """Invoice files anywhere in the message (forwarded messages included), the parts that are not posted (with
    the reason when they looked like an invoice), and notes for the reviewer (an invoice file too large to post).
    A part that cannot be read is ignored: one bad part never fails the whole message."""
    found: list[Attachment] = []
    ignored: list[str] = []
    notes: list[str] = []
    taken: set[str] = set()
    for index, part in enumerate(msg.walk(), start=1):
        if part.is_multipart():
            continue
        label = f"part {index}"
        try:
            content_type, filename = part.get_content_type(), _clean(_one_line(part.get_filename() or ""))
            label = filename or content_type
            kind = _invoice_kind(content_type, filename)
            if kind is None:
                if filename or part.get_content_disposition() == "attachment":
                    ignored.append(label)
                continue
            data = part.get_payload(decode=True) or b""
            if len(data) > MAX_ATTACHMENT_BYTES:
                ignored.append(f"{label} (larger than 10 MB)")
                notes.append(f"{label} was not posted: larger than 10 MB, the intake limit")
                continue
            if kind == "pdf" and not data.startswith(b"%PDF-"):
                ignored.append(f"{label} (not a PDF)")  # a mislabelled file
                continue
            if kind == "xml" and not ubl.is_ubl(data):
                ignored.append(f"{label} (not a UBL e-invoice)")
                continue
        except Exception as exc:  # noqa: BLE001  e.g. a header the email package cannot parse
            ignored.append(f"{label} (unreadable: {type(exc).__name__})")
            continue
        found.append(Attachment(_file_name(filename, kind, index, taken),
                                "application/pdf" if kind == "pdf" else "application/xml", data))
    return found, ignored, notes


def _address(msg: EmailMessage) -> str:
    try:
        header = msg.get("From")
        addresses = getattr(header, "addresses", ())
        if addresses:
            return addresses[0].addr_spec
        return parseaddr(str(header or ""))[1]
    except Exception:  # noqa: BLE001  a From header policy.default cannot parse
        return parseaddr(_raw_header(msg, "From"))[1]


def _sender(msg: EmailMessage) -> Optional[str]:
    """The From address when the webhook accepts it (name@domain.tld), else None."""
    address = _clean(_address(msg)).strip()
    return address if _EMAIL_RE.match(address) else None


def _message_id(msg: EmailMessage, raw: bytes) -> str:
    """The raw Message-ID text as the dedup key (parsing would cut "<a b@c d>" to "<a" and raises on "<>"); a
    stable id from the message bytes when it is missing or has no "@"."""
    message_id = _raw_header(msg, "Message-ID")
    return message_id if "@" in message_id else f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@intake>"


def parse_message(raw: bytes) -> ParsedEmail:
    """Parse one RFC 822 message. A message without a usable Message-ID gets a stable one from its bytes."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    attachments, ignored, notes = _attachments(msg)
    text = _raw_body(msg)
    if not attachments:  # the invoice may be the text of a message forwarded as an attachment
        for inner in _forwarded(msg):
            text += "\n\n" + _forwarded_text(inner)
    sender = _sender(msg)
    if sender is None:
        notes.append(f"the From header has no usable email address: {_raw_decoded(msg, 'From') or '(none)'}")
    body = _tidy(text)
    if notes:  # first, so the reviewer sees them and a long text cannot truncate them
        body = "\n".join(f"[intake note] {note}" for note in notes) + (f"\n\n{body}" if body else "")
    return ParsedEmail(message_id=_message_id(msg, raw), sender=sender or UNKNOWN_SENDER,
                       subject=_header(msg, "Subject"), body=body, attachments=tuple(attachments),
                       ignored=tuple(ignored))


# --------------------------------------------------------------------------------------------
# Posting to the webhook
# --------------------------------------------------------------------------------------------


def form_parts(mail: ParsedEmail, channel: str, scenario: str,
               attachment: Optional[Attachment]) -> list[tuple[str, Any]]:
    """Multipart fields of one POST (text fields as (None, value) so the request is always multipart)."""
    body = mail.body or (EMPTY_BODY if attachment is None else "")
    fields = {"channel": channel, "sender": mail.sender, "subject": mail.subject, "scenario": scenario,
              "message_id": mail.message_id, "email_body": body}
    parts: list[tuple[str, Any]] = [(name, (None, value)) for name, value in fields.items() if value]
    if attachment is not None:
        parts.append(("file", (attachment.filename, attachment.data, attachment.content_type)))
    return parts


def requests_for(mail: ParsedEmail, channel: str, scenario: str) -> list[list[tuple[str, Any]]]:
    """One POST per invoice attachment, or a single body-only POST when there is none."""
    if not mail.attachments:
        return [form_parts(mail, channel, scenario, None)]
    return [form_parts(mail, channel, scenario, attachment) for attachment in mail.attachments]


def _error_detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return _one_line(detail or response.text)[:200]


def post_document(client: httpx.Client, webhook: WebhookConfig, parts: list[tuple[str, Any]]) -> tuple[int, dict]:
    """POST one document; returns (status, JSON reply). Raises httpx.HTTPError or WebhookError (non-2xx)."""
    auth = httpx.BasicAuth(webhook.username, webhook.password) if webhook.password else None
    response = client.post(webhook.url, files=parts, auth=auth)
    if not response.is_success:
        raise WebhookError(f"HTTP {response.status_code}: {_error_detail(response)}", response.status_code)
    try:
        reply = response.json()
    except ValueError:
        reply = {}
    return response.status_code, reply if isinstance(reply, dict) else {}


def make_client() -> httpx.Client:
    return httpx.Client(timeout=WEBHOOK_TIMEOUT_S)


# --------------------------------------------------------------------------------------------
# IMAP
# --------------------------------------------------------------------------------------------


def _ok(response: tuple[str, list[Any]], what: str) -> list[Any]:
    status, data = response
    if status != "OK":
        raise imaplib.IMAP4.error(f"{what} failed: {_one_line(data)[:200]}")
    return data


def _quoted(folder: str) -> str:
    if folder.startswith('"') or not re.search(r'[\s"()\\{%*]', folder):
        return folder
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


_UTF7_RUN = re.compile(r"&([A-Za-z0-9+,]*)-")


def _is_imap_utf7(name: str) -> bool:
    """Printable ASCII in which every "&" starts a valid encoded run ("&-", "&APw-"): already modified UTF-7."""
    if not all(" " <= char <= "~" for char in name) or "&" in _UTF7_RUN.sub("", name):
        return False
    for run in _UTF7_RUN.findall(name):
        if not run:
            continue  # "&-" is "&"
        try:
            decoded = base64.b64decode(run.replace(",", "/") + "=" * (-len(run) % 4), validate=True)
            text = decoded.decode("utf-16-be")
        except ValueError:  # binascii.Error, UnicodeDecodeError: "Q&A-2026" is a plain name
            return False
        if any(ord(char) < 0x80 for char in text):  # an encoder never encodes ASCII
            return False
    return True


def imap_utf7(name: str) -> str:
    """A folder name in IMAP modified UTF-7 (RFC 3501 section 5.1.3), which SELECT needs since imaplib sends ASCII
    only: "Entwürfe" -> "Entw&APw-rfe", "R&D" -> "R&-D". A name already in that form (e.g. copied from a LIST
    response) is kept as it is; plain ASCII without "&" is the same in both forms."""
    if _is_imap_utf7(name):
        return name
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            encoded = base64.b64encode("".join(pending).encode("utf-16-be")).decode("ascii")
            out.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if " " <= char <= "~":
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def _login(conn: imaplib.IMAP4, user: str, password: str) -> None:
    """LOGIN for ASCII credentials. imaplib sends LOGIN arguments as ASCII, so a password such as "contraseña" goes
    with AUTHENTICATE PLAIN (RFC 4616), which carries UTF-8."""
    if user.isascii() and password.isascii():
        conn.login(user, password)
        return
    credentials = b"\0" + user.encode("utf-8") + b"\0" + password.encode("utf-8")
    conn.authenticate("PLAIN", lambda _challenge: credentials)


def connect(mailbox: MailboxConfig) -> imaplib.IMAP4:
    """Open, log in and select the folder (read-write, to set \\Seen). The certificate is verified."""
    conn = imaplib.IMAP4_SSL(mailbox.host, mailbox.port, ssl_context=ssl.create_default_context(),
                             timeout=IMAP_TIMEOUT_S)
    try:
        _login(conn, mailbox.user, mailbox.password)
        _ok(conn.select(_quoted(imap_utf7(mailbox.folder))), f"select {mailbox.folder}")
    except BaseException:
        with suppress(Exception):
            conn.logout()
        raise
    return conn


def imap_date(day: date) -> str:
    """An RFC 3501 date for SEARCH ("25-Sep-2026"): English month names whatever the locale."""
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}"


def unseen_uids(conn: imaplib.IMAP4, since: Optional[date] = None) -> list[str]:
    criteria = ("UNSEEN",) if since is None else ("UNSEEN", "SINCE", imap_date(since))
    data = _ok(conn.uid("SEARCH", None, *criteria), "search")
    return [uid.decode() for uid in (data[0] or b"").split()] if data else []


def _fetched(conn: imaplib.IMAP4, uid: str, what: str, label: str) -> bytes:
    for item in _ok(conn.uid("FETCH", uid, what), f"fetch {label}uid {uid}"):
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    raise imaplib.IMAP4.error(f"fetch {label}uid {uid} returned no message")


def fetch_message(conn: imaplib.IMAP4, uid: str) -> bytes:
    return _fetched(conn, uid, "(BODY.PEEK[])", "")


def fetch_sender(conn: imaplib.IMAP4, uid: str) -> Optional[str]:
    """The From address from the header alone (PEEK: the message stays unseen), for the sender allowlist."""
    header = _fetched(conn, uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])", "the From header of ")
    return _sender(email.message_from_bytes(header, policy=policy.default))


def sender_allowed(sender: Optional[str], allowed: tuple[str, ...]) -> bool:
    """No allowlist: every sender. Else the address itself or its exact domain ("@velox.com") must be listed."""
    if not allowed:
        return True
    if not sender:
        return False
    address = sender.lower()
    return address in allowed or "@" + address.rsplit("@", 1)[1] in allowed


def mark_seen(conn: imaplib.IMAP4, uid: str) -> None:
    _ok(conn.uid("STORE", uid, "+FLAGS", "(\\Seen)"), f"store \\Seen on uid {uid}")


# --------------------------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------------------------


@dataclass
class PassResult:
    messages: int = 0  # new (unseen) messages found
    failed: int = 0  # left unseen because a POST (or the \Seen flag) failed: the next pass retries them
    errors: int = 0  # mailboxes that could not be opened or read to the end
    rejected: int = 0  # the webhook refused a document for good (4xx): logged REJECTED and marked \Seen
    skipped: int = 0  # from a sender not in the allowlist: left unseen, never fetched in full

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors

    def add(self, other: PassResult) -> None:
        for name in ("messages", "failed", "errors", "rejected", "skipped"):
            setattr(self, name, getattr(self, name) + getattr(other, name))


# (host, port, user, folder) -> UIDs whose "not an allowed sender" line was logged: each is logged once per process
_SKIPS_LOGGED: dict[tuple[str, int, str, str], set[str]] = {}


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _describe(exc: BaseException, *secrets: str) -> str:
    return _redact(_one_line(f"{type(exc).__name__}: {exc}")[:300], *secrets)


def _reply_summary(status: int, reply: dict) -> tuple[str, str]:
    outcome = str(reply.get("outcome") or "-")
    if reply.get("exception_type"):
        outcome += f"/{reply['exception_type']}"
    if status == 200:
        outcome += " (already registered)"
    return str(reply.get("doc_id") or "?"), outcome


def process_message(conn: imaplib.IMAP4, uid: str, mailbox: MailboxConfig, webhook: WebhookConfig,
                    client: httpx.Client, log: Log) -> str:
    """Post one message's documents; returns OK, FAILED or REJECTED.

    Every POST succeeded: \\Seen, OK. A transient failure (network, 5xx, 408 / 425 / 429, or a setup 4xx such as
    401): left unseen for the next pass, FAILED. Otherwise, if the webhook refused a document for good (another
    4xx): logged, \\Seen and REJECTED, since the same POST would be refused on every pass.
    """
    mail = parse_message(fetch_message(conn, uid))
    doc_ids, outcomes, errors, rejections = [], [], [], []
    for parts in requests_for(mail, mailbox.channel, webhook.scenario):
        try:
            doc_id, outcome = _reply_summary(*post_document(client, webhook, parts))
        except (httpx.HTTPError, WebhookError) as exc:  # the other documents are still posted
            permanent = isinstance(exc, WebhookError) and exc.permanent
            (rejections if permanent else errors).append(_describe(exc, webhook.password, mailbox.password))
            continue
        doc_ids.append(doc_id)
        outcomes.append(outcome)
    head = f"[intake] mailbox={mailbox.label} uid={uid} from={mail.sender} attachments={len(mail.attachments)}"
    head += f" ignored={len(mail.ignored)} [{'; '.join(mail.ignored)}]" if mail.ignored else ""
    result = f" -> doc_id={','.join(doc_ids) or '-'} outcome={','.join(outcomes) or '-'}"
    if errors:
        log(f"{head}{result} FAILED ({'; '.join(errors + rejections)}); left unseen")
        return FAILED
    try:
        mark_seen(conn, uid)
    except imaplib.IMAP4.abort:
        raise  # a dead connection ends this mailbox's pass
    except imaplib.IMAP4.error as exc:  # registered; the retry is deduplicated by the webhook
        log(f"{head}{result} FAILED to mark as seen ({_describe(exc, mailbox.password)})")
        return FAILED
    if rejections:
        log(f"{head}{result} REJECTED ({'; '.join(rejections)}); marked as seen, not retried")
        return REJECTED
    log(head + result)
    return OK


def poll_mailbox(mailbox: MailboxConfig, webhook: WebhookConfig, client: httpx.Client, *,
                 log: Log = print) -> PassResult:
    """One pass over one mailbox. Never raises for IMAP, network or webhook errors: they are logged (credentials
    redacted) and counted, so the next mailbox is still polled."""
    result = PassResult()
    try:
        conn = connect(mailbox)
    except Exception as exc:  # noqa: BLE001  DNS, TLS, bad credentials, missing folder ...
        log(f"[intake] mailbox={mailbox.label} ERROR cannot open the mailbox: {_describe(exc, mailbox.password)}")
        result.errors += 1
        return result
    key = (mailbox.host, mailbox.port, mailbox.user, mailbox.folder)
    logged, skipped, finished = _SKIPS_LOGGED.get(key, set()), set(), False
    try:
        for uid in unseen_uids(conn, mailbox.since):
            result.messages += 1
            try:
                if mailbox.allowed_senders:
                    sender = fetch_sender(conn, uid)
                    if not sender_allowed(sender, mailbox.allowed_senders):
                        result.skipped += 1
                        skipped.add(uid)
                        if uid not in logged:
                            log(f"[intake] mailbox={mailbox.label} uid={uid} from={sender or '?'} SKIPPED: not an "
                                "allowed sender; left unseen (logged once)")
                        continue
                outcome = process_message(conn, uid, mailbox, webhook, client, log)
            except (OSError, imaplib.IMAP4.abort):
                raise  # a dead connection ends this mailbox's pass (imaplib reports it as IMAP4.abort)
            except Exception as exc:  # noqa: BLE001  one bad message must not stop the others
                log(f"[intake] mailbox={mailbox.label} uid={uid} ERROR {_describe(exc, mailbox.password)}; "
                    "left unseen")
                outcome = FAILED
            result.failed += outcome == FAILED
            result.rejected += outcome == REJECTED
        finished = True
    except Exception as exc:  # noqa: BLE001  the search failed or the connection dropped
        log(f"[intake] mailbox={mailbox.label} ERROR {_describe(exc, mailbox.password)}; pass of this mailbox ended")
        result.errors += 1
    finally:
        _SKIPS_LOGGED[key] = skipped if finished else logged | skipped  # forget UIDs that are no longer unseen
        with suppress(Exception):
            conn.logout()
    return result


def run_once(settings: Settings, *, client: Optional[httpx.Client] = None, log: Log = print) -> PassResult:
    """One pass over every configured mailbox."""
    total = PassResult()
    own_client = client is None
    client = client or make_client()
    try:
        for mailbox in settings.mailboxes:
            total.add(poll_mailbox(mailbox, settings.webhook, client, log=log))
    finally:
        if own_client:
            client.close()
    log(f"[intake] pass done: {total.messages} new message(s), {total.failed} left unseen, "
        f"{total.errors} mailbox error(s), {total.rejected} rejected, {total.skipped} skipped (sender not allowed)")
    return total


def run_loop(settings: Settings, interval_s: int, *, passes: Optional[int] = None,
             sleep: Callable[[float], None] = time.sleep, client: Optional[httpx.Client] = None,
             log: Log = print) -> int:
    """A pass every interval_s seconds (forever, or `passes` times); Ctrl+C stops it cleanly."""
    done = 0
    try:
        while passes is None or done < passes:
            try:
                run_once(settings, client=client, log=log)
            except Exception as exc:  # noqa: BLE001  keep polling; the next pass retries
                log(f"[intake] ERROR pass failed: {_describe(exc, settings.webhook.password)}")
            done += 1
            if passes is None or done < passes:
                sleep(interval_s)
    except KeyboardInterrupt:
        log("[intake] stopped")
    return 0


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------


def _seconds(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {text!r}")
    return int(text)


def _print(line: str) -> None:
    print(line, flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Poll the intake mailboxes (IMAP) and post new messages to the "
                                                 "intake webhook.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one pass over all mailboxes (exit code 0 or 1)")
    mode.add_argument("--loop", type=_seconds, metavar="SECONDS", help="a pass every SECONDS until interrupted")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        _print(f"[intake] configuration error: {exc}")
        return 1
    if not settings.mailboxes:
        _print("[intake] no mailbox configured: set IMAP_HOST, IMAP_USER and IMAP_PASSWORD (optionally IMAP2_*)")
        return 1
    if args.once:
        return 0 if run_once(settings, log=_print).ok else 1
    return run_loop(settings, args.loop, log=_print)


if __name__ == "__main__":
    sys.exit(main())
