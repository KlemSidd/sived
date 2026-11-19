"""Author access fails closed unless Patreon confirms that editor's campaign."""

import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

from app import config
from app import patreon
from app.db import open_db
from app.services import (
    become_customer,
    create_owner,
    editor_for_user,
    invite_editor,
    set_patreon_integration,
    token_hash,
)
from app.web import App
from tests.test_http import Client


CAMPAIGN = "12345"
SECRET = "hook-secret"


def identity(
    user_id="444",
    email="bea@patreon.test",
    name="Bea",
    members=None,
    email_set=True,
    verified=True,
):
    attributes = {"full_name": name, "is_email_verified": verified}
    if email_set:
        attributes["email"] = email
    return {
        "data": {"id": user_id, "type": "user", "attributes": attributes},
        "included": members or [],
    }


def member(campaign, cents=500, tiers=None, include_cents=True, include_tiers=True):
    attributes = {}
    if include_cents:
        attributes["currently_entitled_amount_cents"] = cents
    relationships = {"campaign": {"data": {"id": str(campaign), "type": "campaign"}}}
    if include_tiers:
        relationships["currently_entitled_tiers"] = {"data": list(tiers or [])}
    return {
        "type": "member",
        "id": "member-1",
        "attributes": attributes,
        "relationships": relationships,
    }


class FakePatreon:
    def __init__(self):
        self.identity = identity()
        self.token_status = 200
        self.identity_status = 200
        self.token_body = {"access_token": "access-1", "refresh_token": "refresh-1"}
        self.token_raw = None
        self.raise_error = None
        self.calls = []
        self.bodies = []

    def __call__(self, method, url, headers, body=None, timeout=15):
        self.calls.append((method, url))
        self.bodies.append(body)
        if self.raise_error is not None:
            raise self.raise_error
        if url.startswith(patreon.TOKEN_URL):
            raw = self.token_raw if self.token_raw is not None else json.dumps(self.token_body).encode()
            return self.token_status, raw
        if url.startswith(patreon.IDENTITY_URL):
            return self.identity_status, json.dumps(self.identity).encode()
        raise AssertionError(url)


