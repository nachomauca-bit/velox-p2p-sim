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
    f"{prefix}_{key}" for prefix in ENV_PREFIXES for key in ("HOST", "PORT", "USER", "PASSWORD", "FOLDER", "CHANNEL")]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No intake setting from the developer's environment or .env leaks into a test."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------------------------
# Fake IMAP server
# --------------------------------------------------------------------------------------------


class FakeServer:
    def __init__(self, user: str = "ap@velox.test", password: str = IMAP_PASSWORD, folder: str = "INBOX"):
        self.user, self.password, self.folder = user, password, folder
        self.messages: dict[str, bytes] = {}
        self.seen: set[str] = set()
        self.connections: list[dict[str, Any]] = []
        self.logged_out = 0
        self.fail_store = False

    def add(self, raw: bytes) -> str:
        uid = str(len(self.messages) + 101)  # UIDs are not sequence numbers
        self.messages[uid] = raw
        return uid

    @property
    def unseen(self) -> list[str]:
        return [uid for uid in self.messages if uid not in self.seen]


class FakeIMAP:
    """The part of imaplib.IMAP4_SSL the poller uses; `servers` maps a host name to a FakeServer."""

    servers: dict[str, FakeServer] = {}

    def __init__(self, host: str, port: int = 993, *, ssl_context: Optional[ssl.SSLContext] = None,
                 timeout: Optional[float] = None):
        if host not in self.servers:
            raise ConnectionRefusedError(f"cannot reach {host}:{port}")
        self.server = self.servers[host]
        self.server.connections.append({"port": port, "ssl_context": ssl_context, "timeout": timeout})
        self.logged_in = False

    def login(self, user: str, password: str):
        if (user, password) != (self.server.user, self.server.password):
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.logged_in = True
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: str = "INBOX", readonly: bool = False):
        assert self.logged_in and not readonly
        if mailbox != self.server.folder:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(len(self.server.messages)).encode()]

    def uid(self, command: str, *args: Any):
        if command == "SEARCH":
            assert args[-1] == "UNSEEN"
            return "OK", [" ".join(self.server.unseen).encode()]
        if command == "FETCH":
            uid, what = args
            assert what == "(BODY.PEEK[])"  # PEEK: fetching must not set \Seen
            raw = self.server.messages[uid]
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
