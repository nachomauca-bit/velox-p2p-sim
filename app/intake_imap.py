"""Real intake (brief section 17): poll IMAP mailboxes and post every new message to POST /intake/webhook.

For every UNSEEN message of every configured mailbox (fetched with BODY.PEEK[], which does not mark it read):
- one POST per invoice attachment: a PDF (application/pdf or *.pdf) or a UBL e-invoice (XML that ubl.is_ubl
  accepts). The email text goes along as email_body (e.g. a store manager's forwarding comment). Attachments of
  a message forwarded as an attachment (message/rfc822) count too; anything else (images, other XML) is ignored.
- no invoice attachment: one POST with the email text only (the plain-text part, else the HTML part as text);
  the webhook registers it as "unknown" and routes it to human review.
The message is marked \\Seen only when every POST returned 2xx; otherwise it stays unseen and the next pass
retries it. The webhook deduplicates on message_id + file name (or body), so a retry never registers a
document twice. Credentials are never logged. IMAP over TLS only (IMAP4_SSL, certificate verified).

Configuration (environment or .env):
  INTAKE_WEBHOOK_URL   default http://127.0.0.1:8010/intake/webhook
  APP_USERNAME / APP_PASSWORD   basic auth for the webhook (sent only when APP_PASSWORD is set)
  INTAKE_SCENARIO      asis | tobe (default tobe)
  IMAP_HOST, IMAP_PORT (993), IMAP_USER, IMAP_PASSWORD, IMAP_FOLDER (INBOX), IMAP_CHANNEL (ap_mailbox)
  IMAP2_HOST, ... IMAP2_CHANNEL (store_mailbox)   optional second mailbox

CLI:  python -m app.intake_imap --once       one pass over all mailboxes; exit code 0 (all ok) or 1
      python -m app.intake_imap --loop 60    a pass every 60 seconds until interrupted
"""
from __future__ import annotations

import argparse
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
from email import policy
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
EMPTY_BODY = "(the email has no text)"  # the webhook needs a body when there is no attachment
XML_TYPES = ("application/xml", "text/xml")


class ConfigError(ValueError):
    """The environment does not describe a usable configuration."""


class WebhookError(RuntimeError):
    """The webhook answered with a non-2xx status."""


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
                         folder=_env(env, f"{prefix}_FOLDER", "INBOX"), channel=channel)


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
    ignored: tuple[str, ...]  # attachments that are not invoices (images, other XML, ...)


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