class PatreonGateTests(unittest.TestCase):
    def setUp(self):
        self.prev = (
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.PATREON_REDIRECT_URI,
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            patreon.RECHECK_SECONDS,
            patreon._request,
        )
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-patreon-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEV_LOGIN = False
        config.DEMO_MODE = False
        config.PATREON_REDIRECT_URI = "http://127.0.0.1/patreon/callback"
        conn = open_db(config.DATABASE_PATH)
        self.owner_id = create_owner(conn, "owner@example.com")
        editor = editor_for_user(conn, self.owner_id)
        self.editor_id = editor["id"]
        self.org_id = editor["organization_id"]
        conn.close()
        self.fake = FakePatreon()
        patreon._request = self.fake
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), App)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        (
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.PATREON_REDIRECT_URI,
            config.DATA_DIR,
            config.DATABASE_PATH,
            config.OBJECT_DIR,
            patreon.RECHECK_SECONDS,
            patreon._request,
        ) = self.prev

    def client(self) -> Client:
        return Client(self.port)

    def connect(
        self,
        campaign=CAMPAIGN,
        client_id="client",
        client_secret="secret",
        webhook_secret=SECRET,
        editor=None,
    ):
        conn = open_db(config.DATABASE_PATH)
        row = editor if editor is not None else editor_for_user(conn, self.owner_id)
        set_patreon_integration(
            conn,
            row,
            campaign_id=campaign,
            client_id=client_id,
            client_secret=client_secret,
            webhook_secret=webhook_secret,
        )
        conn.close()

    def start(self, editor_id=None, client_id="client"):
        client = self.client()
        status, location, page = client.request("GET", f"/e/{editor_id or self.editor_id}/patreon")
        self.assertEqual(status, 303, page[:400])
        parsed = parse_qs(urlparse(location).query)
        self.assertEqual(parsed["client_id"], [client_id])
        self.assertEqual(parsed["redirect_uri"], ["http://127.0.0.1/patreon/callback"])
        self.assertIn("identity.memberships", parsed["scope"][0])
        self.assertIn("identity[email]", parsed["scope"][0])
        return client, parsed["state"][0]

    def callback(self, client, state, code="code-1"):
        return client.follow("GET", f"/patreon/callback?code={code}&state={state}")

    def sign_webhook(self, body: bytes) -> str:
        return hmac.new(SECRET.encode(), body, hashlib.md5).hexdigest()

    def post_webhook(self, body: bytes, event: str, signature: str | None = None, length: int | None = None):
        headers = {"Content-Type": "application/json", "X-Patreon-Event": event}
        if signature is not None:
            headers["X-Patreon-Signature"] = signature
        if length is None:
            length = len(body)
        headers["Content-Length"] = str(length)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/patreon/webhook", body, headers)
        response = conn.getresponse()
        return response.status, response.read()

    def test_closed_page_when_patreon_is_not_connected(self):
        anon = self.client()
        status, _loc, page = anon.request("GET", f"/e/{self.editor_id}")
        self.assertEqual(status, 200)
        self.assertIn("This editor has not connected Patreon.", page)
        self.assertNotIn("Become a customer", page)
        self.assertNotIn("Continue with Patreon", page)
        self.assertNotIn("$", page)
        status, _loc, refused = anon.request(
            "POST",
            f"/e/{self.editor_id}/join",
            {"name": "Bea", "email": "bea@example.com"},
        )
        self.assertEqual(status, 400)
        self.assertIn("sign in with Patreon", refused)
        conn = open_db(config.DATABASE_PATH)
        count = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = 'author'").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 0)

        self.connect()
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE editors SET patreon_client_secret = '' WHERE id = ?",
            (self.editor_id,),
        )
        conn.commit()
        conn.close()
        status, _loc, page = anon.request("GET", f"/e/{self.editor_id}")
        self.assertIn("has not connected Patreon", page)
        self.assertNotIn("Continue with Patreon", page)
        status, _loc, refused = anon.request("GET", f"/e/{self.editor_id}/patreon")
        self.assertEqual(status, 400)
        self.assertIn("this editor has not connected Patreon", refused)

    def test_continue_with_patreon_admits_an_entitled_member(self):
        self.connect()
        anon = self.client()
        status, _loc, page = anon.request("GET", f"/e/{self.editor_id}")
        self.assertIn(f"href='/e/{self.editor_id}/patreon'", page)
        self.assertIn("Continue with Patreon", page)
        self.assertNotIn("Become a customer", page)
        self.assertNotIn('name="email"', page)
        status, _loc, login = anon.request("GET", "/login")
        self.assertIn("Continue with Patreon", login)
        self.assertNotIn("owner@example.com", login)

        self.fake.identity = identity(members=[member(CAMPAIGN, cents=500)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 200, page[:500])
        self.assertIn("Your manuscripts", page)
        self.assertNotIn("refresh-1", page)
        self.assertNotIn("$", page)
        conn = open_db(config.DATABASE_PATH)
        user = conn.execute(
            "SELECT auth_provider, auth_subject, role FROM users WHERE email = ?",
            ("bea@patreon.test",),
        ).fetchone()
        author = conn.execute(
            "SELECT patron_status, patreon_user_id, patreon_refresh_token FROM authors WHERE patreon_user_id = ?",
            ("444",),
        ).fetchone()
        conn.close()
        self.assertEqual(user["auth_provider"], "patreon")
        self.assertEqual(user["auth_subject"], "444")
        self.assertEqual(user["role"], "author")
        self.assertEqual(author["patron_status"], "active")
        self.assertEqual(author["patreon_refresh_token"], "refresh-1")

        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("that Patreon login expired", page)

    def test_refuses_the_wrong_campaign_a_former_member_and_a_missing_email(self):
        self.connect()
        self.fake.identity = identity(members=[member("99999", cents=500)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("not an active member of this editor", page)
        self.assertIn("campaign", page)

        self.fake.identity = identity(members=[member(CAMPAIGN, cents=0, tiers=[])])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("not an active member", page)

        self.fake.identity = identity(email_set=False, members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("Patreon did not share an email", page)
        conn = open_db(config.DATABASE_PATH)
        count = conn.execute("SELECT COUNT(*) AS n FROM authors").fetchone()["n"]
        conn.close()
        self.assertEqual(count, 0)

    def test_unverified_patreon_email_does_not_open_an_account(self):
        self.connect()
        self.fake.identity = identity(verified=False, members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("verify your email at Patreon before signing in", page)
        token_bodies = [
            body for body in self.fake.bodies if body and b"grant_type=authorization_code" in body
        ]
        self.assertIn(b"client_id=client", token_bodies[-1])
        self.assertIn(b"client_secret=secret", token_bodies[-1])

        payload = identity(members=[member(CAMPAIGN)])
        del payload["data"]["attributes"]["is_email_verified"]
        self.fake.identity = payload
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("verify your email at Patreon before signing in", page)
        conn = open_db(config.DATABASE_PATH)
        authors = conn.execute("SELECT COUNT(*) AS n FROM authors").fetchone()["n"]
        users = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE email = ?",
            ("bea@patreon.test",),
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(authors, 0)
        self.assertEqual(users, 0)

    def test_another_editors_webhook_does_not_change_this_author(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        owner_client, state = self.start()
        self.callback(owner_client, state)

        conn = open_db(config.DATABASE_PATH)
        raw = invite_editor(conn, self.owner_id, "ada@example.com", None)
        from app.services import accept_editor_invite

        ada_user = accept_editor_invite(conn, raw, "Ada")
        ada = editor_for_user(conn, ada_user)
        set_patreon_integration(
            conn,
            ada,
            campaign_id="777",
            client_id="ada-client",
            client_secret="ada-secret",
            webhook_secret="ada-hook",
        )
        conn.close()
        self.fake.identity = identity(
            user_id="555",
            email="cara@patreon.test",
            name="Cara",
            members=[member("777")],
        )
        ada_client, ada_state = self.start(ada["id"], client_id="ada-client")
        status, page = self.callback(ada_client, ada_state)
        self.assertEqual(status, 200, page[:400])
        token_bodies = [
            body for body in self.fake.bodies if body and b"grant_type=authorization_code" in body
        ]
        self.assertIn(b"client_id=ada-client", token_bodies[-1])
        self.assertIn(b"client_secret=ada-secret", token_bodies[-1])

        def member_body(user_id, campaign):
            return json.dumps(
                {
                    "data": {
                        "type": "member",
                        "relationships": {
                            "user": {"data": {"id": user_id}},
                            "campaign": {"data": {"id": campaign}},
                        },
                    }
                }
            ).encode()

        def signed(body, secret):
            return hmac.new(secret.encode(), body, hashlib.md5).hexdigest()

        conn = open_db(config.DATABASE_PATH)
        conn.execute("UPDATE editors SET patreon_webhook_secret = 'same-hook'")
        conn.commit()
        conn.close()
        shared = member_body("444", CAMPAIGN)
        status, raw = self.post_webhook(shared, "members:delete", signed(shared, "same-hook"))
        self.assertEqual(status, 400)
        self.assertIn(b"not valid", raw)
        status, page = owner_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)
        status, page = ada_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE editors SET patreon_webhook_secret = ? WHERE id = ?",
            (SECRET, self.editor_id),
        )
        conn.execute(
            "UPDATE editors SET patreon_webhook_secret = ? WHERE id = ?",
            ("ada-hook", ada["id"]),
        )
        conn.commit()
        conn.close()

        crossed = member_body("444", CAMPAIGN)
        status, raw = self.post_webhook(crossed, "members:delete", signed(crossed, "ada-hook"))
        self.assertEqual(status, 200, raw)
        status, page = owner_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        other = member_body("555", "777")
        status, _raw = self.post_webhook(other, "members:delete", self.sign_webhook(other))
        self.assertEqual(status, 200)
        status, page = ada_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        named = member_body("444", "777")
        status, _raw = self.post_webhook(named, "members:delete", signed(named, "ada-hook"))
        self.assertEqual(status, 200)
        status, page = owner_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)
        status, page = ada_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        revoke = member_body("555", "777")
        status, raw = self.post_webhook(revoke, "members:delete", signed(revoke, "ada-hook"))
        self.assertEqual(status, 200, raw)
        status, _loc, page = ada_client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)
        status, page = owner_client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

    def test_a_free_entitled_tier_is_a_membership(self):
        self.connect()
        self.fake.identity = identity(
            members=[member(CAMPAIGN, cents=0, tiers=[{"id": "tier-1", "type": "tier"}])]
        )
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 200, page[:500])
        self.assertIn("Your manuscripts", page)

    def test_patreon_login_state_is_stored_as_a_hash(self):
        self.connect()
        client, state = self.start()
        conn = open_db(config.DATABASE_PATH)
        stored = conn.execute("SELECT state FROM patreon_oauth_states").fetchone()["state"]
        self.assertEqual(stored, token_hash(state))
        self.assertNotEqual(stored, state)
        status, page = self.callback(self.client(), stored)
        self.assertEqual(status, 400)
        self.assertIn("that Patreon login expired", page)
        self.fake.identity = identity(members=[member(CAMPAIGN, cents=500)])
        status, page = self.callback(client, state)
        self.assertEqual(status, 200, page[:400])

        raw = "still-raw-state"
        conn.execute(
            """
            INSERT INTO patreon_oauth_states (state, editor_id, created_at, expires_at)
            VALUES (?, ?, datetime('now'), datetime('now', '+10 minutes'))
            """,
            (raw, self.editor_id),
        )
        conn.commit()
        conn.close()
        conn = open_db(config.DATABASE_PATH)
        self.assertIsNone(
            conn.execute(
                "SELECT state FROM patreon_oauth_states WHERE state = ?",
                (raw,),
            ).fetchone()
        )
        conn.close()
        status, _loc, page = self.client().request(
            "GET", f"/patreon/callback?code=abc&state={raw}"
        )
        self.assertEqual(status, 400)
        self.assertIn("that Patreon login expired", page)

    def test_bad_patreon_login_does_not_open_a_session(self):
        self.connect()
        status, _loc, page = self.client().request("GET", "/patreon/callback?error=access_denied")
        self.assertEqual(status, 400)
        self.assertIn("Patreon did not authorize this login", page)

        status, _loc, page = self.client().request("GET", "/patreon/callback?code=abc&state=missing")
        self.assertEqual(status, 400)
        self.assertIn("that Patreon login expired", page)

        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            """
            INSERT INTO patreon_oauth_states (state, editor_id, created_at, expires_at)
            VALUES (?, ?, datetime('now', '-20 minutes'), datetime('now', '-10 minutes'))
            """,
            (token_hash("old-state"), self.editor_id),
        )
        conn.commit()
        conn.close()
        status, _loc, page = self.client().request("GET", "/patreon/callback?code=abc&state=old-state")
        self.assertEqual(status, 400)
        self.assertIn("that Patreon login expired", page)

        self.fake.token_status = 400
        self.fake.token_raw = b'{"error":"refresh-secret-value"}'
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("Patreon did not authorize this login", page)
        self.assertNotIn("refresh-secret-value", page)

    def test_patreon_email_cannot_take_another_role_or_another_editor(self):
        self.connect()
        self.fake.identity = identity(email="owner@example.com", members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("that email already has another role", page)

        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertEqual(status, 200, page[:400])

        conn = open_db(config.DATABASE_PATH)
        raw = invite_editor(conn, self.owner_id, "ada@example.com", None)
        from app.services import accept_editor_invite

        ada_id = accept_editor_invite(conn, raw, "Ada")
        ada = editor_for_user(conn, ada_id)
        set_patreon_integration(
            conn,
            ada,
            campaign_id="777",
            client_id="ada-client",
            client_secret="ada-secret",
            webhook_secret="ada-hook",
        )
        conn.close()
        self.fake.identity = identity(members=[member("777", cents=500)])
        client, state = self.start(ada["id"], client_id="ada-client")
        status, page = self.callback(client, state)
        self.assertEqual(status, 400)
        self.assertIn("author already belongs to an editor", page)

    def test_webhook_revokes_and_the_next_visit_fails_closed(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertIn("Your manuscripts", page)

        body = json.dumps(
            {
                "data": {
                    "type": "member",
                    "attributes": {"patron_status": "active_patron"},
                    "relationships": {
                        "user": {"data": {"id": "444", "type": "user"}},
                        "campaign": {"data": {"id": CAMPAIGN, "type": "campaign"}},
                    },
                }
            }
        ).encode()
        status, raw = self.post_webhook(body, "members:update", "not-the-signature")
        self.assertEqual(status, 400)
        self.assertIn(b"Patreon webhook signature was not valid", raw)
        status, home = client.follow("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Your manuscripts", home)

        payload = {
            "data": {
                "type": "member",
                "attributes": {
                    "currently_entitled_amount_cents": 0,
                    "patron_status": "former_patron",
                },
                "relationships": {
                    "user": {"data": {"id": "444"}},
                    "campaign": {"data": {"id": CAMPAIGN}},
                    "currently_entitled_tiers": {"data": []},
                },
            }
        }
        body = json.dumps(payload).encode()
        status, raw = self.post_webhook(body, "members:update", self.sign_webhook(body).upper())
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"ok")
        status, _loc, page = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)
        self.assertNotIn("Your manuscripts", page)
        status, location, _page = client.request("GET", "/submit")
        self.assertEqual(status, 303)
        self.assertEqual(location, "/login")

    def test_delete_webhook_revokes_and_a_thin_payload_does_not(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        self.callback(client, state)
        thin = json.dumps(
            {
                "data": {
                    "type": "member",
                    "attributes": {},
                    "relationships": {
                        "user": {"data": {"id": "444"}},
                        "campaign": {"data": {"id": CAMPAIGN}},
                    },
                }
            }
        ).encode()
        status, raw = self.post_webhook(thin, "members:update", self.sign_webhook(thin))
        self.assertEqual(status, 200, raw)
        status, page = client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        other = json.dumps(
            {
                "data": {
                    "type": "member",
                    "attributes": {"currently_entitled_amount_cents": 0},
                    "relationships": {
                        "user": {"data": {"id": "444"}},
                        "campaign": {"data": {"id": "99999"}},
                        "currently_entitled_tiers": {"data": []},
                    },
                }
            }
        ).encode()
        status, _raw = self.post_webhook(other, "members:update", self.sign_webhook(other))
        self.assertEqual(status, 200)
        status, page = client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        post = json.dumps({"data": {"type": "post", "id": "1"}}).encode()
        status, raw = self.post_webhook(post, "posts:publish", self.sign_webhook(post))
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"ok")

        body = json.dumps(
            {
                "data": {
                    "type": "member",
                    "relationships": {
                        "user": {"data": {"id": "444"}},
                        "campaign": {"data": {"id": CAMPAIGN}},
                    },
                }
            }
        ).encode()
        status, raw = self.post_webhook(body, "members:delete", self.sign_webhook(body))
        self.assertEqual(status, 200, raw)
        status, _loc, page = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)

    def test_webhook_without_a_secret_or_a_body_does_not_change_access(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        self.callback(client, state)
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE editors SET patreon_webhook_secret = '' WHERE id = ?",
            (self.editor_id,),
        )
        conn.commit()
        conn.close()
        body = json.dumps(
            {
                "data": {
                    "type": "member",
                    "relationships": {
                        "user": {"data": {"id": "444"}},
                        "campaign": {"data": {"id": CAMPAIGN}},
                    },
                }
            }
        ).encode()
        status, raw = self.post_webhook(body, "members:delete", self.sign_webhook(body))
        self.assertEqual(status, 400)
        self.assertIn(b"not valid", raw)
        status, page = client.follow("GET", "/")
        self.assertIn("Your manuscripts", page)

        status, raw = self.post_webhook(b"", "members:update", signature=None, length=None)
        self.assertEqual(status, 400)
        status, raw = self.post_webhook(b"x", "members:update", signature="aa", length=1_000_001)
        self.assertEqual(status, 400)
        self.assertIn(b"could not be read", raw)

    def test_stale_membership_fails_closed_when_patreon_does_not_confirm(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        status, page = self.callback(client, state)
        self.assertIn("Your manuscripts", page)
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE authors SET membership_checked_at = ? WHERE patreon_user_id = ?",
            ("2020-01-01T00:00:00+00:00", "444"),
        )
        conn.commit()
        conn.close()
        self.fake.identity = identity(members=[member(CAMPAIGN, cents=0, tiers=[])])
        self.fake.token_body = {"access_token": "access-2", "refresh_token": "rotated-secret"}
        status, _loc, page = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)
        self.assertNotIn("rotated-secret", page)

        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        self.callback(client, state)
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE authors SET membership_checked_at = ? WHERE patreon_user_id = ?",
            ("2020-01-01T00:00:00+00:00", "444"),
        )
        conn.commit()
        conn.close()
        self.fake.raise_error = patreon.PatreonError("Patreon could not be reached")
        status, _loc, page = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("Patreon could not be reached", page)
        conn = open_db(config.DATABASE_PATH)
        status_row = conn.execute(
            "SELECT patron_status FROM authors WHERE patreon_user_id = ?",
            ("444",),
        ).fetchone()
        conn.close()
        self.assertEqual(status_row["patron_status"], "active")

    def test_a_still_entitled_member_keeps_access_and_the_new_token_stays_hidden(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        self.callback(client, state)
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE authors SET membership_checked_at = ? WHERE patreon_user_id = ?",
            ("2020-01-01T00:00:00+00:00", "444"),
        )
        conn.commit()
        conn.close()
        self.fake.token_body = {"access_token": "access-2", "refresh_token": "rotated-secret"}
        status, page = client.follow("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Your manuscripts", page)
        self.assertNotIn("rotated-secret", page)
        conn = open_db(config.DATABASE_PATH)
        token = conn.execute(
            "SELECT patreon_refresh_token FROM authors WHERE patreon_user_id = ?",
            ("444",),
        ).fetchone()["patreon_refresh_token"]
        conn.close()
        self.assertEqual(token, "rotated-secret")

    def test_profile_stays_open_and_manuscripts_do_not(self):
        self.connect()
        self.fake.identity = identity(members=[member(CAMPAIGN)])
        client, state = self.start()
        _status, page = self.callback(client, state)
        conn = open_db(config.DATABASE_PATH)
        conn.execute(
            "UPDATE authors SET patron_status = 'inactive' WHERE patreon_user_id = ?",
            ("444",),
        )
        conn.commit()
        conn.close()
        status, _loc, profile = client.request("GET", "/profile")
        self.assertEqual(status, 200)
        self.assertIn("Profile", profile)
        status, _loc, home = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", home)

    def test_local_and_demo_bypass_stops_when_the_flag_is_off(self):
        config.DEV_LOGIN = True
        anon = self.client()
        status, _loc, page = anon.request("GET", f"/e/{self.editor_id}")
        self.assertIn("Become a customer", page)
        self.assertIn("does not ask Patreon", page)
        self.assertNotIn("Continue with Patreon", page)
        client = self.client()
        status, page = client.follow(
            "POST",
            f"/e/{self.editor_id}/join",
            {"name": "Bea", "email": "bea@example.com"},
        )
        self.assertEqual(status, 200, page[:400])
        self.assertIn("Your manuscripts", page)
        config.DEV_LOGIN = False
        status, _loc, page = client.request("GET", "/")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)

        config.DEMO_MODE = True
        demo = self.client()
        status, page = demo.follow(
            "POST",
            f"/e/{self.editor_id}/join",
            {"name": "Bea", "email": "bea@example.com"},
        )
        self.assertIn("Your manuscripts", page)
        config.DEMO_MODE = False
        status, _loc, page = demo.request("GET", "/submit")
        self.assertEqual(status, 400)
        self.assertIn("this Patreon membership is not active", page)

    def test_only_the_editor_can_save_the_campaign(self):
        config.DEV_LOGIN = True
        owner = self.client()
        status, page = owner.follow("POST", "/dev/login", {"email": "owner@example.com"})
        self.assertNotIn("Save Patreon", page)
        self.assertNotIn("Authors are your customers", page)
        status, _loc, index = owner.request("GET", "/settings")
        self.assertEqual(status, 200)
        self.assertIn('role="tablist"', index)
        self.assertNotIn("Save Patreon", index)
        status, _loc, page = owner.request("GET", "/settings/patreon")
        self.assertEqual(status, 200)
        self.assertIn("Not connected", page)
        self.assertIn('aria-selected="true">Patreon', page)
        self.assertIn("Save Patreon", page)
        status, _loc, appearance = owner.request("GET", "/settings/appearance")
        self.assertIn("Save appearance", appearance)
        self.assertNotIn("are not a membership", page)
        self.assertNotIn("https://www.patreon.com/portal/start/oauth-explained", page)
        self.assertIn("http://127.0.0.1/patreon/callback", page)
        self.assertIn("http://127.0.0.1/patreon/webhook", page)
        from tests.test_http import csrf_of

        status, page = owner.follow(
            "POST",
            "/editor/patreon",
            {"csrf": csrf_of(page), "campaign_id": "12ab", "client_id": "client"},
        )
        self.assertEqual(status, 400)
        self.assertIn("a Patreon campaign id is required", page)
        status, page = owner.follow("POST", "/dev/login", {"email": "owner@example.com"})
        status, page = owner.follow(
            "POST",
            "/editor/patreon",
            {
                "csrf": csrf_of(page),
                "campaign_id": CAMPAIGN,
                "client_id": "client",
                "client_secret": "editor-secret-value",
                "webhook_secret": "editor-hook-value",
            },
        )
        self.assertIn("Status: Connected", page)
        self.assertIn(f"Campaign: {CAMPAIGN}", page)
        self.assertNotIn("editor-secret-value", page)
        self.assertNotIn("editor-hook-value", page)
        self.assertIn("Client secret: saved", page)

        conn = open_db(config.DATABASE_PATH)
        become_customer(conn, self.editor_id, "Bea", "bea@example.com")
        conn.execute(
            """
            UPDATE authors
            SET patreon_user_id = '444', patron_status = 'active', membership_checked_at = ?
            WHERE user_id = (SELECT id FROM users WHERE email = 'bea@example.com')
            """,
            ("2026-11-18T00:00:00+00:00",),
        )
        conn.commit()
        raw = invite_editor(
            conn,
            self.owner_id,
            "aide@example.com",
            self.org_id,
            {"manuscripts": 1},
        )
        conn.close()
        aide = self.client()
        aide.follow("POST", f"/invite/{raw}", {"name": "Aide"})
        status, page = aide.follow("POST", "/dev/login", {"email": "aide@example.com"})
        self.assertNotIn("Save Patreon", page)
        self.assertNotIn("href='/settings'", page)
        aide_csrf = csrf_of(page)
        status, _loc, denied_settings = aide.request("GET", "/settings")
        self.assertEqual(status, 400)
        self.assertIn("only the editor can change settings", denied_settings)
        status, page = aide.follow(
            "POST",
            "/editor/patreon",
            {"csrf": aide_csrf, "campaign_id": "999"},
        )
        self.assertEqual(status, 400)
        self.assertIn("only the editor can connect Patreon", page)
        conn = open_db(config.DATABASE_PATH)
        campaign = conn.execute(
            "SELECT patreon_campaign_id FROM editors WHERE id = ?",
            (self.editor_id,),
        ).fetchone()["patreon_campaign_id"]
        checked = conn.execute(
            """
            SELECT membership_checked_at FROM authors
            WHERE user_id = (SELECT id FROM users WHERE email = 'bea@example.com')
            """
        ).fetchone()["membership_checked_at"]
        conn.close()
        self.assertEqual(campaign, CAMPAIGN)
        self.assertEqual(checked, "2026-11-18T00:00:00+00:00")

        status, page = owner.follow("POST", "/dev/login", {"email": "owner@example.com"})
        owner.follow(
            "POST",
            "/editor/patreon",
            {"csrf": csrf_of(page), "campaign_id": "54321", "client_id": "client"},
        )
        conn = open_db(config.DATABASE_PATH)
        checked = conn.execute(
            """
            SELECT membership_checked_at FROM authors
            WHERE user_id = (SELECT id FROM users WHERE email = 'bea@example.com')
            """
        ).fetchone()["membership_checked_at"]
        conn.close()
        self.assertEqual(checked, "")

    def test_token_error_body_is_not_shown_and_unreachable_patreon_is_closed(self):
        self.fake.token_status = 400
        self.fake.token_raw = b'{"error":"refresh-secret-value"}'
        with self.assertRaises(patreon.PatreonError) as caught:
            patreon.login_from_code("code", "127.0.0.1", "client", "secret")
        self.assertEqual(str(caught.exception), "Patreon did not authorize this login")
        self.assertNotIn("refresh-secret-value", str(caught.exception))

        self.fake.identity_status = 429
        self.fake.token_status = 200
        self.fake.token_raw = None
        with self.assertRaises(patreon.PatreonError) as caught:
            patreon.login_from_code("code", "127.0.0.1", "client", "secret")
        self.assertEqual(str(caught.exception), "Patreon could not confirm this membership")

        original = patreon.urlopen

        def explode(*_args, **_kwargs):
            raise URLError("timed out")

        patreon._request = self.prev[-1]
        patreon.urlopen = explode
        try:
            with self.assertRaises(patreon.PatreonError) as caught:
                patreon._request("GET", "https://www.patreon.com/api/oauth2/v2/identity", {})
            self.assertEqual(str(caught.exception), "Patreon could not be reached")
        finally:
            patreon.urlopen = original
            patreon._request = self.fake


class PatreonSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-patreon-schema-"))
        self.prev = (config.DATA_DIR, config.DATABASE_PATH, config.OBJECT_DIR)
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"

    def tearDown(self):
        config.DATA_DIR, config.DATABASE_PATH, config.OBJECT_DIR = self.prev

    def test_account_table_gains_patreon_and_keeps_the_owner(self):
        path = self.tmp / "old-auth.sqlite"
        raw = sqlite3.connect(path)
        raw.execute(
            """
            CREATE TABLE users (
              id text PRIMARY KEY,
              email text NOT NULL,
              name text,
              role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'author')),
              is_owner integer NOT NULL DEFAULT 0,
              is_admin integer NOT NULL DEFAULT 0,
              contact_email text NOT NULL DEFAULT '',
              phone text NOT NULL DEFAULT '',
              auth_provider text NOT NULL CHECK (auth_provider IN ('google', 'dev')),
              auth_subject text NOT NULL,
              created_at text NOT NULL,
              deleted_at text,
              UNIQUE (auth_provider, auth_subject)
            )
            """
        )
        raw.execute(
            """
            INSERT INTO users (
              id, email, name, role, is_owner, is_admin, contact_email, phone,
              auth_provider, auth_subject, created_at
            ) VALUES (
              'u1', 'owner@example.com', 'Ada', 'owner', 1, 0, 'ada@contact.test', '555',
              'dev', 'dev:owner', '2026-01-01T00:00:00+00:00'
            )
            """
        )
        raw.execute("CREATE TABLE editors (id text PRIMARY KEY, user_id text)")
        raw.execute("INSERT INTO editors (id, user_id) VALUES ('e1', 'u1')")
        raw.execute("CREATE TABLE authors (id text PRIMARY KEY, user_id text)")
        raw.commit()
        raw.close()
        opened = open_db(path)
        sql = opened.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
        self.assertIn("'patreon'", sql)
        row = opened.execute(
            "SELECT name, is_owner, contact_email, phone FROM users WHERE id = 'u1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("Ada", 1, "ada@contact.test", "555"))
        editors = {info[1] for info in opened.execute("PRAGMA table_info(editors)")}
        authors = {info[1] for info in opened.execute("PRAGMA table_info(authors)")}
        self.assertIn("patreon_campaign_id", editors)
        self.assertIn("patreon_client_secret", editors)
        self.assertIn("patreon_webhook_secret", editors)
        self.assertIn("patreon_refresh_token", authors)
        self.assertIsNotNone(
            opened.execute(
                "SELECT name FROM sqlite_master WHERE name = 'patreon_oauth_states'"
            ).fetchone()
        )
        opened.execute(
            """
            INSERT INTO users (
              id, email, name, role, auth_provider, auth_subject, created_at
            ) VALUES ('u2', 'a@b.c', 'Ann', 'author', 'patreon', '444', '2026-01-01T00:00:00+00:00')
            """
        )
        opened.commit()
        opened.close()

    def test_stub_users_table_is_left_alone(self):
        path = self.tmp / "stub.sqlite"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE users (id text)")
        raw.execute("INSERT INTO users (id) VALUES ('x')")
        raw.commit()
        raw.close()
        opened = open_db(path)
        columns = {info[1] for info in opened.execute("PRAGMA table_info(users)")}
        self.assertEqual(columns, {"id"})
        opened.close()


if __name__ == "__main__":
    unittest.main()
