"""Checked mailboxes: owner invitations, and each editor's author notices."""

import io
import smtplib
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from app import config, mail
from app.db import open_db
from app.services import (
    author_for_user,
    author_notices_account,
    create_job,
    editor_for_user,
    invitation_mail_account,
    mail_public,
    notify_author,
    seed_local_accounts,
    set_author_notices,
    set_invitation_mail,
    write_manuscript,
)
from app.web import App
from app.worker import run_once
from tests.test_http import Client, csrf_of, invite_token


def _https(**extra):
    fields = {
        "transport": "https",
        "from_address": "desk@example.com",
        "https_provider": "resend",
        "secret": "re_test_key",
    }
    fields.update(extra)
    return fields


def _smtp(**extra):
    fields = {
        "transport": "smtp",
        "from_address": "desk@example.com",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "desk@example.com",
        "secret": "app password",
    }
    fields.update(extra)
    return fields


class _Smtp:
    def __init__(self, fail=""):
        self.fail = fail
        self.steps = []
        self.message = None

    def ehlo(self, name):
        self.steps.append(("ehlo", name))

    def starttls(self, context=None):
        self.steps.append("starttls")

    def login(self, user, password):
        self.steps.append(("login", user))
        if self.fail == "auth":
            raise smtplib.SMTPAuthenticationError(535, b"secret-password-echo")

    def mail(self, sender):
        self.steps.append(("mail", sender))
        if self.fail == "from":
            raise smtplib.SMTPSenderRefused(550, b"secret-from-echo", sender)

    def rset(self):
        self.steps.append("rset")

    def quit(self):
        self.steps.append("quit")

    def close(self):
        self.steps.append("close")

    def send_message(self, message):
        self.steps.append("send")
        self.message = message
        if self.fail == "data":
            raise smtplib.SMTPDataError(554, b"secret-body-echo")


