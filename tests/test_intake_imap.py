"""IMAP intake poller (app/intake_imap.py). Never touches the network: imaplib.IMAP4_SSL is a fake IMAP server
serving RFC 822 messages, and the webhook is an httpx.MockTransport that records the multipart fields (or, in
the end-to-end test, the real FastAPI app through TestClient).
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import inspect
import ssl
from datetime import date
from email.message import EmailMessage
from typing import Any, Optional

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config, intake_imap, main, ubl, world
from app.intake_imap import MailboxConfig, Settings, WebhookConfig
from app.models import GateDecision, InboundDocument

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
UBL = ubl.render_ubl(world.DOCUMENT_BY_NO[3])
IMAP_PASSWORD = "imap-app-password-123"
WEBHOOK_PASSWORD = "webhook-secret-456"
URL = "http://velox.test/intake/webhook"
ENV_PREFIXES = ("IMAP", "IMAP2")
ENV_NAMES = ["INTAKE_WEBHOOK_URL", "APP_USERNAME", "APP_PASSWORD", "INTAKE_SCENARIO"] + [
    f"{prefix}_{key}" for prefix in ENV_PREFIXES for key in ("HOST", "PORT", "USER", "PASSWORD", "FOLDER", "CHANNEL",
                                                             "SINCE", "ALLOWED_SENDERS")]
MONTH_NO = {name: no for no, name in enumerate(intake_imap.MONTHS, start=1)}
RECEIVED = date(2026, 9, 25)  # the IMAP internal date of a message added without one


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No intake setting from the developer's environment or .env leaks into a test; no skip is remembered."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(intake_imap, "_SKIPS_LOGGED", {})


# --------------------------------------------------------------------------------------------
# Fake IMAP server
# --------------------------------------------------------------------------------------------


class FakeServer:
    """One mailbox. `folder` is the name on the wire (IMAP modified UTF-7, e.g. "Entw&APw-rfe")."""

    def __init__(self, user: str = "ap@velox.test", password: str = IMAP_PASSWORD, folder: str = "INBOX"):
        self.user, self.password, self.folder = user, password, folder
        self.messages: dict[str, bytes] = {}
        self.received: dict[str, date] = {}
        self.seen: set[str] = set()
        self.connections: list[dict[str, Any]] = []
        self.logged_out = 0
        self.fail_store = False
        self.connect_error: Optional[BaseException] = None  # raised when a client connects
        self.abort_on: Optional[tuple[str, int]] = None  # (command, n): the n-th such command drops the connection
        self.commands: list[tuple[str, ...]] = []  # every UID command, e.g. ("FETCH", "101", "(BODY.PEEK[])")
        self.auth: list[str] = []  # "LOGIN" or "AUTHENTICATE PLAIN", per successful login

    def add(self, raw: bytes, received: date = RECEIVED) -> str:
        uid = str(len(self.messages) + 101)  # UIDs are not sequence numbers
        self.messages[uid] = raw
        self.received[uid] = received
        return uid

    @property
    def unseen(self) -> list[str]:
        return [uid for uid in self.messages if uid not in self.seen]

    def fetched(self, what: str = "(BODY.PEEK[])") -> list[str]:
        return [command[1] for command in self.commands if command[0] == "FETCH" and command[2] == what]


def _from_header(raw: bytes) -> bytes:
    """What BODY.PEEK[HEADER.FIELDS (FROM)] returns: the From line(s) and the blank line after the header."""
    lines, keep = [], False
    for line in raw.split(b"\r\n\r\n", 1)[0].replace(b"\r\n", b"\n").split(b"\n"):
        if line[:1] not in (b" ", b"\t"):
            keep = line.lower().startswith(b"from:")
        if keep:
            lines.append(line)
    return b"\r\n".join(lines) + b"\r\n\r\n"


class FakeIMAP:
    """The part of imaplib.IMAP4_SSL the poller uses; `servers` maps a host name to a FakeServer. Like imaplib,
    LOGIN and SELECT arguments must be ASCII (UnicodeEncodeError otherwise)."""

    servers: dict[str, FakeServer] = {}

    def __init__(self, host: str, port: int = 993, *, ssl_context: Optional[ssl.SSLContext] = None,
                 timeout: Optional[float] = None):
        if host not in self.servers:
            raise ConnectionRefusedError(f"cannot reach {host}:{port}")
        self.server = self.servers[host]
        if self.server.connect_error is not None:
            raise self.server.connect_error
        self.server.connections.append({"port": port, "ssl_context": ssl_context, "timeout": timeout})
        self.logged_in = False

    def login(self, user: str, password: str):
        user.encode("ascii"), password.encode("ascii")  # imaplib's own encoding of the command
        if (user, password) != (self.server.user, self.server.password):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.logged_in = True
        self.server.auth.append("LOGIN")
        return "OK", [b"LOGIN completed"]

    def authenticate(self, mechanism: str, authobject):
        assert mechanism == "PLAIN"
        response = authobject(b"")  # the server's empty continuation
        _authzid, user, password = response.split(b"\0")
        if (user.decode("utf-8"), password.decode("utf-8")) != (self.server.user, self.server.password):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.logged_in = True
        self.server.auth.append("AUTHENTICATE PLAIN")
        return "OK", [b"AUTHENTICATE completed"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        assert self.logged_in and not readonly
        mailbox.encode("ascii")
        if mailbox != self.server.folder:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(len(self.server.messages)).encode()]

    def _maybe_abort(self, command: str) -> None:
        if self.server.abort_on and self.server.abort_on[0] == command:
            count = sum(1 for done in self.server.commands if done[0] == command)
            if count >= self.server.abort_on[1]:
                raise imaplib.IMAP4.abort("socket error: EOF")

    def uid(self, command: str, *args: Any):
        self.server.commands.append((command, *[str(arg) for arg in args]))
        self._maybe_abort(command)
        if command == "SEARCH":
            charset, *criteria = args
            assert charset is None and criteria[0] == "UNSEEN" and len(criteria) in (1, 3), criteria
            uids = self.server.unseen
            if len(criteria) == 3:
                assert criteria[1] == "SINCE"
                day, month, year = criteria[2].split("-")
                since = date(int(year), MONTH_NO[month], int(day))
                uids = [uid for uid in uids if self.server.received[uid] >= since]
            return "OK", [" ".join(uids).encode()]
        if command == "FETCH":
            uid, what = args
            raw = self.server.messages[uid]
            if what == "(BODY.PEEK[HEADER.FIELDS (FROM)])":
                header = _from_header(raw)
                return "OK", [(f"1 (UID {uid} BODY[HEADER.FIELDS (FROM)] {{{len(header)}}}".encode(), header), b")"]
            assert what == "(BODY.PEEK[])"  # PEEK: fetching must not set \Seen
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            uid, operation, flags = args
            assert (operation, flags) == ("+FLAGS", "(\\Seen)")
            if self.server.fail_store:
                return "NO", [b"STORE failed"]
            self.server.seen.add(uid)
            return "OK", [f"1 (UID {uid} FLAGS (\\Seen))".encode()]
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.server.logged_out += 1
        return "BYE", [b"LOGOUT"]


@pytest.fixture()
def imap(monkeypatch) -> dict[str, FakeServer]:
    servers: dict[str, FakeServer] = {}
    monkeypatch.setattr(FakeIMAP, "servers", servers)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return servers


# --------------------------------------------------------------------------------------------
# Mock webhook
# --------------------------------------------------------------------------------------------


def parse_multipart(request: httpx.Request) -> tuple[dict[str, str], dict[str, tuple[str, str, bytes]]]:
    """Text fields and files of a multipart/form-data request (stdlib email parser)."""
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data")
    head = f"Content-Type: {content_type}\r\n\r\n".encode()
    message = email.message_from_bytes(head + request.read(), policy=email.policy.HTTP)
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if part.get_filename() is None:
            fields[name] = payload.decode("utf-8")
        else:
            files[name] = (part.get_filename(), part.get_content_type(), payload)
    return fields, files


class Webhook:
    """Records every POST; answers 201 like contract C6, or the statuses queued in `replies`."""

    def __init__(self, *replies: Any):
        self.replies = list(replies)  # int status, "connect-error", or None (= 201)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fields, files = parse_multipart(request)
        self.calls.append({"url": str(request.url), "fields": fields, "files": files, "headers": request.headers})
        reply = self.replies.pop(0) if self.replies else None
        if reply == "connect-error":
            raise httpx.ConnectError("connection refused", request=request)
        status = reply or 201
        if status >= 400:
            return httpx.Response(status, json={"detail": "something broke"})
        return httpx.Response(status, json={
            "doc_id": f"B-W{len(self.calls):02d}", "scenario": fields.get("scenario"), "registered": True,
            "extracted": "file" in files, "outcome": "posted" if "file" in files else "human_review",
            "exception_type": None if "file" in files else "human_review", "owner_name": None})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def make_email(*, sender: str = "Nordwind Billing <billing@nordwind-logistics.de>", subject: str = "Invoice 1027",
               body: Optional[str] = "Please find our invoice attached.", html: Optional[str] = None,
               attachments: tuple[tuple[str, bytes, str], ...] = (), forwarded: Optional[EmailMessage] = None,
               message_id: Optional[str] = "<msg-1@nordwind-logistics.de>") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "ap@velox.test"
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    if body is not None:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    for filename, data, content_type in attachments:
        maintype, subtype = content_type.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    if forwarded is not None:
        msg.add_attachment(forwarded)  # message/rfc822
    return msg.as_bytes()


def mailbox(host: str = "imap.velox.test", channel: str = "ap_mailbox", user: str = "ap@velox.test") -> MailboxConfig:
    return MailboxConfig(host=host, port=993, user=user, password=IMAP_PASSWORD, folder="INBOX", channel=channel)


def settings(*boxes: MailboxConfig, password: str = "") -> Settings:
    return Settings(webhook=WebhookConfig(url=URL, scenario="tobe", username="velox", password=password),
                    mailboxes=boxes or (mailbox(),))


def run(settings_: Settings, webhook: Webhook) -> tuple[intake_imap.PassResult, list[str]]:
    lines: list[str] = []
    with webhook.client() as client:
        result = intake_imap.run_once(settings_, client=client, log=lines.append)
    return result, lines


@pytest.fixture()
def server(imap) -> FakeServer:
    imap["imap.velox.test"] = FakeServer()
    return imap["imap.velox.test"]


def set_env(monkeypatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


# --------------------------------------------------------------------------------------------
# One message, one or more documents
# --------------------------------------------------------------------------------------------


def test_pdf_attachment_is_posted_with_the_email_fields_then_marked_seen(server):
    uid = server.add(make_email(attachments=(("NWL-2026-01027.pdf", PDF, "application/pdf"),)))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and result.messages == 1
    assert server.seen == {uid}
    [call] = webhook.calls
    assert call["url"] == URL
    assert call["fields"] == {"channel": "ap_mailbox", "sender": "billing@nordwind-logistics.de",
                              "subject": "Invoice 1027", "scenario": "tobe",
                              "message_id": "<msg-1@nordwind-logistics.de>",
                              "email_body": "Please find our invoice attached."}
    assert call["files"] == {"file": ("NWL-2026-01027.pdf", "application/pdf", PDF)}
    assert "authorization" not in call["headers"]  # APP_PASSWORD not set: no basic auth
    assert lines[0] == (f"[intake] mailbox=ap@velox.test/INBOX uid={uid} from=billing@nordwind-logistics.de "
                        "attachments=1 -> doc_id=B-W01 outcome=posted")


def test_ubl_attachment_is_posted_as_xml(server):
    server.add(make_email(sender="einvoice@metromedia.de", subject="E-Rechnung MM-2026-248", body="",
                          attachments=(("MM-2026-248.xml", UBL, "application/xml"),)))
    webhook = Webhook()
    result, _ = run(settings(), webhook)
    assert result.ok
    [call] = webhook.calls
    assert call["files"] == {"file": ("MM-2026-248.xml", "application/xml", UBL)}
    assert "email_body" not in call["fields"]  # no text in the email: the field is left out


def test_ubl_sent_as_text_xml_or_octet_stream_is_recognised(server):
    server.add(make_email(attachments=(("invoice.xml", UBL, "text/xml"), ("scan.pdf", PDF, "application/octet-stream"))))
    webhook = Webhook()
    run(settings(), webhook)
    assert [call["files"]["file"][:2] for call in webhook.calls] == [("invoice.xml", "application/xml"),
                                                                     ("scan.pdf", "application/pdf")]


def test_body_only_email_posts_the_text_without_a_file(server):
    text = ("Guten Tag, anbei unsere Rechnung 2026/140 vom 06.11.2026 über 58,31 EUR.\n\n"
            "Mit freundlichen Grüßen, Kaffee & Co OHG")
    uid = server.add(make_email(sender="info@kaffee-und-co.de", subject="Rechnung 2026/140", body=text))
    webhook = Webhook()
    result, lines = run(settings(mailbox(channel="store_mailbox")), webhook)
    assert result.ok and server.seen == {uid}
    [call] = webhook.calls
    assert call["files"] == {}
    assert call["fields"]["email_body"] == text
    assert call["fields"]["channel"] == "store_mailbox"
    assert "attachments=0 -> doc_id=B-W01 outcome=human_review/human_review" in lines[0]


def test_html_only_email_is_sent_as_text(server):
    html = ("<html><head><style>p {color: red}</style></head><body><p>Rechnung&nbsp;2026/141</p>"
            "<div>Betrag: 12,50&nbsp;EUR<br>Danke</div><script>alert(1)</script></body></html>")
    server.add(make_email(body=None, html=html))
    webhook = Webhook()
    run(settings(), webhook)
    assert webhook.calls[0]["fields"]["email_body"] == "Rechnung 2026/141\n\nBetrag: 12,50 EUR\nDanke"


def test_plain_text_is_preferred_over_html(server):
    server.add(make_email(body="Plain version", html="<p>HTML version</p>"))
    webhook = Webhook()
    run(settings(), webhook)
    assert webhook.calls[0]["fields"]["email_body"] == "Plain version"


def test_empty_email_without_attachment_still_reaches_the_webhook(server):
    server.add(make_email(body=None, attachments=(("logo.png", PNG, "image/png"),)))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok
    assert webhook.calls[0]["fields"]["email_body"] == intake_imap.EMPTY_BODY
    assert "attachments=0 ignored=1" in lines[0]


def test_forwarded_invoice_keeps_the_managers_comment(server):
    comment = ("Bonjour, facture reçue au magasin la semaine dernière (affiches et flyers de la campagne "
               "d'automne). Merci de la régler. Luc")
    server.add(make_email(sender="Luc Bernard <luc.bernard@velox.com>", subject="TR: Facture QP-26-1107",
                          body=comment, attachments=(("QP-26-1107.pdf", PDF, "application/pdf"),)))
    webhook = Webhook()
    run(settings(), webhook)
    [call] = webhook.calls
    assert call["fields"]["sender"] == "luc.bernard@velox.com"
    assert call["fields"]["subject"] == "TR: Facture QP-26-1107"
    assert call["fields"]["email_body"] == comment
    assert call["files"]["file"][0] == "QP-26-1107.pdf"


def test_invoice_inside_a_message_forwarded_as_attachment_is_found(server):
    original = EmailMessage()
    original["From"] = "accounts@quickprint.fr"
    original["Subject"] = "Facture QP-26-1107"
    original.set_content("Veuillez trouver ci-joint notre facture.")
    original.add_attachment(PDF, maintype="application", subtype="pdf", filename="QP-26-1107.pdf")
    server.add(make_email(sender="luc.bernard@velox.com", subject="Fwd: Facture", body="Merci de la régler. Luc",
                          forwarded=original))
    webhook = Webhook()
    run(settings(), webhook)
    [call] = webhook.calls
    assert call["files"] == {"file": ("QP-26-1107.pdf", "application/pdf", PDF)}
    assert call["fields"]["email_body"] == "Merci de la régler. Luc"  # the forwarder's text, not the original's
    assert call["fields"]["sender"] == "luc.bernard@velox.com"


def test_attachments_that_are_not_invoices_are_ignored(server):
    server.add(make_email(attachments=(
        ("logo.png", PNG, "image/png"),
        ("order.xml", b"<order><id>1</id></order>", "application/xml"),  # XML, but not a UBL invoice
        ("fake.pdf", b"this is not a pdf", "application/pdf"),  # mislabelled
        ("invoice.pdf", PDF, "application/pdf"),
    )))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok
    assert [call["files"]["file"][0] for call in webhook.calls] == ["invoice.pdf"]
    assert "attachments=1 ignored=3" in lines[0]


def test_two_attachments_make_two_posts_with_the_same_message_id(server):
    uid = server.add(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),
                                             ("invoice.pdf", PDF + b"%second", "application/pdf"),
                                             ("einvoice.xml", UBL, "application/xml"))))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    # Same message id on every POST; names made unique, because the webhook deduplicates on id + file name.
    assert {call["fields"]["message_id"] for call in webhook.calls} == {"<msg-1@nordwind-logistics.de>"}
    assert [call["files"]["file"][0] for call in webhook.calls] == ["invoice.pdf", "invoice-2.pdf", "einvoice.xml"]
    assert "attachments=3 -> doc_id=B-W01,B-W02,B-W03 outcome=posted,posted,posted" in lines[0]


def test_message_without_message_id_gets_a_stable_one():
    raw = make_email(message_id=None)
    first, second = intake_imap.parse_message(raw), intake_imap.parse_message(raw)
    assert first.message_id == second.message_id
    assert first.message_id.startswith("<sha256-") and first.message_id.endswith("@intake>")
    assert intake_imap.parse_message(make_email(message_id=None, subject="Other")).message_id != first.message_id


def test_attachment_without_a_name_gets_one():
    msg = EmailMessage()
    msg["From"] = "a@b.test"
    msg.set_content("x")
    msg.add_attachment(PDF, maintype="application", subtype="pdf")  # no filename
    parsed = intake_imap.parse_message(msg.as_bytes())
    assert [(a.filename, a.content_type) for a in parsed.attachments] == [("attachment-3.pdf", "application/pdf")]


# --------------------------------------------------------------------------------------------
# Forwarded messages, odd parts and malformed headers never lose or block a message
# --------------------------------------------------------------------------------------------

KAFFEE_TEXT = "Guten Tag, anbei unsere Rechnung 2026/140 vom 06.11.2026 über 58,31 EUR."
KAFFEE_HEADERS = ("From: Kaffee & Co OHG <info@kaffee-und-co.de>\nDate: Fri, 06 Nov 2026 09:14:00 +0100\n"
                  "Subject: Rechnung 2026/140")
UNKNOWN_ENCODING_XML = f'<?xml version="1.0" encoding="x-foo"?><Invoice xmlns="{ubl.INVOICE_NS}"/>'.encode("ascii")


def supplier_email(text: str = KAFFEE_TEXT, attachments: tuple[tuple[str, bytes, str], ...] = ()) -> EmailMessage:
    original = EmailMessage()
    original["From"] = "Kaffee & Co OHG <info@kaffee-und-co.de>"
    original["Date"] = "Fri, 06 Nov 2026 09:14:00 +0100"
    original["Subject"] = "Rechnung 2026/140"
    original.set_content(text)
    for filename, data, content_type in attachments:
        maintype, subtype = content_type.split("/")
        original.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return original


def forward(inner: EmailMessage, text: str = "Pour info.") -> EmailMessage:
    middle = EmailMessage()
    middle["From"] = "luc.bernard@velox.com"
    middle["Subject"] = "TR: Rechnung 2026/140"
    middle.set_content(text)
    middle.add_attachment(inner)  # message/rfc822
    return middle


def with_header(raw: bytes, name: str, value: Optional[bytes]) -> bytes:
    """The message with one header's value replaced as raw bytes (None: the header removed), bypassing the
    email package, which refuses to write malformed values."""
    head, sep, rest = raw.partition(b"\n\n")
    lines = head.split(b"\n")
    index = next(i for i, line in enumerate(lines) if line.lower().startswith(name.lower().encode() + b":"))
    if value is None:
        del lines[index]
    else:
        lines[index] = name.encode() + b": " + value
    return b"\n".join(lines) + sep + rest


def test_invoice_text_of_a_message_forwarded_as_attachment_is_appended_to_the_body(server):
    uid = server.add(make_email(sender="anna.schmidt@velox.com", subject="Fwd: Rechnung 2026/140",
                                body="Bitte bezahlen. Anna", forwarded=supplier_email()))
    webhook = Webhook()
    result, _ = run(settings(mailbox(channel="store_mailbox")), webhook)
    assert result.ok and server.seen == {uid}
    [call] = webhook.calls
    assert call["files"] == {}
    assert call["fields"]["email_body"] == (f"Bitte bezahlen. Anna\n\n{intake_imap.FORWARDED}\n{KAFFEE_HEADERS}\n\n"
                                            f"{KAFFEE_TEXT}")
    assert call["fields"]["sender"] == "anna.schmidt@velox.com"  # the forwarder, as for an inline forward


def test_forward_without_a_comment_and_a_forward_of_a_forward_keep_every_text(server):
    html = supplier_email()
    html.set_content(f"<p>{KAFFEE_TEXT}</p>", subtype="html")  # an HTML-only original
    server.add(make_email(sender="anna.schmidt@velox.com", subject="Fwd: TR: Rechnung 2026/140", body=None,
                          forwarded=forward(html)))
    webhook = Webhook()
    run(settings(), webhook)
    assert webhook.calls[0]["fields"]["email_body"] == (
        f"{intake_imap.FORWARDED}\nFrom: luc.bernard@velox.com\nSubject: TR: Rechnung 2026/140\n\nPour info.\n\n"
        f"{intake_imap.FORWARDED}\n{KAFFEE_HEADERS}\n\n{KAFFEE_TEXT}")


def test_invoice_file_inside_a_forward_of_a_forward_is_posted_with_the_forwarders_text(server):
    inner = supplier_email(attachments=(("KC-2026-140.pdf", PDF, "application/pdf"),))
    server.add(make_email(body="Merci de la régler. Luc", forwarded=forward(inner)))
    webhook = Webhook()
    result, _ = run(settings(), webhook)
    assert result.ok
    [call] = webhook.calls
    assert call["files"] == {"file": ("KC-2026-140.pdf", "application/pdf", PDF)}
    assert call["fields"]["email_body"] == "Merci de la régler. Luc"  # the file is the invoice: no forwarded text


def test_xml_with_an_unknown_encoding_is_ignored_and_the_pdf_still_posted(server):
    uid = server.add(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),
                                             ("meta.xml", UNKNOWN_ENCODING_XML, "application/xml"))))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    assert [call["files"]["file"][0] for call in webhook.calls] == ["invoice.pdf"]
    assert "attachments=1 ignored=1 [meta.xml (not a UBL e-invoice)] -> doc_id=B-W01" in lines[0]


def test_a_part_that_cannot_be_read_is_ignored_and_the_rest_still_posted(server, monkeypatch):
    def broken(_data: bytes) -> bool:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(ubl, "is_ubl", broken)
    uid = server.add(make_email(attachments=(("einvoice.xml", UBL, "application/xml"),
                                             ("invoice.pdf", PDF, "application/pdf"))))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    assert [call["files"]["file"][0] for call in webhook.calls] == ["invoice.pdf"]
    assert "ignored=1 [einvoice.xml (unreadable: RuntimeError)]" in lines[0]


def test_ignored_parts_are_listed_with_the_reason():
    parsed = intake_imap.parse_message(make_email(attachments=(
        ("logo.png", PNG, "image/png"), ("order.xml", b"<order/>", "application/xml"),
        ("fake.pdf", b"not a pdf", "application/pdf"))))
    assert parsed.ignored == ("logo.png", "order.xml (not a UBL e-invoice)", "fake.pdf (not a PDF)")


@pytest.mark.parametrize("header, expected", [
    (b"<>", None),  # policy.default raises IndexError on these three
    (b"<@>", "<@>"),
    (b"<@abc>", "<@abc>"),
    (b"<no-at-sign>", None),
    (b"   ", None),
    (b"<a b@c d>", "<a b@c d>"),  # kept whole: the parsed value would be "<a"
])
def test_malformed_message_id_never_crashes_and_falls_back_to_the_hash_id(header, expected):
    raw = with_header(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),)), "Message-ID", header)
    parsed = intake_imap.parse_message(raw)
    if expected is None:
        assert parsed.message_id.startswith("<sha256-") and parsed.message_id.endswith("@intake>")
        assert parsed.message_id == intake_imap.parse_message(raw).message_id  # stable
    else:
        assert parsed.message_id == expected
    assert [attachment.filename for attachment in parsed.attachments] == ["invoice.pdf"]


def test_message_with_an_empty_message_id_is_posted_and_marked_seen(server):
    uid = server.add(with_header(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),)),
                                 "Message-ID", b"<>"))
    webhook = Webhook()
    result, _ = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    assert webhook.calls[0]["fields"]["message_id"].startswith("<sha256-")


def test_malformed_from_and_subject_headers_never_crash(monkeypatch):
    parsed = intake_imap.parse_message(with_header(make_email(), "From", b"a@"))  # IndexError in policy.default
    assert parsed.sender == intake_imap.UNKNOWN_SENDER
    assert parsed.body == ("[intake note] the From header has no usable email address: a@\n\n"
                           "Please find our invoice attached.")

    real_get = EmailMessage.get

    def get(self: EmailMessage, name: str, failobj: Any = None) -> Any:
        if name.lower() == "subject":
            raise IndexError("list index out of range")
        return real_get(self, name, failobj)

    monkeypatch.setattr(EmailMessage, "get", get)
    parsed = intake_imap.parse_message(make_email(subject="Rechnung für Oktober"))  # RFC 2047-encoded on the wire
    assert parsed.subject == "Rechnung für Oktober"  # from the raw header, decoded
    assert parsed.sender == "billing@nordwind-logistics.de"


@pytest.mark.parametrize("from_header, shown", [
    (b"Nordwind Billing", "Nordwind Billing"),  # a name, no address
    (b"billing@erp-server", "billing@erp-server"),  # no dot in the domain: the webhook refuses it
    (b"undisclosed-recipients:;", "undisclosed-recipients:;"),  # an empty group
    (None, "(none)"),  # no From header at all
])
def test_no_usable_sender_address_falls_back_to_a_placeholder_and_reaches_review(server, from_header, shown):
    uid = server.add(with_header(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),)), "From",
                                 from_header))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    [call] = webhook.calls
    assert call["fields"]["sender"] == intake_imap.UNKNOWN_SENDER == "unknown-sender@invalid.example"
    assert call["fields"]["email_body"] == (f"[intake note] the From header has no usable email address: {shown}\n\n"
                                            "Please find our invoice attached.")
    assert f"from={intake_imap.UNKNOWN_SENDER} attachments=1" in lines[0]


def test_the_attachment_limit_is_the_webhooks():
    assert intake_imap.MAX_ATTACHMENT_BYTES == main.MAX_UPLOAD_BYTES == 10 * 1024 * 1024


def test_an_invoice_file_over_the_limit_is_not_posted_and_the_reviewer_is_told(server, monkeypatch):
    monkeypatch.setattr(intake_imap, "MAX_ATTACHMENT_BYTES", 1024)  # the real limit is 10 MB
    big = PDF + b"%" + b"x" * 2000
    uid = server.add(make_email(attachments=(("scan.pdf", big, "application/pdf"),
                                             ("invoice.pdf", PDF, "application/pdf"))))
    webhook = Webhook()
    result, lines = run(settings(), webhook)
    assert result.ok and server.seen == {uid}
    [call] = webhook.calls  # never uploaded: the webhook would answer 413 on every pass
    assert call["files"]["file"][0] == "invoice.pdf"
    assert call["fields"]["email_body"] == ("[intake note] scan.pdf was not posted: larger than 10 MB, the intake "
                                            "limit\n\nPlease find our invoice attached.")
    assert "attachments=1 ignored=1 [scan.pdf (larger than 10 MB)]" in lines[0]


def test_an_email_whose_only_invoice_is_too_large_still_reaches_review_as_text(server, monkeypatch):
    monkeypatch.setattr(intake_imap, "MAX_ATTACHMENT_BYTES", 1024)
    server.add(make_email(attachments=(("scan.pdf", PDF + b"x" * 2000, "application/pdf"),)))
    webhook = Webhook()
    result, _ = run(settings(), webhook)
    assert result.ok
    [call] = webhook.calls
    assert call["files"] == {}
    assert call["fields"]["email_body"].startswith("[intake note] scan.pdf was not posted: larger than 10 MB")


# --------------------------------------------------------------------------------------------
# Failures: the message stays unseen until every POST succeeded
# --------------------------------------------------------------------------------------------


def test_failed_post_leaves_the_message_unseen_and_the_next_pass_retries(server):
    uid = server.add(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),)))
    result, lines = run(settings(), Webhook(500))
    assert not result.ok and result.failed == 1
    assert server.seen == set()
    assert lines[0].endswith("-> doc_id=- outcome=- FAILED (WebhookError: HTTP 500: something broke); left unseen")
    result, _ = run(settings(), Webhook())
    assert result.ok and server.seen == {uid}


def test_partial_failure_leaves_the_message_unseen_and_reposts_everything(server):
    server.add(make_email(attachments=(("a.pdf", PDF, "application/pdf"), ("b.pdf", PDF, "application/pdf"))))
    webhook = Webhook(201, 503)
    result, lines = run(settings(), webhook)
    assert result.failed == 1 and server.seen == set()
    assert "doc_id=B-W01 outcome=posted FAILED (WebhookError: HTTP 503" in lines[0]
    retry = Webhook(200, 201)  # the webhook already has a.pdf for this message id: 200 with the same doc
    result, lines = run(settings(), retry)
    assert result.ok and len(server.seen) == 1
    assert [call["files"]["file"][0] for call in retry.calls] == ["a.pdf", "b.pdf"]
    assert "outcome=posted (already registered),posted" in lines[0]


def test_network_error_leaves_the_message_unseen(server):
    server.add(make_email())
    result, lines = run(settings(), Webhook("connect-error"))
    assert result.failed == 1 and server.seen == set()
    assert "FAILED (ConnectError: connection refused); left unseen" in lines[0]


def test_every_attachment_is_tried_even_after_a_failure(server):
    server.add(make_email(attachments=(("a.pdf", PDF, "application/pdf"), ("b.pdf", PDF, "application/pdf"))))
    webhook = Webhook(500, 201)
    result, _ = run(settings(), webhook)
    assert len(webhook.calls) == 2 and result.failed == 1 and server.seen == set()


@pytest.mark.parametrize("status", [400, 409, 413, 415, 422])
def test_a_permanent_rejection_is_logged_marked_seen_and_counted_apart(server, status):
    uid = server.add(make_email(attachments=(("invoice.pdf", PDF, "application/pdf"),)))
    result, lines = run(settings(), Webhook(status))
    assert result.ok and (result.rejected, result.failed) == (1, 0)  # the pass does not fail
    assert server.seen == {uid}
    assert lines[0].endswith(f"-> doc_id=- outcome=- REJECTED (WebhookError: HTTP {status}: something broke); "
                             "marked as seen, not retried")
    assert lines[-1].endswith("0 left unseen, 0 mailbox error(s), 1 rejected, 0 skipped (sender not allowed)")
    webhook = Webhook()
    result, _ = run(settings(), webhook)
    assert result.messages == 0 and webhook.calls == []  # never posted again


@pytest.mark.parametrize("status", [401, 403, 404, 405, 407, 408, 425, 429, 500, 502, 503])
def test_transient_and_setup_errors_leave_the_message_unseen(server, status):
    # 408 / 425 / 429 and 5xx pass; 401 / 403 / 404 / 405 / 407 mean the poller's URL or password is wrong, and
    # marking every message read would lose them all.
    server.add(make_email())
    result, lines = run(settings(), Webhook(status))
    assert not result.ok and (result.failed, result.rejected) == (1, 0) and server.seen == set()
    assert lines[0].endswith("; left unseen")


def test_a_rejected_attachment_next_to_a_posted_one(server):
    uid = server.add(make_email(attachments=(("a.pdf", PDF, "application/pdf"), ("b.pdf", PDF, "application/pdf"))))
    result, lines = run(settings(), Webhook(201, 422))
    assert result.ok and result.rejected == 1 and server.seen == {uid}
    assert "-> doc_id=B-W01 outcome=posted REJECTED (WebhookError: HTTP 422: something broke)" in lines[0]


def test_a_transient_failure_wins_over_a_rejection(server):
    server.add(make_email(attachments=(("a.pdf", PDF, "application/pdf"), ("b.pdf", PDF, "application/pdf"))))
    result, lines = run(settings(), Webhook(422, 503))
    assert (result.failed, result.rejected) == (1, 0) and server.seen == set()
    assert lines[0].endswith("FAILED (WebhookError: HTTP 503: something broke; WebhookError: HTTP 422: something "
                             "broke); left unseen")


def test_failure_to_set_seen_counts_as_a_failure(server):
    server.add(make_email())
    server.fail_store = True
    result, lines = run(settings(), Webhook())
    assert result.failed == 1
    assert "FAILED to mark as seen" in lines[0]


def test_unreadable_mailbox_is_an_error_and_other_mailboxes_still_run(imap):
    imap["imap.velox.test"] = FakeServer(password="another-password")
    imap["imap.store.test"] = FakeServer(user="store@velox.test")
    imap["imap.store.test"].add(make_email())
    boxes = (mailbox(), mailbox(host="imap.store.test", channel="store_mailbox", user="store@velox.test"),
             mailbox(host="imap.down.test"))
    result, lines = run(settings(*boxes), Webhook())
    assert result.errors == 2 and result.messages == 1 and result.failed == 0 and not result.ok
    assert "mailbox=ap@velox.test/INBOX ERROR cannot open the mailbox: error: [AUTHENTICATIONFAILED]" in lines[0]
    assert "ConnectionRefusedError" in lines[2]
    assert imap["imap.velox.test"].logged_out == 1  # the failed login still closed the connection
    assert all(IMAP_PASSWORD not in line for line in lines)


def test_missing_folder_is_a_mailbox_error(imap):
    imap["imap.velox.test"] = FakeServer(folder="Invoices")
    result, lines = run(settings(), Webhook())
    assert result.errors == 1
    assert "select INBOX failed" in lines[0]


def test_imap_uses_tls_with_certificate_verification(server):
    run(settings(), Webhook())
    [connection] = server.connections
    context = connection["ssl_context"]
    assert connection["port"] == 993 and connection["timeout"] == intake_imap.IMAP_TIMEOUT_S
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert server.logged_out == 1


def test_folder_names_with_spaces_are_quoted():
    assert intake_imap._quoted("INBOX") == "INBOX"
    assert intake_imap._quoted("[Gmail]/All Mail") == '"[Gmail]/All Mail"'
    assert intake_imap._quoted('"Already quoted"') == '"Already quoted"'


@pytest.mark.parametrize("name, wire", [
    ("INBOX", "INBOX"),
    ("[Gmail]/All Mail", "[Gmail]/All Mail"),
    ("Entwürfe", "Entw&APw-rfe"),
    ("Rechnungseingänge", "Rechnungseing&AOQ-nge"),
    ("Factures reçues 2026", "Factures re&AOc-ues 2026"),
    ("~peter/mail/台北/日本語", "~peter/mail/&U,BTFw-/&ZeVnLIqe-"),  # RFC 3501 section 5.1.3
    ("😀", "&2D3eAA-"),  # outside the BMP: a UTF-16 surrogate pair
    ("R&D", "R&-D"),
    ("Q&A-2026", "Q&-A-2026"),  # "&A-" is not a valid encoded run: a plain name
    ("Entw&APw-rfe", "Entw&APw-rfe"),  # already encoded (e.g. copied from a LIST response): kept
    ("&-", "&-"),
])
def test_folder_names_are_sent_in_imap_modified_utf7(name, wire):
    assert intake_imap.imap_utf7(name) == wire


def test_non_ascii_folder_is_selected_and_logged_by_its_name(imap):
    imap["imap.velox.test"] = FakeServer(folder="Rechnungseing&AOQ-nge")
    uid = imap["imap.velox.test"].add(make_email())
    box = MailboxConfig(host="imap.velox.test", port=993, user="ap@velox.test", password=IMAP_PASSWORD,
                        folder="Rechnungseingänge", channel="ap_mailbox")
    result, lines = run(settings(box), Webhook())
    assert result.ok and imap["imap.velox.test"].seen == {uid}
    assert lines[0].startswith(f"[intake] mailbox=ap@velox.test/Rechnungseingänge uid={uid} ")


def test_non_ascii_password_is_sent_with_authenticate_plain(imap):
    password = "contraseña-Grüße"
    imap["imap.velox.test"] = FakeServer(password=password)
    uid = imap["imap.velox.test"].add(make_email())
    box = MailboxConfig(host="imap.velox.test", port=993, user="ap@velox.test", password=password, folder="INBOX",
                        channel="ap_mailbox")
    result, lines = run(settings(box), Webhook())
    assert result.ok and imap["imap.velox.test"].seen == {uid}
    assert imap["imap.velox.test"].auth == ["AUTHENTICATE PLAIN"]
    assert all(password not in line for line in lines)


def test_ascii_credentials_use_login(server):
    run(settings(), Webhook())
    assert server.auth == ["LOGIN"]


def test_wrong_non_ascii_password_is_a_redacted_mailbox_error(imap):
    imap["imap.velox.test"] = FakeServer()
    box = MailboxConfig(host="imap.velox.test", port=993, user="ap@velox.test", password="contraseña",
                        folder="INBOX", channel="ap_mailbox")
    result, lines = run(settings(box), Webhook())
    assert result.errors == 1
    assert "ERROR cannot open the mailbox: error: [AUTHENTICATIONFAILED]" in lines[0]
    assert all("contraseña" not in line for line in lines)


@pytest.mark.parametrize("error", [ValueError(f"odd failure with {IMAP_PASSWORD}"),
                                   UnicodeEncodeError("ascii", "ñ", 0, 1, "ordinal not in range(128)"),
                                   ssl.SSLCertVerificationError("certificate verify failed")],
                         ids=["value-error", "unicode", "certificate"])
def test_any_error_opening_a_mailbox_is_logged_redacted_and_the_next_mailbox_still_runs(imap, error):
    imap["imap.velox.test"] = FakeServer()
    imap["imap.velox.test"].connect_error = error
    imap["imap.store.test"] = FakeServer(user="store@velox.test")
    store_uid = imap["imap.store.test"].add(make_email())
    boxes = (mailbox(), mailbox(host="imap.store.test", channel="store_mailbox", user="store@velox.test"))
    result, lines = run(settings(*boxes), Webhook())
    assert (result.errors, result.messages, result.failed) == (1, 1, 0)
    assert imap["imap.store.test"].seen == {store_uid}
    assert f"mailbox=ap@velox.test/INBOX ERROR cannot open the mailbox: {type(error).__name__}" in lines[0]
    assert all(IMAP_PASSWORD not in line for line in lines)


def test_a_dropped_connection_ends_the_mailbox_pass_with_one_error(imap):
    imap["imap.velox.test"] = FakeServer()
    ap = imap["imap.velox.test"]
    uids = [ap.add(make_email(message_id=f"<m{no}@nordwind-logistics.de>")) for no in range(4)]
    ap.abort_on = ("FETCH", 2)  # the second FETCH finds the socket closed (imaplib raises IMAP4.abort)
    imap["imap.store.test"] = FakeServer(user="store@velox.test")
    imap["imap.store.test"].add(make_email())
    boxes = (mailbox(), mailbox(host="imap.store.test", channel="store_mailbox", user="store@velox.test"))
    result, lines = run(settings(*boxes), Webhook())
    assert (result.messages, result.failed, result.errors) == (3, 0, 1)
    assert ap.seen == {uids[0]} and ap.fetched() == uids[:2]  # no attempt on the remaining UIDs
    assert [line for line in lines if "abort" in line] == [
        "[intake] mailbox=ap@velox.test/INBOX ERROR abort: socket error: EOF; pass of this mailbox ended"]
    assert imap["imap.store.test"].seen  # the other mailbox is still polled


def test_a_connection_dropped_while_setting_seen_ends_the_pass_too(server):
    server.add(make_email(message_id="<a@nordwind-logistics.de>"))
    server.add(make_email(message_id="<b@nordwind-logistics.de>"))
    server.abort_on = ("STORE", 1)
    result, lines = run(settings(), Webhook())
    assert (result.failed, result.errors) == (0, 1) and server.seen == set()
    assert not any("FAILED to mark as seen" in line for line in lines)
    assert len(server.fetched()) == 1


# --------------------------------------------------------------------------------------------
# IMAP_SINCE and IMAP_ALLOWED_SENDERS
# --------------------------------------------------------------------------------------------


def test_imap_dates_use_english_month_names():
    assert intake_imap.imap_date(date(2026, 9, 5)) == "05-Sep-2026"
    assert intake_imap.imap_date(date(2027, 1, 31)) == "31-Jan-2027"


def test_since_limits_the_search_to_messages_received_from_that_day(server):
    old = server.add(make_email(message_id="<old@nordwind-logistics.de>"), received=date(2026, 9, 1))
    new = server.add(make_email(message_id="<new@nordwind-logistics.de>"), received=date(2026, 9, 25))
    box = MailboxConfig(host="imap.velox.test", port=993, user="ap@velox.test", password=IMAP_PASSWORD,
                        folder="INBOX", channel="ap_mailbox", since=date(2026, 9, 25))
    webhook = Webhook()
    result, _ = run(settings(box), webhook)
    assert result.ok and result.messages == 1
    assert server.seen == {new} and server.unseen == [old]  # the old unread mail is left alone
    assert ("SEARCH", "None", "UNSEEN", "SINCE", "25-Sep-2026") in server.commands


def test_since_and_allowed_senders_settings(monkeypatch):
    set_env(monkeypatch, IMAP_HOST="imap.gmail.com", IMAP_USER="ap.velox@gmail.com", IMAP_PASSWORD=IMAP_PASSWORD,
            IMAP_SINCE="2026-09-25", IMAP_ALLOWED_SENDERS=" Billing@Nordwind-Logistics.de, @velox.com,, @VELOX.com ",
            IMAP2_HOST="imap.gmail.com", IMAP2_USER="store.velox@gmail.com", IMAP2_PASSWORD="other",
            IMAP2_ALLOWED_SENDERS="@kaffee-und-co.de")
    first, second = intake_imap.load_settings().mailboxes
    assert first.since == date(2026, 9, 25)
    assert first.allowed_senders == ("billing@nordwind-logistics.de", "@velox.com")
    assert (second.since, second.allowed_senders) == (None, ("@kaffee-und-co.de",))


@pytest.mark.parametrize("sender, allowed", [
    ("billing@nordwind-logistics.de", True),
    ("Billing@Nordwind-Logistics.DE", True),
    ("luc.bernard@velox.com", True),  # the domain
    ("x@mail.velox.com", False),  # a subdomain is another domain
    ("x@evilvelox.com", False),
    ("velox.com@evil.example", False),
    ("other@nordwind-logistics.de", False),
    (None, False),  # no usable From address
])
def test_sender_allowlist_matches_addresses_and_exact_domains(sender, allowed):
    assert intake_imap.sender_allowed(sender, ("billing@nordwind-logistics.de", "@velox.com")) is allowed
    assert intake_imap.sender_allowed(sender, ()) is True  # no allowlist: everyone


def test_other_senders_are_skipped_left_unseen_and_logged_once(server):
    nordwind = server.add(make_email(message_id="<1@nordwind-logistics.de>"))
    store = server.add(make_email(sender="Luc Bernard <luc.bernard@velox.com>", message_id="<2@velox.com>"))
    google = server.add(make_email(sender="Google <no-reply@accounts.google.com>", subject="Security alert",
                                   message_id="<3@accounts.google.com>"))
    nameless = server.add(with_header(make_email(message_id="<4@x.example>"), "From", b"Nordwind Billing"))
    box = MailboxConfig(host="imap.velox.test", port=993, user="ap@velox.test", password=IMAP_PASSWORD,
                        folder="INBOX", channel="ap_mailbox",
                        allowed_senders=("billing@nordwind-logistics.de", "@velox.com"))
    webhook = Webhook()
    result, lines = run(settings(box), webhook)
    assert result.ok and (result.messages, result.skipped, result.failed) == (4, 2, 0)
    assert server.seen == {nordwind, store} and server.unseen == [google, nameless]
    assert server.fetched() == [nordwind, store]  # the others: only their From header was read
    assert [call["fields"]["sender"] for call in webhook.calls] == ["billing@nordwind-logistics.de",
                                                                   "luc.bernard@velox.com"]
    assert [line for line in lines if "SKIPPED" in line] == [
        f"[intake] mailbox=ap@velox.test/INBOX uid={google} from=no-reply@accounts.google.com SKIPPED: not an "
        "allowed sender; left unseen (logged once)",
        f"[intake] mailbox=ap@velox.test/INBOX uid={nameless} from=? SKIPPED: not an allowed sender; left unseen "
        "(logged once)"]
    assert lines[-1].endswith("0 rejected, 2 skipped (sender not allowed)")

    result, lines = run(settings(box), Webhook())  # the next pass: still skipped, not logged again
    assert (result.messages, result.skipped) == (2, 2) and server.unseen == [google, nameless]
    assert not any("SKIPPED" in line for line in lines)


# --------------------------------------------------------------------------------------------
# Basic auth, settings, two mailboxes, CLI
# --------------------------------------------------------------------------------------------


def test_basic_auth_header_is_sent_when_configured(server):
    server.add(make_email())
    webhook = Webhook()
    result, lines = run(settings(password=WEBHOOK_PASSWORD), webhook)
    assert result.ok
    expected = httpx.BasicAuth("velox", WEBHOOK_PASSWORD).auth_flow(httpx.Request("POST", URL))
    assert webhook.calls[0]["headers"]["authorization"] == next(expected).headers["authorization"]
    assert all(WEBHOOK_PASSWORD not in line for line in lines)


def test_settings_defaults_and_second_mailbox(monkeypatch):
    set_env(monkeypatch, IMAP_HOST="imap.gmail.com", IMAP_USER="ap.velox@gmail.com", IMAP_PASSWORD=IMAP_PASSWORD,
            IMAP2_HOST="imap.gmail.com", IMAP2_USER="store.velox@gmail.com", IMAP2_PASSWORD="other",
            IMAP2_FOLDER="Rechnungen", IMAP2_PORT="1993")
    loaded = intake_imap.load_settings()
    assert loaded.webhook == WebhookConfig(url=intake_imap.DEFAULT_WEBHOOK_URL, scenario="tobe", username="velox",
                                           password="")
    first, second = loaded.mailboxes
    assert (first.host, first.port, first.user, first.folder, first.channel) == (
        "imap.gmail.com", 993, "ap.velox@gmail.com", "INBOX", "ap_mailbox")
    assert (second.port, second.folder, second.channel) == (1993, "Rechnungen", "store_mailbox")
    assert IMAP_PASSWORD not in repr(loaded)  # passwords never appear in a repr (or a log built from it)


def test_no_mailbox_without_imap_host():
    assert intake_imap.load_settings({}).mailboxes == ()


@pytest.mark.parametrize("env, message", [
    ({"IMAP_HOST": "h", "IMAP_USER": "u"}, "IMAP_USER or IMAP_PASSWORD is missing"),
    ({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "IMAP_PORT": "imaps"}, "IMAP_PORT"),
    ({"IMAP2_HOST": "h", "IMAP2_USER": "u", "IMAP2_PASSWORD": "p", "IMAP2_CHANNEL": "fax"}, "IMAP2_CHANNEL"),
    ({"INTAKE_SCENARIO": "prod"}, "INTAKE_SCENARIO"),
    ({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "IMAP_SINCE": "25.09.2026"}, "IMAP_SINCE"),
    ({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "IMAP_SINCE": "2026-02-30"}, "IMAP_SINCE"),
    ({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "IMAP_ALLOWED_SENDERS": "nordwind"},
     "IMAP_ALLOWED_SENDERS: 'nordwind'"),
    ({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p", "IMAP_ALLOWED_SENDERS": "@velox.com, a@b"},
     "IMAP_ALLOWED_SENDERS: 'a@b'"),
    ({"IMAP2_HOST": "h", "IMAP2_USER": "u", "IMAP2_PASSWORD": "p", "IMAP2_ALLOWED_SENDERS": "@"},
     "IMAP2_ALLOWED_SENDERS"),
])
def test_invalid_settings_are_rejected(env, message):
    with pytest.raises(intake_imap.ConfigError, match=message):
        intake_imap.load_settings(env)


def test_two_mailboxes_post_with_their_channels(imap, monkeypatch):
    imap["imap.ap.test"] = FakeServer(user="ap@velox.test")
    imap["imap.store.test"] = FakeServer(user="store@velox.test", password="store-pass")
    imap["imap.ap.test"].add(make_email(attachments=(("a.pdf", PDF, "application/pdf"),)))
    imap["imap.store.test"].add(make_email(sender="info@kaffee-und-co.de", body="Rechnung 2026/140"))
    set_env(monkeypatch, IMAP_HOST="imap.ap.test", IMAP_USER="ap@velox.test", IMAP_PASSWORD=IMAP_PASSWORD,
            IMAP2_HOST="imap.store.test", IMAP2_USER="store@velox.test", IMAP2_PASSWORD="store-pass",
            INTAKE_WEBHOOK_URL=URL, INTAKE_SCENARIO="asis")
    webhook = Webhook()
    result, _ = run(intake_imap.load_settings(), webhook)
    assert result.ok and result.messages == 2
    assert [(c["fields"]["channel"], c["fields"]["sender"], c["fields"]["scenario"]) for c in webhook.calls] == [
        ("ap_mailbox", "billing@nordwind-logistics.de", "asis"), ("store_mailbox", "info@kaffee-und-co.de", "asis")]
    assert imap["imap.ap.test"].seen and imap["imap.store.test"].seen


def cli(monkeypatch, webhook: Webhook, *args: str) -> int:
    monkeypatch.setattr(intake_imap, "make_client", webhook.client)
    return intake_imap.main(list(args))


def test_once_exits_0_when_everything_was_posted(server, monkeypatch, capsys):
    server.add(make_email())
    set_env(monkeypatch, IMAP_HOST="imap.velox.test", IMAP_USER="ap@velox.test", IMAP_PASSWORD=IMAP_PASSWORD,
            APP_PASSWORD=WEBHOOK_PASSWORD, INTAKE_WEBHOOK_URL=URL)
    webhook = Webhook()
    assert cli(monkeypatch, webhook, "--once") == 0
    assert "authorization" in webhook.calls[0]["headers"]
    out = capsys.readouterr().out
    assert "[intake] mailbox=ap@velox.test/INBOX uid=101" in out
    assert "[intake] pass done: 1 new message(s), 0 left unseen, 0 mailbox error(s)" in out
    assert IMAP_PASSWORD not in out and WEBHOOK_PASSWORD not in out


def test_once_exits_1_when_a_post_failed(server, monkeypatch, capsys):
    server.add(make_email())
    set_env(monkeypatch, IMAP_HOST="imap.velox.test", IMAP_USER="ap@velox.test", IMAP_PASSWORD=IMAP_PASSWORD)
    assert cli(monkeypatch, Webhook(502), "--once") == 1
    assert server.seen == set()


def test_once_exits_0_when_a_message_was_rejected(server, monkeypatch, capsys):
    uid = server.add(make_email())
    set_env(monkeypatch, IMAP_HOST="imap.velox.test", IMAP_USER="ap@velox.test", IMAP_PASSWORD=IMAP_PASSWORD)
    assert cli(monkeypatch, Webhook(400), "--once") == 0
    assert server.seen == {uid}
    assert "REJECTED" in capsys.readouterr().out


def test_once_exits_1_when_the_login_fails(server, monkeypatch, capsys):
    set_env(monkeypatch, IMAP_HOST="imap.velox.test", IMAP_USER="ap@velox.test", IMAP_PASSWORD="wrong-password")
    assert cli(monkeypatch, Webhook(), "--once") == 1
    out = capsys.readouterr().out
    assert "AUTHENTICATIONFAILED" in out and "wrong-password" not in out


def test_once_exits_1_without_a_mailbox_or_with_a_bad_setting(monkeypatch, capsys):
    assert cli(monkeypatch, Webhook(), "--once") == 1
    assert "no mailbox configured" in capsys.readouterr().out
    set_env(monkeypatch, IMAP_HOST="imap.velox.test")
    assert cli(monkeypatch, Webhook(), "--once") == 1
    assert "configuration error" in capsys.readouterr().out


def test_cli_needs_once_or_a_positive_loop_interval(monkeypatch):
    for args in ([], ["--loop", "0"], ["--loop", "soon"], ["--once", "--loop", "5"]):
        with pytest.raises(SystemExit) as exc:
            cli(monkeypatch, Webhook(), *args)
        assert exc.value.code == 2


def test_loop_runs_a_pass_per_interval_and_survives_errors(server, monkeypatch):
    server.add(make_email())
    naps: list[float] = []
    lines: list[str] = []
    webhook = Webhook(500)  # first pass fails, second pass succeeds
    with webhook.client() as client:
        code = intake_imap.run_loop(settings(), 30, passes=2, sleep=naps.append, client=client, log=lines.append)
    assert code == 0 and naps == [30]
    assert len(webhook.calls) == 2 and server.unseen == []

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    with webhook.client() as client:
        assert intake_imap.run_loop(settings(), 5, sleep=interrupted, client=client, log=lines.append) == 0
    assert lines[-1] == "[intake] stopped"


# --------------------------------------------------------------------------------------------
# End to end: the real FastAPI app (contract C6) through TestClient
# --------------------------------------------------------------------------------------------


def c6_missing() -> Optional[str]:
    """Why the real webhook cannot be used yet (None when it implements contract C6)."""
    params = set(inspect.signature(main.intake_webhook).parameters)
    missing = sorted({"email_body", "message_id"} - params)
    if missing:
        return f"the webhook does not implement contract C6 yet (no {', '.join(missing)} field)"
    if not hasattr(config, "INBOUND_DIR"):
        return "config.INBOUND_DIR (contract C1) is not there yet"
    return None


def test_end_to_end_with_the_real_webhook(session, server, tmp_path, monkeypatch):
    reason = c6_missing()
    if reason:
        pytest.skip(f"{reason}: the lead re-runs this test once app/main.py has it")
    monkeypatch.setattr(config, "INBOUND_DIR", tmp_path / "inbound")
    ubl_uid = server.add(make_email(sender="billing@bright-agency.fr", subject="Invoice INV-2026-0457",
                                    body="", message_id="<e2e-ubl@bright-agency.fr>",
                                    attachments=(("INV-2026-0457.xml", UBL, "application/xml"),)))
    body_uid = server.add(make_email(sender="info@kaffee-und-co.de", subject="Rechnung 2026/140",
                                     body="Guten Tag, anbei unsere Rechnung 2026/140 über 58,31 EUR.",
                                     message_id="<e2e-body@kaffee-und-co.de>"))
    pdf_uid = server.add(make_email(message_id="<e2e-pdf@nordwind-logistics.de>",
                                    attachments=(("invoice.pdf", PDF, "application/pdf"),)))
    lines: list[str] = []
    with TestClient(main.app) as client:
        result = intake_imap.run_once(settings(), client=client, log=lines.append)
        assert result.ok and result.messages == 3, lines
        assert server.seen == {ubl_uid, body_uid, pdf_uid}

        docs = {d.subject: d for d in session.scalars(
            select(InboundDocument).where(InboundDocument.scenario == "tobe", InboundDocument.sample_no == 0))}
        assert set(docs) == {"Invoice INV-2026-0457", "Rechnung 2026/140", "Invoice 1027"}
        decisions = {d.doc_id: d for d in session.scalars(select(GateDecision))}

        e_invoice = docs["Invoice INV-2026-0457"]
        assert e_invoice.content_type == "ubl_xml" and e_invoice.channel == "ap_mailbox"
        assert e_invoice.extraction.model == ubl.UBL_MODEL  # parsed, no model call
        assert decisions[e_invoice.doc_id].outcome == "posted"  # the golden to-be result of sample 3

        body_only = docs["Rechnung 2026/140"]
        assert body_only.content_type == "email_body" and "58,31 EUR" in (body_only.email_body or "")
        assert decisions[body_only.doc_id].outcome == "human_review"  # brief section 17
        assert docs["Invoice 1027"].content_type == "pdf" and docs["Invoice 1027"].doc_id in decisions

        # A second delivery of the same messages (e.g. \Seen was lost) registers nothing new.
        server.seen.clear()
        again: list[str] = []
        result = intake_imap.run_once(settings(), client=client, log=again.append)
        assert result.ok
        assert all("(already registered)" in line for line in again[:3]), again
        session.expire_all()
        count = len(session.scalars(select(InboundDocument).where(InboundDocument.sample_no == 0)).all())
        assert count == 3


def test_end_to_end_odd_messages_reach_the_real_webhook(session, server, tmp_path, monkeypatch):
    reason = c6_missing()
    if reason:
        pytest.skip(f"{reason}: the lead re-runs this test once app/main.py has it")
    monkeypatch.setattr(config, "INBOUND_DIR", tmp_path / "inbound")
    nameless = server.add(with_header(make_email(message_id="<e2e-nameless@x.example>", subject="Rechnung 2026/141",
                                                 body="Rechnung 2026/141 über 12,50 EUR"), "From", b"Kaffee & Co"))
    forwarded = server.add(make_email(sender="anna.schmidt@velox.com", subject="Fwd: Rechnung 2026/140", body=None,
                                      message_id="<e2e-fwd@velox.com>", forwarded=supplier_email()))
    mixed = server.add(make_email(message_id="<e2e-mixed@nordwind-logistics.de>", subject="Invoice 1028",
                                  attachments=(("invoice.pdf", PDF, "application/pdf"),
                                               ("meta.xml", UNKNOWN_ENCODING_XML, "application/xml"))))
    lines: list[str] = []
    with TestClient(main.app) as client:
        result = intake_imap.run_once(settings(), client=client, log=lines.append)
    assert result.ok and (result.messages, result.rejected) == (3, 0), lines
    assert server.seen == {nameless, forwarded, mixed}
    docs = {d.subject: d for d in session.scalars(
        select(InboundDocument).where(InboundDocument.scenario == "tobe", InboundDocument.sample_no == 0))}
    assert set(docs) == {"Rechnung 2026/141", "Fwd: Rechnung 2026/140", "Invoice 1028"}
    assert docs["Rechnung 2026/141"].sender_email == intake_imap.UNKNOWN_SENDER  # accepted by the webhook
    assert "no usable email address: Kaffee & Co" in docs["Rechnung 2026/141"].email_body
    assert KAFFEE_TEXT in docs["Fwd: Rechnung 2026/140"].email_body  # the invoice text is kept for the reviewer
    assert docs["Invoice 1028"].content_type == "pdf"
