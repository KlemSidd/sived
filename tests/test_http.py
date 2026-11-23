"""Walk a solo editor desk, then an assistant, then removal, over HTTP."""

import html
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("XAI_MODE", "local")

from app import config  # noqa: E402
from app.db import open_db  # noqa: E402
from app.services import create_owner, seed_local_accounts, token_hash  # noqa: E402
from app.web import App  # noqa: E402
from app.worker import run_once  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

MANUSCRIPT = "The city kept its lights on after the river left. "
HARBOR = "The harbor counted every bell. "
_BOUNDARY = "----editting-boundary-7f3a"


def _docx(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t xml:space="preserve">'
        + html.escape(text)
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def _encode_form(form: dict) -> tuple[bytes, str]:
    if any(isinstance(value, tuple) for value in form.values()):
        chunks = []
        for name, value in form.items():
            if isinstance(value, tuple):
                filename, data = value
                if isinstance(data, str):
                    data = data.encode()
                chunks.append(
                    (
                        f"--{_BOUNDARY}\r\n"
                        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                        "Content-Type: application/octet-stream\r\n\r\n"
                    ).encode()
                    + data
                    + b"\r\n"
                )
            else:
                chunks.append(
                    (
                        f"--{_BOUNDARY}\r\n"
                        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                        f"{value}\r\n"
                    ).encode()
                )
        chunks.append(f"--{_BOUNDARY}--\r\n".encode())
        return b"".join(chunks), f"multipart/form-data; boundary={_BOUNDARY}"
    return urlencode(form).encode(), "application/x-www-form-urlencoded"


class Client:
    def __init__(self, port: int):
        self.port = port
        self.sid = ""

    def request(self, method: str, path: str, form: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        body = None
        if self.sid:
            headers["Cookie"] = f"sid={self.sid}"
        if form is not None:
            body, content_type = _encode_form(form)
            headers["Content-Type"] = content_type
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        cookie = resp.getheader("Set-Cookie") or ""
        if "sid=" in cookie:
            self.sid = cookie.split("sid=", 1)[1].split(";", 1)[0]
        return resp.status, resp.getheader("Location"), raw

    def follow(self, method: str, path: str, form: dict | None = None):
        status, loc, body = self.request(method, path, form)
        if status == 303:
            status, _loc, body = self.request("GET", loc)
        return status, body


def csrf_of(page: str) -> str:
    match = re.search(r"name=['\"]csrf['\"] value=['\"]([^'\"]+)", page)
    if match is None:
        raise AssertionError(page[:800])
    return match.group(1)


def invite_token(page: str) -> str:
    match = re.search(r"href='/invite/([^']+)'", page)
    if match is None:
        raise AssertionError(page[:800])
    return match.group(1)


class HttpFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-http-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEV_LOGIN = True
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        conn = open_db(config.DATABASE_PATH)
        create_owner(conn, "owner@example.com")
        conn.close()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def client(self) -> Client:
        return Client(self.port)

    def test_a_response_header_cannot_add_another_header(self):
        with patch(
            "app.web.route",
            return_value=(204, b"", [("Location", "ok\r\nX-Injected: yes")]),
        ):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/login")
            resp = conn.getresponse()
            resp.read()
        self.assertIsNone(resp.getheader("X-Injected"))
        self.assertEqual(resp.getheader("Location"), "okX-Injected: yes")

    def sign_in(self, email: str) -> tuple[Client, str, str]:
        client = self.client()
        status, page = client.follow("POST", "/dev/login", {"email": email})
        self.assertEqual(status, 200, page[:500])
        return client, page, csrf_of(page)

    def test_a_copied_session_id_is_not_a_login_and_an_old_cookie_still_works(self):
        conn = open_db(config.DATABASE_PATH)
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("owner@example.com",),
        ).fetchone()["id"]
        conn.close()
        raw = "11111111-1111-4111-8111-111111111111"
        direct = sqlite3.connect(config.DATABASE_PATH)
        direct.execute(
            """
            INSERT INTO sessions (id, user_id, csrf_token, created_at, expires_at)
            VALUES (?, ?, 'csrf-kept', datetime('now'), datetime('now', '+7 days'))
            """,
            (raw, user_id),
        )
        direct.commit()
        direct.close()

        client = self.client()
        client.sid = raw
        status, page = client.follow("GET", "/")
        self.assertEqual(status, 200, page[:400])
        self.assertIn("Your page", page)

        conn = open_db(config.DATABASE_PATH)
        row = conn.execute(
            "SELECT id, csrf_token FROM sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        self.assertEqual(row["id"], token_hash(raw))
        self.assertNotEqual(row["id"], raw)
        self.assertEqual(row["csrf_token"], "csrf-kept")
        again = open_db(config.DATABASE_PATH)
        self.assertEqual(
            again.execute("SELECT id FROM sessions WHERE user_id = ?", (user_id,)).fetchone()["id"],
            token_hash(raw),
        )
        again.close()
        conn.close()

        copied = self.client()
        copied.sid = token_hash(raw)
        status, loc, _body = copied.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/login")
        status, page = client.follow("GET", "/")
        self.assertEqual(status, 200, page[:400])
        self.assertIn("Your page", page)

    def test_sign_out_deletes_the_session_row(self):
        client, _page, _csrf = self.sign_in("owner@example.com")
        raw = client.sid
        conn = open_db(config.DATABASE_PATH)
        stored = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        conn.close()
        self.assertEqual(stored, token_hash(raw))
        self.assertNotEqual(stored, raw)

        status, page = client.follow("POST", "/logout")
        self.assertEqual(status, 200, page[:400])
        self.assertIn("Sign in", page)
        self.assertEqual(client.sid, "")
        conn = open_db(config.DATABASE_PATH)
        self.assertIsNone(conn.execute("SELECT id FROM sessions WHERE id = ?", (stored,)).fetchone())
        conn.close()

        again = self.client()
        again.sid = raw
        status, loc, _body = again.request("GET", "/")
        self.assertEqual(status, 303)
        self.assertEqual(loc, "/login")

    def test_baked_in_author_desk(self):
        conn = open_db(config.DATABASE_PATH)
        seeded = seed_local_accounts(conn)
        conn.close()
        anon = self.client()
        status, _loc, page = anon.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("owner@example.com", page)
        self.assertNotIn("editor@example.com", page)
        self.assertIn("author@example.com", page)
        self.assertIn(f"/e/{seeded['editor_id']}", page)
        author, page, _csrf = self.sign_in("author@example.com")
        self.assertIn("Your manuscripts", page)
        self.assertIn("Profile", page)
        self.assertNotIn("System Admin", page)
        self.assertNotIn("$", page)
        status, _loc, profile = author.request("GET", "/profile")
        self.assertEqual(status, 200)
        self.assertIn("Sign-in email", profile)
        self.assertIn("Bea", profile)
        self.assertNotIn("System Admin", profile)
        self.assertIn("New manuscript", page)
        status, _loc, submit = author.request("GET", "/submit")
        self.assertEqual(status, 200)
        self.assertIn('type="file"', submit)
        self.assertIn('name="manuscript"', submit)
        self.assertNotIn("<textarea name=\"manuscript\"", submit)
        self.assertIn("turned into text for the editing workflow", submit)
        editor, page, _csrf = self.sign_in("owner@example.com")
        self.assertIn("Your page", page)
        self.assertIn("Active personas", page)
        self.assertIn("Developmental editor", page)
        self.assertNotIn('name="agent_code"', page)
        self.assertNotIn("Agent code", page)
        self.assertIn(f'href="/e/{seeded["editor_id"]}"', page)
        status, _loc, local_agent = editor.request("GET", "/local-agent")
        self.assertEqual(status, 200)
        self.assertIn("Local agent", local_agent)
        self.assertIn("Create token", local_agent)
        self.assertIn("what this process still needs", local_agent)
        status, _loc, public = anon.request("GET", f"/e/{seeded['editor_id']}")
        self.assertEqual(status, 200)
        self.assertIn("Become a customer", public)

    def test_owner_opens_a_practice_on_the_same_login(self):
        owner, page, csrf = self.sign_in("owner@example.com")
        self.assertIn("System Admin", page)
        self.assertIn("Profile", page)
        self.assertIn("Local agent", page)
        self.assertIn("Assistants", page)
        self.assertNotIn("Open my practice", page)
        status, _loc, local_agent = owner.request("GET", "/local-agent")
        self.assertEqual(status, 200)
        self.assertIn("/skills/desk-persona", local_agent)
        status, admin = owner.follow("GET", "/admin")
        self.assertEqual(status, 200, admin[:500])
        self.assertIn("You are the Owner.", admin)
        self.assertIn("Roles: Owner, Editor.", admin)
        self.assertIn("Add an editor", admin)
        self.assertIn('aria-selected="true">Editors', admin)
        self.assertNotIn("Add an admin", admin)
        self.assertNotIn(">Suspend</button>", admin)
        status, admins_tab = owner.follow("GET", "/admin?part=admins")
        self.assertIn("Add an admin", admins_tab)
        self.assertIn('aria-selected="true">Admins', admins_tab)
        status, profile = owner.follow(
            "POST",
            "/profile",
            {
                "csrf": csrf,
                "name": "Ada Owner",
                "contact_email": "ada.owner@example.com",
                "phone": "555-0100",
            },
        )
        self.assertIn("Ada Owner", profile)
        self.assertIn("ada.owner@example.com", profile)
        self.assertIn("555-0100", profile)
        self.assertIn("Sign-in email", profile)
        status, page = owner.follow(
            "POST",
            "/admin/practice",
            {"csrf": csrf, "name": "North Desk"},
        )
        self.assertEqual(status, 200, page[:500])
        self.assertIn("North Desk", page)
        self.assertIn("Assistants", page)
        self.assertIn("Active personas", page)
        self.assertNotIn('name="agent_code"', page)
        self.assertNotIn("Agent code", page)
        status, _loc, personas = owner.request("GET", "/personas")
        persona_id = re.search(r'href="/personas/([0-9a-f-]{36})"', personas).group(1)
        status, page = owner.follow(
            "POST",
            f"/personas/{persona_id}",
            {"csrf": csrf_of(personas), "name": "North editor", "max_words": "180000"},
        )
        self.assertIn(">Publish</button>", page)
        refused = io.BytesIO()
        with zipfile.ZipFile(refused, "w") as archive:
            archive.writestr(
                "manifest.json",
                '{"harness": "python", "entry": "agent.py"}\n',
            )
            archive.writestr(
                "agent.py",
                "def run(manuscript, form, skill):\n    return {'letter': 'no'}\n",
            )
        status, denied = owner.follow(
            "POST",
            f"/personas/{persona_id}/package",
            {"csrf": csrf_of(page), "package": ("python.zip", refused.getvalue())},
        )
        self.assertEqual(status, 400, denied[:500])
        self.assertIn("a persona is a workflow.json file", denied)
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "manifest.json",
                '{"harness": "workflow", "entry": "workflow.json"}\n',
            )
            archive.writestr(
                "workflow.json",
                '{"title": "North", "steps": [{"name": "house", "skill": "house-style", '
                '"ask": "Keep the names.", "produces": "letter"}]}\n',
            )
            archive.writestr("skills/house-style/SKILL.md", "# House\nKeep the names.\n")
        status, page = owner.follow(
            "POST",
            f"/personas/{persona_id}/package",
            {"csrf": csrf_of(page), "package": ("north.zip", package.getvalue())},
        )
        self.assertEqual(status, 200, page[:500])
        self.assertIn("North editor", page)
        self.assertIn("skills/house-style/SKILL.md", page)
        self.assertIn("workflow.json", page)
        self.assertNotIn("agent.py", page)
        self.assertNotIn('name="agent_code"', page)
        self.assertIn("System Admin", page)
        self.assertNotIn(">Platform</a>", page)
        status, page = owner.follow("GET", "/")
        self.assertIn("North Desk", page)
        self.assertIn("Active personas", page)
        status, admin = owner.follow("GET", "/admin")
        self.assertIn("Add an editor", admin)
        status, _loc, login = owner.request("GET", "/login")
        self.assertIn("owner@example.com (owner)", login)

    def test_admin_cannot_change_the_owner(self):
        owner, _page, owner_csrf = self.sign_in("owner@example.com")
        status, page = owner.follow(
            "POST",
            "/admin/invites/admins",
            {"csrf": owner_csrf, "email": "admin@example.com"},
        )
        self.assertEqual(status, 200, page[:500])
        guest = self.client()
        guest.follow("POST", f"/invite/{invite_token(page)}", {"name": "Ann"})
        admin, page, admin_csrf = self.sign_in("admin@example.com")
        self.assertIn("You are an Admin.", page)
        self.assertIn("Roles: Admin.", page)
        self.assertIn("System Admin", page)
        self.assertNotIn("Add an admin", page)
        self.assertNotIn(">Suspend</button>", page)
        status, invited = admin.follow(
            "POST",
            "/admin/invites/editors",
            {"csrf": admin_csrf, "email": "ada@example.com"},
        )
        self.assertIn("/invite/", invited)
        status, denied = _post_status(
            admin,
            "/admin/invites/admins",
            {"csrf": admin_csrf, "email": "other@example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("only the owner can add an admin", denied)
        conn = open_db(config.DATABASE_PATH)
        owner_editor = conn.execute(
            """
            SELECT e.id FROM editors e
            JOIN users u ON u.id = e.user_id
            WHERE u.email = 'owner@example.com'
            """
        ).fetchone()["id"]
        admin_id = conn.execute(
            "SELECT id FROM users WHERE email = 'admin@example.com'"
        ).fetchone()["id"]
        conn.close()
        status, denied = _post_status(
            admin,
            f"/admin/editors/{owner_editor}/suspend",
            {"csrf": admin_csrf, "reason": "no"},
        )
        self.assertEqual(status, 400)
        self.assertIn("the owner cannot be changed", denied)
        status, page = owner.follow(
            "POST",
            f"/admin/admins/{admin_id}/remove",
            {"csrf": owner_csrf},
        )
        self.assertIn("You are the Owner.", page)
        self.assertNotIn("admin@example.com", page)
        removed, home, _csrf = self.sign_in("admin@example.com")
        self.assertIn("This login has no desk.", home)
        self.assertNotIn("System Admin", home)
        status, denied = _post_status(
            removed,
            "/admin",
            {"csrf": _csrf},
        )
        self.assertEqual(status, 400)
        self.assertIn("that is not assigned to this login", denied)

    def test_demo_pay_skips_the_workflow(self):
        previous_demo = config.DEMO_MODE
        previous_login = config.DEV_LOGIN
        config.DEMO_MODE = True
        config.DEV_LOGIN = False
        try:
            anon = self.client()
            status, _loc, page = anon.request("GET", "/login")
            self.assertEqual(status, 200)
            self.assertIn("Demo sign-in lists every account.", page)
            self.assertIn("does not run the editing workflow", page)

            owner, _page, owner_csrf = self.sign_in("owner@example.com")
            status, page = owner.follow(
                "POST",
                "/owner/invites/editors",
                {"csrf": owner_csrf, "email": "ada@example.com"},
            )
            guest = self.client()
            guest.follow("POST", f"/invite/{invite_token(page)}", {"name": "Ada"})

            ada, page, ada_csrf = self.sign_in("ada@example.com")
            status, _loc, personas = ada.request("GET", "/personas")
            persona_id = re.search(r'href="/personas/([0-9a-f-]{36})"', personas).group(1)
            ada.follow(
                "POST",
                f"/editor/personas/{persona_id}/publish",
                {"csrf": ada_csrf},
            )
            ada, page, _ada_csrf = self.sign_in("ada@example.com")
            editor_id = re.search(r"href=\"/e/([^\"]+)\"", page).group(1)

            bea = self.client()
            status, page = bea.follow(
                "POST",
                f"/e/{editor_id}/join",
                {"name": "Bea", "email": "bea@example.com"},
            )
            self.assertIn("does not run the editing workflow", page)
            self.assertIn("Your manuscripts", page)
            status, _loc, submit = bea.request("GET", "/submit")
            self.assertIn('type="file"', submit)
            bea_csrf = csrf_of(page)
            prose = "The city kept its lights on. " * 40
            status, rejected = bea.follow(
                "POST",
                "/jobs",
                {
                    "csrf": bea_csrf,
                    "persona_id": persona_id,
                    "title": "After the river",
                    "genre": "fantasy",
                    "audience": "adult",
                    "manuscript": ("notes.exe", b"not a manuscript"),
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("upload a manuscript as", rejected)
            status, page = bea.follow(
                "POST",
                "/jobs",
                {
                    "csrf": bea_csrf,
                    "persona_id": persona_id,
                    "title": "After the river",
                    "genre": "fantasy",
                    "audience": "adult",
                    "manuscript": ("after.docx", _docx(prose)),
                },
            )
            self.assertIn("ready", page)
            self.assertIn("240 words", page)
            self.assertNotIn("$", page)
            self.assertNotIn("/pay", page)
            self.assertIn("The editing workflow did not run.", page)
            self.assertNotIn("## Promise and genre fit", page)
            self.assertIsNone(run_once(config.DATABASE_PATH))
        finally:
            config.DEMO_MODE = previous_demo
            config.DEV_LOGIN = previous_login

    def test_solo_then_assistant_then_removed(self):
        anon = self.client()
        status, _loc, body = anon.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")
        status, loc, _body = anon.request("GET", "/")
        self.assertEqual((status, loc), (303, "/login"))

        owner, _page, owner_csrf = self.sign_in("owner@example.com")
        status, page = owner.follow(
            "POST",
            "/owner/invites/editors",
            {"csrf": owner_csrf, "email": "ada@example.com"},
        )
        self.assertEqual(status, 200, page[:500])
        ada_token = invite_token(page)

        guest = self.client()
        status, page = guest.follow("POST", f"/invite/{ada_token}", {"name": "Ada"})
        self.assertIn("ada@example.com", page)

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertNotIn("Invite an editor", page)
        self.assertNotIn("Upgrade", page)
        self.assertNotIn("<th>Editor</th>", page)
        self.assertNotIn("Firm", page)
        self.assertNotIn("Indie", page)
        self.assertIn("Your page", page)
        status, _loc, assistants = ada.request("GET", "/assistants")
        self.assertIn("<h2>Assistants</h2>", assistants)
        self.assertIn("They sign in to this desk.", assistants)
        status, _loc, personas = ada.request("GET", "/personas")
        persona_id = re.search(r'href="/personas/([0-9a-f-]{36})"', personas).group(1)
        status, page = ada.follow(
            "POST",
            f"/editor/personas/{persona_id}/publish",
            {"csrf": ada_csrf},
        )
        self.assertIn("Status active", page)
        self.assertNotIn("Author price", page)
        status, _loc, pipeline = ada.request("GET", "/pipeline")
        self.assertIn("Author asked for a human editor", pipeline)
        self.assertNotIn("Save trigger", pipeline)
        editor_id = re.search(r"href=\"/e/([^\"]+)\"", pipeline).group(1)
        bea = self.client()
        status, _loc, public = bea.request("GET", f"/e/{editor_id}")
        self.assertEqual(status, 200)
        self.assertIn("Become a customer", public)
        self.assertIn("Patreon", public)
        self.assertNotIn("$", public)
        status, page = bea.follow(
            "POST",
            f"/e/{editor_id}/join",
            {"name": "Bea", "email": "bea@example.com"},
        )
        self.assertIn("Your manuscripts", page)
        bea_csrf = csrf_of(page)
        self.assertNotIn("$", page)
        status, page = bea.follow(
            "POST",
            "/jobs",
            {
                "csrf": bea_csrf,
                "persona_id": persona_id,
                "title": "After the river",
                "genre": "fantasy",
                "audience": "adult",
                "comps": "The City We Became",
                "do_not_change": "The river does not return.",
                "focus": "",
                "manuscript": ("after.txt", (MANUSCRIPT * 40).encode()),
            },
        )
        self.assertIn("queued", page)
        self.assertNotIn("$", page)
        self.assertNotIn("/pay", page)
        self.assertIn("Ask this editor to work on it", page)
        job_id = re.search(r"/jobs/([0-9a-f-]{36})", page).group(1)

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertIn("<h1>After the river</h1>", page)
        status, _loc, pipeline = ada.request("GET", "/pipeline")
        self.assertIn("No one has asked for a human editor.", pipeline)
        self.assertNotIn("After the river", pipeline)
        status, _loc, fantasy = ada.request("GET", "/pipeline?genre=fantasy")
        self.assertIn("After the river", fantasy)
        self.assertIn("The City We Became", fantasy)

        self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        status, _loc, page = bea.request("GET", f"/jobs/{job_id}")
        self.assertIn("ready", page)
        self.assertIn("## Promise and genre fit", page)
        self.assertIn("## Plot and structure", page)
        self.assertIn("## Character", page)
        self.assertIn("## Stakes and pacing", page)
        self.assertNotIn(MANUSCRIPT[:80], page)
        self.assertIn("LOCKED: The river does not return.", page)
        self.assertIn("readback", page)
        status, _loc, quiet = ada.request("GET", "/pipeline")
        self.assertNotIn("After the river", quiet)

        status, page = bea.follow(
            "POST",
            f"/jobs/{job_id}/thread",
            {"csrf": bea_csrf, "body": "Can you edit the river by hand?"},
        )
        status, page = bea.follow("POST", f"/jobs/{job_id}/human-request", {"csrf": csrf_of(page)})
        self.assertIn("Can you edit the river by hand?", page)

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertIn("1 asked for a human editor", page)
        self.assertNotIn("No one has asked for a human editor.", page)
        status, _loc, pipeline = ada.request("GET", "/pipeline")
        self.assertIn("After the river", pipeline)
        self.assertIn("bea@example.com", pipeline)
        self.assertNotIn("Save trigger", pipeline)
        self.assertNotIn("reviewing", pipeline)
        status, _loc, costs = ada.request("GET", "/costs")
        self.assertEqual(status, 200)
        cost_line = re.search(r"Model cost (\$\d+(?:\.\d{2})?)", costs)
        self.assertIsNotNone(cost_line, costs)
        self.assertGreater(_shown_cents(cost_line.group(1)), 0)
        self.assertNotIn("Record payout", costs)

        self.assertNotIn("<th>Editor</th>", costs)
        status, page = ada.follow(
            "POST",
            "/editor/invites/editors",
            {
                "csrf": ada_csrf,
                "email": "cy@example.com",
                "perm_manuscripts": "1",
                "perm_pipeline": "1",
            },
        )
        cy_token = invite_token(page)
        guest.follow("POST", f"/invite/{cy_token}", {"name": "Cy"})

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertIn("After the river", page)
        status, _loc, assistants = ada.request("GET", "/assistants")
        self.assertIn("cy@example.com", assistants)
        self.assertIn("<h2>Assistants</h2>", assistants)
        self.assertNotIn("<th>Editor</th>", assistants)
        cy_editor_id = re.search(r"action='/editor/assistants/([^']+)'", assistants).group(1)
        status, _loc, ada_job = ada.request("GET", f"/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertIn("Can you edit the river by hand?", ada_job)
        status, ada_reply = ada.follow(
            "POST",
            f"/jobs/{job_id}/thread",
            {"csrf": ada_csrf, "body": "Yes. We can discuss that directly."},
        )
        self.assertEqual(status, 200, ada_reply[:400])
        self.assertIn("Yes. We can discuss that directly.", ada_reply)

        cy, page, cy_csrf = self.sign_in("cy@example.com")
        self.assertIn("<h1>After the river</h1>", page)
        self.assertIn("Work in motion", page)
        self.assertNotIn("href='/settings'", page)
        self.assertNotIn("Save Patreon", page)
        self.assertNotIn("You are signed in on this editor", page)
        self.assertIn(f'href="/e/{editor_id}"', page)
        self.assertNotIn(f'href="/e/{cy_editor_id}"', page)
        self.assertIn("After the river", page)
        self.assertIn("Search manuscripts", page)
        self.assertNotIn("Fantasy shelf", page)
        self.assertNotIn("Publish price", page)
        self.assertNotIn("Record payout", page)
        self.assertNotIn("Model cost", page)
        self.assertNotIn("$", page)
        self.assertNotIn("/editor/invites/editors", page)
        self.assertNotIn("<th>Editor</th>", page)
        status, _loc, visible = cy.request("GET", f"/jobs/{job_id}")
        self.assertEqual(status, 200)
        self.assertIn("Messages are not assigned to this login.", visible)
        self.assertNotIn("Model cost", visible)
        self.assertNotIn("$", visible)
        status, denied = _post_status(
            cy,
            f"/jobs/{job_id}/thread",
            {"csrf": cy_csrf, "body": "I should not write yet."},
        )
        self.assertEqual(status, 400)
        self.assertIn("that is not assigned to this login", denied)
        status, denied = _post_status(
            cy,
            "/editor/invites/editors",
            {"csrf": cy_csrf, "email": "nope@example.com", "perm_manuscripts": "1"},
        )
        self.assertEqual(status, 400)
        self.assertIn("you cannot add assistants", denied)
        status, denied = _post_status(
            cy,
            f"/editor/personas/{persona_id}/publish",
            {"csrf": cy_csrf},
        )
        self.assertEqual(status, 400)
        self.assertIn("that is not assigned to this login", denied)
        status, _loc, denied = cy.request("GET", "/costs")
        self.assertEqual(status, 400)
        self.assertIn("that is not assigned to this login", denied)
        stranger = self.client()
        status, _loc, public = stranger.request("GET", f"/e/{cy_editor_id}")
        self.assertEqual(status, 200)
        self.assertIn("This editor is not taking customers.", public)
        status, denied = _post_status(
            stranger,
            f"/e/{cy_editor_id}/join",
            {"name": "Dee", "email": "dee@example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("this editor is not taking customers", denied)

        status, _loc, mystery = cy.request("GET", "/pipeline?genre=mystery")
        self.assertNotIn("The bell harbor", mystery)
        self.assertNotIn("After the river", mystery)

        dee = self.client()
        status, page = dee.follow(
            "POST",
            f"/e/{editor_id}/join",
            {"name": "Dee", "email": "dee@example.com"},
        )
        self.assertIn("Your manuscripts", page)
        dee_csrf = csrf_of(page)
        status, page = dee.follow(
            "POST",
            "/jobs",
            {
                "csrf": dee_csrf,
                "persona_id": persona_id,
                "title": "The bell harbor",
                "genre": "mystery",
                "audience": "adult",
                "comps": "",
                "do_not_change": "",
                "focus": "",
                "manuscript": ("harbor.txt", (HARBOR * 40).encode()),
            },
        )
        self.assertIn("queued", page)
        self.assertNotIn("$", page)
        dee_job = re.search(r"/jobs/([0-9a-f-]{36})", page).group(1)
        self.assertEqual(run_once(config.DATABASE_PATH), dee_job)

        cy, page, cy_csrf = self.sign_in("cy@example.com")
        self.assertIn("The bell harbor", page)
        self.assertIn("After the river", page)
        status, _loc, mystery = cy.request("GET", "/pipeline?genre=mystery")
        self.assertIn("The bell harbor", mystery)
        self.assertNotIn("After the river", mystery)
        status, denied = _post_status(
            cy,
            f"/jobs/{dee_job}/thread",
            {"csrf": cy_csrf, "body": "I can talk about a human pass."},
        )
        self.assertEqual(status, 400)
        self.assertIn("that is not assigned to this login", denied)

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        status, page = ada.follow(
            "POST",
            f"/editor/assistants/{cy_editor_id}",
            {
                "csrf": ada_csrf,
                "perm_manuscripts": "1",
                "perm_pipeline": "1",
                "perm_messages": "1",
            },
        )
        self.assertIn("cy@example.com", page)
        cy, page, cy_csrf = self.sign_in("cy@example.com")
        status, page = cy.follow(
            "POST",
            f"/jobs/{dee_job}/thread",
            {"csrf": cy_csrf, "body": "I can talk about a human pass."},
        )
        self.assertIn("I can talk about a human pass.", page)

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertIn("After the river", page)
        self.assertIn("The bell harbor", page)
        self.assertNotIn("Fantasy shelf", page)
        self.assertNotIn("Mystery shelf", page)
        self.assertNotIn("<th>Editor</th>", page)
        status, ada_reply = ada.follow(
            "POST",
            f"/jobs/{dee_job}/thread",
            {"csrf": ada_csrf, "body": "The practice can write on this thread."},
        )
        self.assertEqual(status, 200, ada_reply[:400])
        self.assertIn("The practice can write on this thread.", ada_reply)
        status, _loc, page = ada.request("GET", "/")
        self.assertNotIn("does not charge", page)
        self.assertNotIn("Save Patreon", page)
        self.assertNotIn("Authors are your customers", page)
        self.assertIn("href='/settings'", page)
        self.assertNotIn("owes this practice", page)

        check = open_db(config.DATABASE_PATH)
        self.assertIsNone(check.execute("SELECT id FROM payouts").fetchone())
        self.assertEqual(check.execute("SELECT COUNT(*) AS n FROM editor_payables").fetchone()["n"], 0)
        self.assertEqual(check.execute("SELECT COUNT(*) AS n FROM charges").fetchone()["n"], 0)
        spent = check.execute(
            "SELECT COALESCE(SUM(cost_usd_micros), 0) AS n FROM token_costs"
        ).fetchone()["n"]
        self.assertGreater(spent, 0)
        check.close()

        ada, page, ada_csrf = self.sign_in("ada@example.com")
        self.assertNotIn("<th>Editor</th>", page)
        self.assertNotIn("does not charge", page)
        self.assertNotIn("Save Patreon", page)
        status, _loc, assistants = ada.request("GET", "/assistants")
        remove_id = re.search(r"action='/editor/editors/([^']+)/remove'", assistants).group(1)
        status, page = ada.follow(
            "POST",
            f"/editor/editors/{remove_id}/remove",
            {"csrf": ada_csrf, "email": ""},
        )
        self.assertEqual(status, 200, page[:500])
        self.assertNotIn("<th>Editor</th>", page)
        self.assertNotIn("cy@example.com", page)
        self.assertIn("<h2>Assistants</h2>", page)
        status, _loc, manuscripts = ada.request("GET", "/manuscripts")
        self.assertIn("After the river", manuscripts)
        self.assertIn("The bell harbor", manuscripts)
        self.assertNotIn("Invite an editor", page)
        status, page = ada.follow(
            "POST",
            f"/jobs/{dee_job}/thread",
            {"csrf": csrf_of(page), "body": "The practice is handling this now."},
        )
        self.assertIn("The practice is handling this now.", page)

        left = self.client()
        status, page = left.follow("POST", "/dev/login", {"email": "cy@example.com"})
        self.assertEqual(status, 400)
        self.assertIn("you are no longer an editor on this practice", page)
        dee, page, _dee_csrf = self.sign_in("dee@example.com")
        self.assertIn("The bell harbor", page)

    def test_settings_hold_the_practice_configuration(self):
        owner, dashboard, _csrf = self.sign_in("owner@example.com")
        self.assertNotIn("Save Patreon", dashboard)
        self.assertNotIn("Save appearance", dashboard)
        self.assertNotIn("Authors are your customers", dashboard)
        self.assertIn("href='/settings'", dashboard)
        status, _loc, settings = owner.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn("<h1>Settings</h1>", settings)
        self.assertIn('role="tablist"', settings)
        self.assertIn('aria-selected="true">Practice', settings)
        self.assertIn("Save practice name", settings)
        self.assertNotIn("Save Patreon", settings)
        self.assertNotIn("Save appearance", settings)
        self.assertNotIn('name="default_models"', settings)
        self.assertNotIn('name="mail_secret"', settings)
        status, _loc, inference = owner.request("GET", "/settings/inference")
        self.assertIn("<h1>Settings</h1>", inference)
        self.assertIn('aria-selected="true">Inference', inference)
        self.assertIn("Save provider", inference)
        status, _loc, defaults = owner.request("GET", "/settings/defaults")
        self.assertIn('aria-selected="true">Default models', defaults)
        self.assertIn("Add model", defaults)
        self.assertIn("No default models.", defaults)
        self.assertIn("No provider is saved, so there is no list of models to choose from.", defaults)
        self.assertNotIn("<textarea", defaults)
        status, _loc, patreon_page = owner.request("GET", "/settings/patreon")
        self.assertIn("Save Patreon", patreon_page)
        self.assertIn("Not connected", patreon_page)
        status, _loc, appearance = owner.request("GET", "/settings/appearance")
        self.assertIn("Save appearance", appearance)
        self._save_inference_provider(owner, inference)
        editor_id = re.search(r'href="/e/([^"]+)"', settings).group(1)
        status, saved = owner.follow(
            "POST",
            "/settings/appearance",
            {
                "csrf": csrf_of(settings),
                "profile": "sea",
                "mode": "dark",
                "accent": "#112233",
                "paper": "#abcdef",
                "ink": "#010101",
            },
        )
        self.assertEqual(status, 200, saved[:400])
        self.assertIn("--paper: #101820", saved)
        self.assertIn("color-scheme: dark", saved)
        self.assertIn('value="sea" selected', saved)
        self.assertIn('value="dark" selected', saved)
        status, _loc, desk = owner.request("GET", "/")
        self.assertIn("--paper: #101820", desk)
        self.assertNotIn("Save Patreon", desk)
        status, _loc, public = self.client().request("GET", f"/e/{editor_id}")
        self.assertIn("--paper: #101820", public)
        self.assertIn("Become a customer", public)
        status, _loc, login = self.client().request("GET", "/login")
        self.assertIn("--paper: #f6f1e7", login)
        self.assertNotIn("--paper: #101820", login)
        status, bad = owner.follow(
            "POST",
            "/settings/appearance",
            {
                "csrf": csrf_of(saved),
                "profile": "custom",
                "mode": "dark",
                "accent": "blue",
                "paper": "#010203",
                "ink": "#fefefe",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("a accent color is required", bad)
        status, custom = owner.follow(
            "POST",
            "/settings/appearance",
            {
                "csrf": csrf_of(saved),
                "profile": "custom",
                "mode": "dark",
                "accent": "#112233",
                "paper": "#010203",
                "ink": "#fefefe",
            },
        )
        self.assertEqual(status, 200, custom[:400])
        self.assertIn("--paper: #010203", custom)
        self.assertIn("color-scheme: dark", custom)
        guest = self.client()
        status, manuscripts = guest.follow(
            "POST",
            f"/e/{editor_id}/join",
            {"name": "Bea", "email": "bea@example.com"},
        )
        self.assertEqual(status, 200, manuscripts[:400])
        self.assertIn("--paper: #010203", manuscripts)
        self.assertIn("Your manuscripts", manuscripts)
        self.assertNotIn("$", manuscripts)
        status, named = owner.follow(
            "POST",
            "/settings/practice",
            {"csrf": csrf_of(custom), "name": "North Desk"},
        )
        self.assertIn('value="North Desk"', named)
        status, _loc, home = owner.request("GET", "/")
        self.assertIn("<h1>North Desk</h1>", home)
        self.assertNotIn("Save appearance", home)
        status, invite = owner.follow(
            "POST",
            "/editor/invites/editors",
            {"csrf": csrf_of(named), "email": "aide@example.com", "perm_manuscripts": "1"},
        )
        token = invite_token(invite)
        self.client().follow("POST", f"/invite/{token}", {"name": "Aide"})
        aide, page, _aide_csrf = self.sign_in("aide@example.com")
        self.assertIn("--paper: #010203", page)
        self.assertNotIn("href='/settings'", page)
        self.assertNotIn("Save Patreon", page)
        status, _loc, denied = aide.request("GET", "/settings")
        self.assertEqual(status, 400)
        self.assertIn("only the editor can change settings", denied)

    def _save_inference_provider(self, owner, settings):
        from unittest.mock import patch

        models = [{"id": f"model-{index}"} for index in range(41)]

        def fake_get(url, headers, timeout):
            self.assertEqual(headers.get("authorization"), "Bearer page-secret")
            self.assertNotIn("page-secret", url)
            return {"data": models}

        with patch("app.llm._get", fake_get):
            status, saved = owner.follow(
                "POST",
                "/settings/inference",
                {
                    "csrf": csrf_of(settings),
                    "label": "Desk xAI",
                    "provider": "xai",
                    "base_url": "https://us.api.x.ai/v1",
                    "inference_secret": "page-secret",
                },
            )
        self.assertEqual(status, 200, saved[:500])
        self.assertIn("Desk xAI", saved)
        self.assertIn("model-0", saved)
        self.assertNotIn("model-40", saved)
        self.assertIn("and 1 more", saved)
        self.assertIn("Browse all 41", saved)
        self.assertIn("Check again", saved)
        self.assertIn("https://api.x.ai/v1", saved)
        self.assertNotIn("us.api.x.ai", saved)
        self.assertIn("API key: saved", saved)
        self.assertNotIn("page-secret", saved)
        status, _loc, personas = owner.request("GET", "/personas")
        self.assertEqual(status, 200)
        self.assertIn("model-0", personas)
        self.assertNotIn("model-40", personas)
        self.assertNotIn("page-secret", personas)
        self.assertIn("Every model these providers list is on Default models.", personas)
        status, _loc, catalog = owner.request("GET", "/settings/defaults")
        self.assertIn("lists 41 models", catalog)
        self.assertIn("Showing 30 of 41", catalog)
        self.assertIn(">model-0<", catalog)
        self.assertNotIn(">model-40<", catalog)
        self.assertIn("Check again", catalog)
        self.assertNotIn("page-secret", catalog)
        status, _loc, found = owner.request("GET", "/settings/defaults?catalog=Desk%20xAI&q=model-40")
        self.assertIn(">model-40<", found)
        self.assertIn(">Add</button>", found)
        self.assertNotIn("page-secret", found)
        status, added = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {
                "csrf": csrf_of(found),
                "add": "1",
                "model": "model-40",
                "catalog": "Desk xAI",
                "q": "model-40",
            },
        )
        self.assertEqual(status, 200, added[:500])
        self.assertIn("In the default list", added)
        self.assertIn(">model-40<", added)
        status, removed = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {
                "csrf": csrf_of(added),
                "remove": "1",
                "model": "model-40",
                "catalog": "Desk xAI",
                "q": "model-40",
            },
        )
        self.assertNotIn("In the default list", removed)
        self.assertIn(">model-40<", removed)
        with patch("app.llm._get", side_effect=ValueError("that provider could not be reached")):
            status, _loc, refused_check = owner.request(
                "POST",
                "/settings/inference/refresh",
                {"csrf": csrf_of(removed), "label": "Desk xAI", "back": "defaults", "catalog": "Desk xAI"},
            )
        self.assertEqual(status, 400)
        self.assertIn("that provider could not be reached", refused_check)
        self.assertIn(">model-0<", refused_check)
        self.assertNotIn("page-secret", refused_check)
        with patch("app.llm._get", return_value={"data": [{"id": "fresh-model"}]}):
            status, refreshed = owner.follow(
                "POST",
                "/settings/inference/refresh",
                {
                    "csrf": csrf_of(refused_check),
                    "label": "Desk xAI",
                    "back": "defaults",
                    "catalog": "Desk xAI",
                },
            )
        self.assertEqual(status, 200, refreshed[:500])
        self.assertIn("Model list updated.", refreshed)
        self.assertIn(">fresh-model<", refreshed)
        self.assertIn("lists 1 model", refreshed)
        self.assertNotIn(">model-0<", refreshed)
        self.assertNotIn("page-secret", refreshed)
        with patch("app.llm._get", return_value={"data": models}):
            status, kept = owner.follow(
                "POST",
                "/settings/inference",
                {
                    "csrf": csrf_of(saved),
                    "label": "Desk xAI",
                    "provider": "xai",
                    "base_url": "",
                    "inference_secret": "",
                },
            )
        self.assertEqual(status, 200, kept[:500])
        self.assertNotIn("page-secret", kept)
        conn = open_db(config.DATABASE_PATH)
        stored = conn.execute("SELECT secret FROM inference_providers").fetchone()
        conn.close()
        self.assertEqual(stored["secret"], "page-secret")
        priced = {
            "data": [
                {
                    "id": "cheap-model",
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                }
            ]
        }
        with patch("app.llm._get", return_value=priced):
            status, priced_page = owner.follow(
                "POST",
                "/settings/inference",
                {
                    "csrf": csrf_of(kept),
                    "label": "Priced",
                    "provider": "openrouter",
                    "base_url": "",
                    "inference_secret": "priced-secret",
                },
            )
        self.assertEqual(status, 200, priced_page[:500])
        self.assertIn("($1, $2)", priced_page)
        self.assertNotIn("priced-secret", priced_page)
        self.assertNotIn("page-secret", priced_page)
        status, _loc, priced_models = owner.request("GET", "/settings/defaults?catalog=Priced")
        self.assertIn('aria-selected="true">Priced', priced_models)
        self.assertIn(">cheap-model<", priced_models)
        self.assertIn("($1, $2)", priced_models)
        self.assertIn("lists 1 model", priced_models)
        self.assertNotIn("priced-secret", priced_models)
        status, dropped = owner.follow(
            "POST",
            "/settings/inference/remove",
            {"csrf": csrf_of(priced_page), "label": "Priced"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Priced", dropped)
        self.assertNotIn("priced-secret", dropped)
        status, with_a = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(dropped), "add": "1", "model": "fallback-a"},
        )
        self.assertEqual(status, 200, with_a[:500])
        status, with_defaults = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(with_a), "add": "1", "model": "fallback-b"},
        )
        self.assertEqual(status, 200, with_defaults[:500])
        self.assertLess(with_defaults.find(">fallback-a<"), with_defaults.find(">fallback-b<"))
        self.assertNotIn("<textarea", with_defaults)
        self.assertNotIn("page-secret", with_defaults)
        status, moved = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(with_defaults), "move": "up", "model": "fallback-b"},
        )
        self.assertLess(moved.find(">fallback-b<"), moved.find(">fallback-a<"))
        status, _loc, bad = owner.request(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(moved), "add": "1", "model": "not a model"},
        )
        self.assertEqual(status, 400)
        self.assertIn("a default model is not valid", bad)
        self.assertNotIn("page-secret", bad)
        status, _loc, still_defaults = owner.request("GET", "/settings/defaults")
        self.assertLess(still_defaults.find(">fallback-b<"), still_defaults.find(">fallback-a<"))
        status, without_b = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(still_defaults), "remove": "1", "model": "fallback-b"},
        )
        status, cleared = owner.follow(
            "POST",
            "/settings/inference/defaults",
            {"csrf": csrf_of(without_b), "remove": "1", "model": "fallback-a"},
        )
        self.assertEqual(status, 200)
        self.assertIn("No default models.", cleared)
        self.assertNotIn("fallback-a", cleared)
        self.assertNotIn("<textarea", cleared)
        with patch("app.llm._get", side_effect=ValueError("that provider could not be reached")):
            status, _loc, refused = owner.request(
                "POST",
                "/settings/inference",
                {
                    "csrf": csrf_of(cleared),
                    "label": "Other desk",
                    "provider": "openai",
                    "base_url": "",
                    "inference_secret": "another-secret",
                },
            )
        self.assertEqual(status, 400)
        self.assertIn("that provider could not be reached", refused)
        self.assertNotIn("another-secret", refused)
        status, _loc, still = owner.request("GET", "/settings")
        self.assertNotIn("Other desk", still)
        status, cleared = owner.follow(
            "POST",
            "/settings/inference/remove",
            {"csrf": csrf_of(still), "label": "Desk xAI"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("Desk xAI", cleared)
        self.assertNotIn("page-secret", cleared)

    def test_a_proxy_sets_https_urls_and_a_secure_cookie(self):
        previous = config.PATREON_REDIRECT_URI
        config.PATREON_REDIRECT_URI = ""
        try:
            plain = HTTPConnection("127.0.0.1", self.port, timeout=10)
            plain.request("GET", "/login")
            plain_page = plain.getresponse()
            plain_page.read()
            self.assertIsNone(plain_page.getheader("Strict-Transport-Security"))

            body = urlencode({"email": "owner@example.com"})
            headers = {
                "Host": "desk.example.com",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "desk.example.com",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("POST", "/dev/login", body, headers)
            response = conn.getresponse()
            response.read()
            cookie = response.getheader("Set-Cookie") or ""
            self.assertEqual(response.status, 303)
            self.assertIn("; Secure", cookie)
            self.assertEqual(response.getheader("Strict-Transport-Security"), "max-age=31536000")
            sid = cookie.split("sid=", 1)[1].split(";", 1)[0]

            page_headers = {
                "Host": "10.0.0.3:8080",
                "Cookie": f"sid={sid}",
                "Forwarded": 'proto=https;host="desk.example.com"',
                "X-Forwarded-Port": "8443",
            }
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/settings/patreon", headers=page_headers)
            response = conn.getresponse()
            page = response.read().decode()
            self.assertEqual(response.status, 200, page[:400])
            self.assertIn("https://desk.example.com:8443/patreon/callback", page)
            self.assertIn("https://desk.example.com:8443/patreon/webhook", page)
            self.assertNotIn("http://desk.example.com", page)
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", "/local-agent", headers=page_headers)
            response = conn.getresponse()
            agent = response.read().decode()
            self.assertIn("https://desk.example.com:8443/mcp", agent)
        finally:
            config.PATREON_REDIRECT_URI = previous

    def test_workflow_json_wizard(self):
        conn = open_db(config.DATABASE_PATH)
        seed_local_accounts(conn)
        before = conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()["n"]
        conn.close()
        editor, _page, _csrf = self.sign_in("owner@example.com")
        status, _loc, personas = editor.request("GET", "/personas")
        self.assertEqual(status, 200)
        self.assertIn("workflow.json", personas)
        self.assertIn('href="/docs/workflow"', personas)
        self.assertIn("local agent", personas)
        self.assertIn('href="/personas/new/wizard"', personas)
        self.assertNotIn('name="harness"', personas)
        seeded_id = re.search(r'href="/personas/([0-9a-f-]{36})"', personas).group(1)
        status, _loc, detail = editor.request("GET", f"/personas/{seeded_id}")
        self.assertEqual(status, 200)
        self.assertIn('textarea class="code" name="workflow"', detail)
        self.assertIn('aria-selected="true">Edit', detail)
        status, _loc, upload = editor.request("GET", f"/personas/{seeded_id}?part=upload")
        self.assertIn("Upload workflow.json", upload)
        self.assertIn('aria-selected="true">Upload', upload)
        self.assertNotIn(f"/personas/{seeded_id}/wizard", detail)
        status, _loc, missing = editor.request("GET", f"/personas/{seeded_id}/wizard")
        self.assertEqual(status, 404)
        status, _loc, fresh = editor.request("GET", "/personas/new")
        self.assertEqual(status, 200)
        self.assertIn("<h1><code>workflow.json</code></h1>", fresh)
        self.assertIn('href="/docs/workflow"', fresh)
        self.assertIn('name="route" value="upload"', fresh)
        self.assertNotIn('name="route" value="editor"', fresh)
        self.assertIn('aria-selected="true">Upload', fresh)
        self.assertIn('href="/personas/new?how=edit"', fresh)
        self.assertIn('href="/personas/new/wizard"', fresh)
        status, _loc, editing = editor.request("GET", "/personas/new?how=edit")
        self.assertIn('name="route" value="editor"', editing)
        self.assertIn('textarea class="code" name="workflow"', editing)
        self.assertIn('aria-selected="true">Edit', editing)
        self.assertIn("install_agent_files", fresh)
        self.assertNotIn('name="route" value="agent"', fresh)
        self.assertNotIn('name="route" value="steps"', fresh)
        self.assertNotIn('name="harness"', fresh)
        status, _loc, wizard = editor.request("GET", "/personas/new/wizard")
        self.assertEqual(status, 200)
        self.assertIn("creates a new file", wizard)
        self.assertIn("compendium", wizard)
        self.assertIn("produces", wizard)
        self.assertIn('href="/docs/workflow"', wizard)
        self.assertIn('action="/personas/new/wizard"', wizard)
        status, wizard = editor.follow(
            "POST",
            "/personas/new/wizard",
            {
                "csrf": csrf_of(wizard),
                "draft": _draft(wizard),
                "persona_name": "Shelf edit",
                "max_words": "180000",
                "title": "Developmental edit",
                "add": "1",
                "name": "voice",
                "skill": "author-dev",
                "ask": "Diagnose the narrative voice.",
                "destination": "into",
                "document": "letter",
                "heading": "Voice",
                "attach": "compendium",
                "when": "",
            },
        )
        self.assertEqual(status, 200, wizard[:500])
        self.assertIn("voice", wizard)
        self.assertIn("Voice", wizard)
        self.assertIn("Diagnose the narrative voice.", wizard)
        self.assertIn("Create this file", wizard)
        self.assertEqual(before, _persona_count())
        status, blocked = editor.follow(
            "POST",
            "/personas/new/wizard",
            {
                "csrf": csrf_of(wizard),
                "draft": _draft(wizard),
                "max_words": "180000",
                "title": "Developmental edit",
                "remove": "compendium",
            },
        )
        self.assertEqual(status, 400, blocked[:500])
        self.assertIn("compendium is included by", blocked)
        self.assertEqual(before, _persona_count())
        status, detail = editor.follow(
            "POST",
            "/personas/new/wizard",
            {
                "csrf": csrf_of(wizard),
                "draft": _draft(wizard),
                "persona_name": "Shelf edit",
                "max_words": "180000",
                "title": "Developmental edit",
                "create": "1",
            },
        )
        self.assertEqual(status, 200, detail[:500])
        self.assertIn("Shelf edit", detail)
        self.assertIn("This persona is a <code>workflow.json</code>", detail)
        self.assertIn("voice", detail)
        self.assertIn('href="/local-agent"', detail)
        self.assertIn('textarea class="code" name="workflow"', detail)
        persona_id = re.search(r'action="/personas/([0-9a-f-]{36})/workflow"', detail).group(1)
        self.assertNotIn(f"/personas/{persona_id}/wizard", detail)
        self.assertEqual(before + 1, _persona_count())
        status, _loc, refused = editor.request(
            "POST",
            f"/personas/{persona_id}/workflow",
            {"csrf": csrf_of(detail), "workflow": "{"},
        )
        self.assertEqual(status, 400)
        self.assertIn("workflow.json is not valid JSON", refused)
        self.assertIn("<textarea", refused)
        status, _loc, detail = editor.request("GET", f"/personas/{persona_id}")
        self.assertIn("voice", detail)
        self.assertNotIn("workflow.json is not valid JSON", detail)
        saved = _editor_text(detail)
        status, page = editor.follow(
            "POST",
            f"/personas/{persona_id}/workflow",
            {
                "csrf": csrf_of(detail),
                "workflow": saved.replace("Developmental edit", "Shelf letter"),
            },
        )
        self.assertEqual(status, 200, page[:500])
        self.assertIn("Shelf letter", page)
        status, page = editor.follow(
            "POST",
            f"/personas/{persona_id}/workflow",
            {
                "csrf": csrf_of(page),
                "workflow": (
                    "workflow.json",
                    json.dumps(
                        {
                            "title": "From a file",
                            "steps": [
                                {
                                    "name": "opening",
                                    "skill": "author-dev",
                                    "ask": "Diagnose the opening.",
                                    "produces": "letter",
                                }
                            ],
                        }
                    ),
                ),
            },
        )
        self.assertEqual(status, 200, page[:500])
        self.assertIn("From a file", page)
        self.assertIn("opening", page)
        status, typed = editor.follow(
            "POST",
            "/personas/new",
            {
                "csrf": csrf_of(fresh),
                "route": "editor",
                "name": "Typed persona",
                "max_words": "180000",
                "workflow": json.dumps(
                    {
                        "title": "Typed edit",
                        "steps": [
                            {
                                "name": "note",
                                "skill": "author-dev",
                                "ask": "Diagnose the note.",
                                "produces": "letter",
                            }
                        ],
                    }
                ),
            },
        )
        self.assertEqual(status, 200, typed[:500])
        self.assertIn("Typed persona", typed)
        self.assertIn("Typed edit", typed)
        status, _loc, broken = editor.request(
            "POST",
            "/personas/new",
            {
                "csrf": csrf_of(fresh),
                "route": "editor",
                "name": "Broken",
                "max_words": "180000",
                "workflow": "{",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("workflow.json is not valid JSON", broken)
        self.assertIn("Broken", broken)
        status, _loc, bad_file = editor.request(
            "POST",
            "/personas/new",
            {
                "csrf": csrf_of(fresh),
                "route": "upload",
                "name": "Not a file",
                "max_words": "180000",
                "workflow": ("notes.txt", b"hello"),
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("upload a workflow.json file", bad_file)
        status, uploaded = editor.follow(
            "POST",
            "/personas/new",
            {
                "csrf": csrf_of(fresh),
                "route": "upload",
                "name": "Uploaded",
                "max_words": "180000",
                "workflow": (
                    "workflow.json",
                    json.dumps(
                        {
                            "title": "Uploaded edit",
                            "steps": [
                                {
                                    "name": "note",
                                    "skill": "author-dev",
                                    "ask": "Diagnose the opening.",
                                    "produces": "letter",
                                }
                            ],
                        }
                    ),
                ),
            },
        )
        self.assertEqual(status, 200, uploaded[:500])
        self.assertIn("Uploaded", uploaded)
        self.assertIn("Uploaded edit", uploaded)
        self.assertEqual(before + 3, _persona_count())
        anon = self.client()
        status, _loc, doc = anon.request("GET", "/docs/workflow")
        self.assertEqual(status, 200)
        self.assertIn("<h1>workflow.json</h1>", doc)
        self.assertIn("<h2>The file</h2>", doc)
        self.assertIn("<code>produces</code>", doc)
        self.assertIn("<code>author-compendium</code>", doc)
        self.assertIn('href="/skills/desk-persona"', doc)
        self.assertIn('href="https://agentskills.io/specification"', doc)
        self.assertNotIn("## The file", doc)
        self.assertIn("Sign in", doc)


def _draft(page: str) -> str:
    match = re.search(r'<textarea class="draft" name="draft">(.*?)</textarea>', page, re.S)
    if match is None:
        raise AssertionError(page[:800])
    return html.unescape(match.group(1))


def _editor_text(page: str) -> str:
    match = re.search(r'<textarea class="code" name="workflow">(.*?)</textarea>', page, re.S)
    if match is None:
        raise AssertionError(page[:800])
    return html.unescape(match.group(1))


def _persona_count() -> int:
    conn = open_db(config.DATABASE_PATH)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM personas").fetchone()["n"]
    finally:
        conn.close()


def _shown_cents(amount: str) -> int:
    text = amount.lstrip("$")
    if "." in text:
        whole, frac = text.split(".")
        return int(whole) * 100 + int(frac)
    return int(text) * 100


def _post_status(client: Client, path: str, form: dict):
    status, _loc, body = client.request("POST", path, form)
    return status, body