class MailShapeTests(unittest.TestCase):
    def test_closed_choices_and_a_blocked_port_are_not_saved(self):
        with self.assertRaises(mail.MailError) as blank:
            mail.normalize({"transport": "", "from_address": "a@b.co"})
        self.assertEqual(str(blank.exception), "a mail transport is required")
        with self.assertRaises(mail.MailError) as host:
            mail.normalize(_smtp(smtp_host="https://smtp.example.com"))
        self.assertEqual(str(host.exception), "an SMTP host is required")
        with self.assertRaises(mail.MailError) as port:
            mail.normalize(_smtp(smtp_port=0))
        self.assertEqual(str(port.exception), "an SMTP port is required")
        with self.assertRaises(mail.MailError) as security:
            mail.normalize(_smtp(smtp_security="none"))
        self.assertEqual(str(security.exception), "SMTP security must be STARTTLS or TLS")
        with self.assertRaises(mail.MailError) as provider:
            mail.normalize(_https(https_provider="ses"))
        self.assertEqual(str(provider.exception), "a mail provider is required")
        with self.assertRaises(mail.MailError) as url:
            mail.normalize(_https(https_provider="https://api.example.com/send"))
        self.assertEqual(str(url.exception), "a mail provider is required")
        with self.assertRaises(mail.MailError) as domain:
            mail.normalize(_https(https_provider="mailgun", mailgun_region="us"))
        self.assertEqual(str(domain.exception), "a Mailgun domain is required")
        with self.assertRaises(mail.MailError) as region:
            mail.normalize(
                _https(https_provider="mailgun", mailgun_domain="mg.example.com", mailgun_region="ap")
            )
        self.assertEqual(str(region.exception), "a Mailgun region is required")
        with self.assertRaises(mail.MailError) as key:
            mail.normalize(_https(secret="has a space"))
        self.assertEqual(str(key.exception), "a mail API key is required")
        account = mail.normalize(_smtp())
        self.assertEqual(account["secret"], "app password")
        self.assertEqual(account["https_provider"], "")
        https = mail.normalize(_https())
        self.assertEqual(https["smtp_host"], "")
        self.assertEqual(https["smtp_port"], 0)
        self.assertEqual(mail.clean_origin("javascript:alert(1)"), "")
        self.assertEqual(mail.clean_origin("http://desk.example:8443/ignored"), "http://desk.example:8443")

        blocked = mail.normalize(
            _smtp(smtp_host="127.0.0.1", smtp_port=1, secret="pw")
        )
        with self.assertRaises(mail.MailError) as reached:
            mail.probe(blocked)
        self.assertEqual(str(reached.exception), mail.SMTP_UNREACHABLE)

    def test_smtp_probe_does_not_send_and_hides_the_server_reply(self):
        client = _Smtp()

        def session(host, port, security):
            client.host = (host, port, security)
            return client

        account = mail.normalize(_smtp())
        with patch("app.mail._smtp_session", session):
            mail.probe(account)
        self.assertEqual(client.steps[0], ("ehlo", "sived"))
        self.assertIn("starttls", client.steps)
        self.assertIn(("mail", "desk@example.com"), client.steps)
        self.assertIn("rset", client.steps)
        self.assertNotIn("send", client.steps)

        client.fail = "auth"
        with patch("app.mail._smtp_session", session):
            with self.assertRaises(mail.MailError) as auth:
                mail.probe(account)
        self.assertEqual(str(auth.exception), mail.SMTP_AUTH)
        self.assertNotIn("secret-password-echo", str(auth.exception))

        client.fail = "from"
        with patch("app.mail._smtp_session", session):
            with self.assertRaises(mail.MailError) as sender:
                mail.probe(account)
        self.assertEqual(str(sender.exception), mail.SMTP_FROM)
        self.assertNotIn("secret-from-echo", str(sender.exception))

    def test_https_probe_calls_the_provider_and_does_not_send(self):
        calls = []

        def request(method, url, headers, body):
            calls.append((method, url, headers, body))
            if url == "https://api.resend.com/domains":
                return 200
            if "mailgun" in url:
                return 500
            return 401

        resend = mail.normalize(_https())
        with patch("app.mail._request", request):
            mail.probe(resend)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][1], "https://api.resend.com/domains")
        self.assertEqual(calls[0][2]["Authorization"], "Bearer re_test_key")
        self.assertIsNone(calls[0][3])

        calls.clear()
        postmark = mail.normalize(_https(https_provider="postmark", secret="pm-token"))
        with patch("app.mail._request", request):
            with self.assertRaises(mail.MailError) as denied:
                mail.probe(postmark)
        self.assertEqual(calls[0][1], "https://api.postmarkapp.com/server")
        self.assertEqual(calls[0][2]["X-Postmark-Server-Token"], "pm-token")
        self.assertEqual(str(denied.exception), mail.API_AUTH)
        self.assertNotIn("pm-token", str(denied.exception))

        calls.clear()
        sendgrid = mail.normalize(_https(https_provider="sendgrid", secret="sg-token"))
        with patch("app.mail._request", lambda method, url, headers, body: 200):
            mail.probe(sendgrid)
        gun = mail.normalize(
            _https(
                https_provider="mailgun",
                secret="mg-key",
                mailgun_domain="mg.example.com",
                mailgun_region="eu",
            )
        )
        with patch("app.mail._request", request):
            with self.assertRaises(mail.MailError) as down:
                mail.probe(gun)
        self.assertEqual(str(down.exception), mail.API_UNREACHABLE)
        self.assertEqual(calls[-1][1], "https://api.eu.mailgun.net/v3/domains/mg.example.com")
        self.assertTrue(calls[-1][2]["Authorization"].startswith("Basic "))

        sent = []

        def capture(method, url, headers, body):
            sent.append((method, url, body))
            return 202

        with patch("app.mail._request", capture):
            mail.deliver(sendgrid, "author@example.com", "Hello\nBcc: x", "Body")
        self.assertEqual(sent[0][0], "POST")
        self.assertEqual(sent[0][1], "https://api.sendgrid.com/v3/mail/send")
        self.assertNotIn(b"Hello\nBcc", sent[0][2])
        self.assertIn(b"Hello Bcc: x", sent[0][2])

        client = _Smtp(fail="data")
        with patch("app.mail._smtp_session", lambda host, port, security: client):
            with self.assertRaises(mail.MailError) as refused:
                mail.deliver(mail.normalize(_smtp()), "author@example.com", "Ready", "Body")
        self.assertEqual(str(refused.exception), mail.SEND_REFUSED)
        self.assertNotIn("secret-body-echo", str(refused.exception))
        self.assertEqual(client.message["Subject"], "Ready")


class MailAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-mail-"))
        self.previous = (
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.XAI_MODE,
        )
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEV_LOGIN = True
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        self.conn = open_db(config.DATABASE_PATH)
        self.seeded = seed_local_accounts(self.conn)
        self.owner = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (self.seeded["owner_id"],)
        ).fetchone()
        self.editor = editor_for_user(self.conn, self.owner["id"])

    def tearDown(self):
        self.conn.close()
        (
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.XAI_MODE,
        ) = self.previous

    def test_a_failed_check_is_not_stored_and_a_blank_secret_is_kept(self):
        with self.assertRaises(mail.MailError) as missing:
            set_author_notices(self.conn, self.editor, **_https(secret=""))
        self.assertEqual(str(missing.exception), "a mail API key is required")
        self.assertIsNone(author_notices_account(self.conn, self.editor["organization_id"]))
        with self.assertRaises(mail.MailError):
            set_author_notices(
                self.conn,
                self.editor,
                **_smtp(smtp_host="127.0.0.1", smtp_port=1, secret="pw"),
            )
        self.assertIsNone(author_notices_account(self.conn, self.editor["organization_id"]))

        seen = []

        def probe(account):
            seen.append((account["from_address"], account["secret"]))

        with patch("app.mail.probe", probe):
            set_author_notices(
                self.conn,
                self.editor,
                notice_origin="http://desk.example/extra",
                **_https(),
            )
            set_invitation_mail(self.conn, self.owner, **_smtp(from_address="owner@example.com"))
        notices = author_notices_account(self.conn, self.editor["organization_id"])
        invites = invitation_mail_account(self.conn, self.owner["id"])
        self.assertEqual(notices["transport"], "https")
        self.assertEqual(notices["notice_origin"], "http://desk.example")
        self.assertEqual(invites["transport"], "smtp")
        self.assertEqual(invites["organization_id"], None)
        self.assertEqual(notices["user_id"], None)
        self.assertNotEqual(notices["secret"], invites["secret"])
        public = mail_public(notices)
        self.assertEqual(public["secret"], "saved")
        self.assertNotIn("re_test_key", str(public))

        with patch("app.mail.probe", probe):
            set_author_notices(
                self.conn,
                self.editor,
                notice_origin="not a url",
                **_https(secret=""),
            )
        self.assertEqual(seen[-1][1], "re_test_key")
        kept = author_notices_account(self.conn, self.editor["organization_id"])
        self.assertEqual(kept["secret"], "re_test_key")
        self.assertEqual(kept["notice_origin"], "http://desk.example")

        def refuse(account):
            raise mail.MailError(mail.API_AUTH)

        with patch("app.mail.probe", refuse):
            with self.assertRaises(mail.MailError):
                set_author_notices(
                    self.conn,
                    self.editor,
                    **_https(from_address="new@example.com", secret="next-key"),
                )
        stayed = author_notices_account(self.conn, self.editor["organization_id"])
        self.assertEqual(stayed["from_address"], "desk@example.com")
        self.assertEqual(stayed["secret"], "re_test_key")

    def test_author_notice_uses_the_sign_in_address_and_a_failure_leaves_the_job(self):
        author = author_for_user(self.conn, self.seeded["author_user_id"])
        self.conn.execute(
            "UPDATE users SET contact_email = ? WHERE id = ?",
            ("other@example.com", self.seeded["author_user_id"]),
        )
        self.conn.commit()
        persona = self.conn.execute(
            "SELECT id FROM personas WHERE editor_id = ? AND status = 'active'",
            (self.editor["id"],),
        ).fetchone()
        prose = "The city kept its lights on after the river left. " * 20
        key = f"manuscripts/{author['organization_id']}/{self.editor['id']}/{author['id']}/story.txt"
        write_manuscript(key, prose)
        with patch("app.mail.probe", lambda account: None):
            set_author_notices(
                self.conn,
                self.editor,
                notice_origin="http://desk.example",
                **_https(from_address="editor@example.com"),
            )
        sent = []

        def deliver(account, to, subject, text):
            sent.append((account["from_address"], to, subject, text))

        config.DEMO_MODE = True
        try:
            with patch("app.mail.deliver", deliver):
                job_id = create_job(
                    self.conn,
                    author,
                    persona_id=persona["id"],
                    title="Hello\nBcc: injected",
                    genre="fantasy",
                    audience="adult",
                    comps="",
                    do_not_change="",
                    focus="",
                    manuscript=prose,
                    object_key=key,
                    digest="abc",
                )
                self.assertIsNone(run_once(config.DATABASE_PATH))
        finally:
            config.DEMO_MODE = False
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "editor@example.com")
        self.assertEqual(sent[0][1], "author@example.com")
        self.assertNotEqual(sent[0][1], "other@example.com")
        self.assertEqual(sent[0][2], "Hello Bcc: injected is ready.")
        self.assertIn(f"http://desk.example/jobs/{job_id}", sent[0][3])
        self.assertNotIn("\nBcc:", sent[0][2])
        job = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "ready")

        def explode(account, to, subject, text):
            raise mail.MailError(mail.SEND_REFUSED)

        buffer = io.StringIO()
        with patch("app.mail.deliver", explode):
            with redirect_stderr(buffer):
                notify_author(self.conn, job_id)
        self.assertIn("author notice was not sent", buffer.getvalue())
        self.assertNotIn(mail.SEND_REFUSED, buffer.getvalue())
        job = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "ready")

    def test_an_old_database_gains_the_mail_table(self):
        path = self.tmp / "old.sqlite"
        import sqlite3

        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE users (id text)")
        raw.commit()
        raw.close()
        conn = open_db(path)
        try:
            found = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'mail_accounts'"
            ).fetchone()
            self.assertIsNotNone(found)
        finally:
            conn.close()


class MailHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-mail-http-"))
        self.previous = (
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.XAI_MODE,
        )
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEV_LOGIN = True
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        conn = open_db(config.DATABASE_PATH)
        seed_local_accounts(conn)
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.XAI_MODE,
        ) = self.previous

    def sign_in(self, email: str):
        client = Client(self.port)
        status, page = client.follow("POST", "/dev/login", {"email": email})
        self.assertEqual(status, 200, page[:500])
        return client, page, csrf_of(page)

    def test_settings_and_system_admin_check_mail_before_saving(self):
        owner, _page, _csrf = self.sign_in("owner@example.com")
        status, dashboard = owner.follow("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn("Save author notices", dashboard)
        self.assertNotIn("Save invitation mail", dashboard)
        status, settings = owner.follow("GET", "/settings/notices")
        self.assertIn("Save author notices", settings)
        self.assertIn('name="mail_secret"', settings)
        self.assertIn('name="transport"', settings)
        self.assertNotIn('name="api_url"', settings)
        self.assertNotIn('name="notes"', settings)
        self.assertIn("Author notices", settings)
        status, profile = owner.follow("GET", "/profile")
        self.assertIn("Contact email", profile)
        self.assertNotIn("Save author notices", profile)
        status, admin = owner.follow("GET", "/admin?part=mail")
        self.assertIn("Save invitation mail", admin)
        self.assertIn("Invitation mail", admin)
        self.assertIn('aria-selected="true">Invitation mail', admin)
        self.assertIn('aria-selected="true">SMTP', admin)
        status, invited = owner.follow(
            "POST",
            "/admin/invites/editors",
            {"csrf": csrf_of(admin), "email": "ada@example.com"},
        )
        self.assertIn("/invite/", invited)
        self.assertNotIn("The invitation was emailed", invited)
        self.assertNotIn("was not emailed", invited)

        form = {
            "csrf": csrf_of(settings),
            "transport": "https",
            "from_address": "desk@example.com",
            "https_provider": "resend",
            "mail_secret": "re_live_secret",
            "smtp_port": "587",
        }
        with patch("app.mail.probe", side_effect=mail.MailError(mail.API_AUTH)):
            status, failed = owner.follow("POST", "/settings/author-notices", form)
        self.assertEqual(status, 400)
        self.assertIn(mail.API_AUTH, failed)
        self.assertNotIn("re_live_secret", failed)
        conn = open_db(config.DATABASE_PATH)
        try:
            self.assertIsNone(
                conn.execute("SELECT id FROM mail_accounts").fetchone()
            )
        finally:
            conn.close()

        with patch("app.mail.probe", lambda account: None):
            status, saved = owner.follow("POST", "/settings/author-notices", form)
        self.assertEqual(status, 200, saved[:500])
        self.assertIn("desk@example.com", saved)
        self.assertIn("Password or API key: saved.", saved)
        self.assertNotIn("re_live_secret", saved)
        with patch("app.mail.probe", lambda account: None):
            status, cleared = owner.follow(
                "POST",
                "/settings/author-notices/remove",
                {"csrf": csrf_of(saved)},
            )
        self.assertEqual(status, 200)
        self.assertNotIn("Remove author notices", cleared)

        sent = []

        def deliver(account, to, subject, text):
            sent.append((account["from_address"], to, text))

        with patch("app.mail.probe", lambda account: None):
            status, admin = owner.follow(
                "POST",
                "/admin/invitation-mail",
                {
                    "csrf": csrf_of(admin),
                    "transport": "https",
                    "from_address": "owner-mail@example.com",
                    "https_provider": "postmark",
                    "mail_secret": "pm_owner_secret",
                },
            )
        self.assertEqual(status, 200, admin[:500])
        self.assertIn("owner-mail@example.com", admin)
        self.assertNotIn("pm_owner_secret", admin)

        with patch("app.mail.deliver", deliver):
            status, invited = owner.follow(
                "POST",
                "/admin/invites/editors",
                {"csrf": csrf_of(invited), "email": "bea@example.com"},
            )
        self.assertIn("The invitation was emailed. The link is also here.", invited)
        self.assertIn("/invite/", invited)
        self.assertEqual(sent[-1][0], "owner-mail@example.com")
        self.assertEqual(sent[-1][1], "bea@example.com")
        self.assertIn(f"http://127.0.0.1:{self.port}/invite/", sent[-1][2])
        self.assertNotIn("pm_owner_secret", invited)

        def explode(account, to, subject, text):
            raise mail.MailError("pm_owner_secret leaked")

        with patch("app.mail.deliver", explode):
            status, invited = owner.follow(
                "POST",
                "/admin/invites/admins",
                {"csrf": csrf_of(invited), "email": "admin@example.com"},
            )
        self.assertIn("The invitation was not emailed. The link is here.", invited)
        self.assertIn("/invite/", invited)
        self.assertNotIn("pm_owner_secret", invited)
        self.assertNotIn("leaked", invited)

        assistant_calls = []
        with patch("app.mail.deliver", lambda *args: assistant_calls.append(args)):
            status, assistant = owner.follow(
                "POST",
                "/editor/invites/editors",
                {
                    "csrf": csrf_of(invited),
                    "email": "aide@example.com",
                    "perm_manuscripts": "1",
                },
            )
        self.assertIn("/invite/", assistant)
        self.assertEqual(assistant_calls, [])
        self.assertNotIn("The invitation was emailed", assistant)

        guest = Client(self.port)
        guest.follow("POST", f"/invite/{invite_token(invited)}", {"name": "Ann"})
        admin_client, admin_page, admin_csrf = self.sign_in("admin@example.com")
        self.assertNotIn("Save invitation mail", admin_page)
        self.assertNotIn("Invitation mail", admin_page)
        with patch("app.mail.deliver", deliver):
            status, by_admin = admin_client.follow(
                "POST",
                "/admin/invites/editors",
                {"csrf": admin_csrf, "email": "cy@example.com"},
            )
        self.assertIn("The invitation was emailed. The link is also here.", by_admin)
        self.assertEqual(sent[-1][0], "owner-mail@example.com")
        self.assertEqual(sent[-1][1], "cy@example.com")