def _content(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, ValueError):  # unknown charset or a broken encoding: best effort
        return (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")


def body_text(msg: EmailMessage) -> str:
    """The email text: the plain-text body, else the HTML body as text ("" if there is none)."""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return _tidy(_content(part))
    part = msg.get_body(preferencelist=("html",))
    return _tidy(html_to_text(_content(part))) if part is not None else ""


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


def _attachments(msg: EmailMessage) -> tuple[list[Attachment], list[str]]:
    """Invoice files anywhere in the message (forwarded messages included) and the names of the others."""
    found: list[Attachment] = []
    ignored: list[str] = []
    taken: set[str] = set()
    for index, part in enumerate(msg.walk(), start=1):
        if part.is_multipart():
            continue
        content_type, filename = part.get_content_type(), part.get_filename() or ""
        kind = _invoice_kind(content_type, filename)
        if kind is None:
            if filename or part.get_content_disposition() == "attachment":
                ignored.append(filename or content_type)
            continue
        data = part.get_payload(decode=True) or b""
        if (kind == "pdf" and not data.startswith(b"%PDF-")) or (kind == "xml" and not ubl.is_ubl(data)):
            ignored.append(filename or content_type)  # a mislabelled file or XML that is not an e-invoice
            continue
        found.append(Attachment(_file_name(filename, kind, index, taken),
                                "application/pdf" if kind == "pdf" else "application/xml", data))
    return found, ignored


def _sender(msg: EmailMessage) -> str:
    header = msg.get("From")
    addresses = getattr(header, "addresses", ())
    if addresses:
        return addresses[0].addr_spec
    return parseaddr(str(header or ""))[1]


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_message(raw: bytes) -> ParsedEmail:
    """Parse one RFC 822 message. A message without Message-ID gets a stable one from its bytes."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    attachments, ignored = _attachments(msg)
    message_id = _one_line(msg.get("Message-ID")) or f"<sha256-{hashlib.sha256(raw).hexdigest()[:32]}@intake>"
    return ParsedEmail(message_id=message_id, sender=_sender(msg), subject=_one_line(msg.get("Subject")),
                       body=body_text(msg), attachments=tuple(attachments), ignored=tuple(ignored))


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
        raise WebhookError(f"HTTP {response.status_code}: {_error_detail(response)}")
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


def connect(mailbox: MailboxConfig) -> imaplib.IMAP4:
    """Open, log in and select the folder (read-write, to set \\Seen). The certificate is verified."""
    conn = imaplib.IMAP4_SSL(mailbox.host, mailbox.port, ssl_context=ssl.create_default_context(),
                             timeout=IMAP_TIMEOUT_S)
    try:
        conn.login(mailbox.user, mailbox.password)
        _ok(conn.select(_quoted(mailbox.folder)), f"select {mailbox.folder}")
    except BaseException:
        with suppress(Exception):
            conn.logout()
        raise
    return conn


def unseen_uids(conn: imaplib.IMAP4) -> list[str]:
    data = _ok(conn.uid("SEARCH", None, "UNSEEN"), "search")
    return [uid.decode() for uid in (data[0] or b"").split()] if data else []


def fetch_message(conn: imaplib.IMAP4, uid: str) -> bytes:
    for item in _ok(conn.uid("FETCH", uid, "(BODY.PEEK[])"), f"fetch uid {uid}"):
        if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
            return item[1]
    raise imaplib.IMAP4.error(f"fetch uid {uid} returned no message")


def mark_seen(conn: imaplib.IMAP4, uid: str) -> None:
    _ok(conn.uid("STORE", uid, "+FLAGS", "(\\Seen)"), f"store \\Seen on uid {uid}")


# --------------------------------------------------------------------------------------------
# One pass
# --------------------------------------------------------------------------------------------


@dataclass
class PassResult:
    messages: int = 0  # new messages found
    failed: int = 0  # messages left unseen because a POST (or the \Seen flag) failed
    errors: int = 0  # mailboxes that could not be read

    @property
    def ok(self) -> bool:
        return not self.failed and not self.errors


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
                    client: httpx.Client, log: Log) -> bool:
    """Post one message's documents; mark it \\Seen only if every POST succeeded. True on success."""
    mail = parse_message(fetch_message(conn, uid))
    doc_ids, outcomes, errors = [], [], []
    for parts in requests_for(mail, mailbox.channel, webhook.scenario):
        try:
            doc_id, outcome = _reply_summary(*post_document(client, webhook, parts))
        except (httpx.HTTPError, WebhookError) as exc:  # the other documents are still posted
            errors.append(_describe(exc, webhook.password, mailbox.password))
            continue
        doc_ids.append(doc_id)
        outcomes.append(outcome)
    head = f"[intake] mailbox={mailbox.label} uid={uid} from={mail.sender or '?'} attachments={len(mail.attachments)}"
    head += f" ignored={len(mail.ignored)}" if mail.ignored else ""
    result = f" -> doc_id={','.join(doc_ids) or '-'} outcome={','.join(outcomes) or '-'}"
    if errors:
        log(f"{head}{result} FAILED ({'; '.join(errors)}); left unseen")
        return False
    try:
        mark_seen(conn, uid)
    except imaplib.IMAP4.error as exc:  # registered; the retry is deduplicated by the webhook
        log(f"{head}{result} FAILED to mark as seen ({_describe(exc, mailbox.password)})")
        return False
    log(head + result)
    return True


def poll_mailbox(mailbox: MailboxConfig, webhook: WebhookConfig, client: httpx.Client, *,
                 log: Log = print) -> PassResult:
    """One pass over one mailbox. Never raises for IMAP, network or webhook errors: they are counted."""
    result = PassResult()
    try:
        conn = connect(mailbox)
    except (imaplib.IMAP4.error, OSError) as exc:
        log(f"[intake] mailbox={mailbox.label} ERROR cannot open the mailbox: {_describe(exc, mailbox.password)}")
        result.errors += 1
        return result
    try:
        for uid in unseen_uids(conn):
            result.messages += 1
            try:
                ok = process_message(conn, uid, mailbox, webhook, client, log)
            except OSError:
                raise  # a dead connection ends this mailbox's pass
            except Exception as exc:  # noqa: BLE001  one bad message must not stop the others
                log(f"[intake] mailbox={mailbox.label} uid={uid} ERROR {_describe(exc, mailbox.password)}; "
                    "left unseen")
                ok = False
            result.failed += int(not ok)
    except (imaplib.IMAP4.error, OSError) as exc:
        log(f"[intake] mailbox={mailbox.label} ERROR {_describe(exc, mailbox.password)}")
        result.errors += 1
    finally:
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
            result = poll_mailbox(mailbox, settings.webhook, client, log=log)
            total.messages += result.messages
            total.failed += result.failed
            total.errors += result.errors
    finally:
        if own_client:
            client.close()
    log(f"[intake] pass done: {total.messages} new message(s), {total.failed} left unseen, "
        f"{total.errors} mailbox error(s)")
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
