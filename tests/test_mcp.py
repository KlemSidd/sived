"""Transfer a local workflow into this login's persona over MCP."""

import json
import os
import re
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("XAI_MODE", "local")

from app import config  # noqa: E402
from app.db import open_db  # noqa: E402
from app.mcp_server import handle_message  # noqa: E402
from app.services import (  # noqa: E402
    accept_editor_invite,
    author_for_user,
    become_customer,
    create_job,
    dev_capture,
    editor_for_user,
    invite_editor,
    issue_agent_token,
    list_agent_tokens,
    open_owner_practice,
    publish_persona,
    request_human,
    revoke_agent_token,
    seed_local_accounts,
    user_for_agent_token,
    write_manuscript,
)
from app.web import App  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

SKILL = {"name": "house-style", "body": "Prefer the names the author already used.\n"}
MCP_URL = "http://127.0.0.1:8080/mcp"


def _package(skill: dict | None = None) -> dict:
    from app.harness import default_workflow

    files = [
        {"path": "manifest.json", "text": '{"harness": "workflow", "entry": "workflow.json"}\n'},
        {"path": "workflow.json", "text": json.dumps(default_workflow()) + "\n"},
    ]
    if skill is not None:
        files.append({"path": f"skills/{skill['name']}/SKILL.md", "text": skill["body"]})
    return {"files": files}


def _user(conn, user_id: str):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def _call(conn, user, name: str, arguments: dict | None = None):
    return handle_message(
        conn,
        user,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        mcp_url=MCP_URL,
    )


def _payload(response) -> dict | list:
    result = response["result"]
    if result["isError"]:
        raise AssertionError(result["content"][0]["text"])
    return json.loads(result["content"][0]["text"])


def _error(response) -> str:
    result = response["result"]
    if not result["isError"]:
        raise AssertionError(result["content"][0]["text"])
    return result["content"][0]["text"]


class McpDeskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-mcp-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEV_LOGIN = True
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        self.conn = open_db(config.DATABASE_PATH)
        self.seeded = seed_local_accounts(self.conn)
        self.owner = _user(self.conn, self.seeded["owner_id"])
        # The seed has no second editor. These tests invite one, the way the owner would.
        raw = invite_editor(self.conn, self.owner["id"], "editor@example.com", None)
        editor_user_id = accept_editor_invite(self.conn, raw, "Ada")
        editor = editor_for_user(self.conn, editor_user_id)
        persona = self.conn.execute(
            "SELECT * FROM personas WHERE editor_id = ?",
            (editor["id"],),
        ).fetchone()
        publish_persona(self.conn, editor, persona["id"])
        author_user_id = become_customer(self.conn, editor["id"], "Bea", "bea@example.com")
        self.editor = _user(self.conn, editor_user_id)
        self.persona = self.conn.execute(
            "SELECT * FROM personas WHERE id = ?",
            (persona["id"],),
        ).fetchone()
        self.seeded = {
            "owner_id": self.seeded["owner_id"],
            "editor_user_id": editor_user_id,
            "editor_id": editor["id"],
            "author_user_id": author_user_id,
        }

    def tearDown(self):
        self.conn.close()

    def test_initialize_lists_the_persona_tools(self):
        response = handle_message(
            self.conn,
            self.editor,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            mcp_url=MCP_URL,
        )
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["name"], "sived")
        listed = handle_message(
            self.conn,
            self.editor,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
            mcp_url=MCP_URL,
        )
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertIn("install_agent_files", names)
        self.assertNotIn("save_persona", names)
        self.assertIn("run_workload", names)
        self.assertIn("send_message", names)
        self.assertIn("invite_assistant", names)
        self.assertIn("patreon_integration", names)
        self.assertIn("set_patreon_integration", names)
        self.assertIn("practice_settings", names)
        self.assertIn("set_practice_settings", names)
        self.assertIn("author_notices", names)
        self.assertIn("set_author_notices", names)
        self.assertIn("inference", names)
        self.assertIn("set_inference", names)
        self.assertIn("set_inference_defaults", names)
        self.assertNotIn("invitation_mail", names)
        self.assertIn("deployment", names)
        self.assertNotIn("list_practices", names)
        owner_names = {
            tool["name"]
            for tool in handle_message(
                self.conn,
                self.owner,
                {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
                mcp_url=MCP_URL,
            )["result"]["tools"]
        }
        self.assertIn("list_practices", owner_names)
        self.assertIn("set_patreon_integration", owner_names)
        self.assertIn("set_practice_settings", owner_names)
        self.assertIn("author_notices", owner_names)
        self.assertIn("inference", owner_names)
        self.assertIn("set_inference", owner_names)
        self.assertIn("set_inference_defaults", owner_names)
        self.assertIn("invitation_mail", owner_names)
        self.assertIn("set_invitation_mail", owner_names)
        self.assertIn("deployment", owner_names)
        missing = handle_message(
            self.conn,
            self.editor,
            {"jsonrpc": "2.0", "id": 7, "method": "nope"},
            mcp_url=MCP_URL,
        )
        self.assertEqual(missing["error"]["code"], -32601)
        self.assertIsNone(
            handle_message(
                self.conn,
                self.editor,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                mcp_url=MCP_URL,
            )
        )

    def test_install_adds_a_custom_skill(self):
        installed = _payload(_call(self.conn, self.editor, "install_agent_files", _package(SKILL)))
        self.assertEqual(installed["entry"], "workflow.json")
        self.assertIn("skills/house-style/SKILL.md", installed["files"])
        row = self.conn.execute(
            "SELECT agent_code, agent_skills FROM personas WHERE id = ?",
            (self.persona["id"],),
        ).fetchone()
        self.assertIn("author-compendium", row["agent_code"])
        self.assertNotIn("agent.py", row["agent_code"])
        self.assertIn("Prefer the names the author already used.", row["agent_skills"])
        detail = _payload(_call(self.conn, self.editor, "get_persona"))
        self.assertEqual(detail["entry"], "workflow.json")
        self.assertNotIn("code", detail)
        self.assertEqual(detail["skills"][0]["body"], SKILL["body"])

    def test_agent_py_is_refused(self):
        text = _error(
            _call(
                self.conn,
                self.editor,
                "install_agent_files",
                {
                    "files": [
                        {"path": "manifest.json", "text": '{"harness": "python", "entry": "agent.py"}\n'},
                        {"path": "agent.py", "text": "def run(manuscript, form, skill):\n    return {}\n"},
                    ]
                },
            )
        )
        self.assertIn("a persona is a workflow.json file", text)
        text = _error(_call(self.conn, self.editor, "save_persona", {"name": "Nope", "code": "def run(manuscript, form, skill):\n    return {}\n"}))
        self.assertIn("unknown tool save_persona", text)
        kept = _payload(_call(self.conn, self.editor, "list_agent_files"))
        self.assertIn("workflow.json", kept["files"])
        self.assertNotIn("agent.py", kept["files"])

    def test_assistant_without_offers_cannot_save(self):
        aide = self._assistant("aide@example.com", {"manuscripts": True})
        text = _error(_call(self.conn, aide, "install_agent_files", _package(SKILL)))
        self.assertIn("that is not assigned to this login", text)
        who = _payload(_call(self.conn, aide, "whoami"))
        self.assertFalse(who["permissions"]["offers"])
        self.assertTrue(who["permissions"]["manuscripts"])

    def test_assistant_with_offers_saves_the_practice_persona(self):
        aide = self._assistant("offers@example.com", {"offers": True})
        saved = _payload(
            _call(
                self.conn,
                aide,
                "install_agent_files",
                {**_package(SKILL), "persona_id": self.persona["id"]},
            )
        )
        self.assertEqual(saved["persona_id"], self.persona["id"])
        self.assertIn("skills/house-style/SKILL.md", saved["files"])
        text = _error(_call(self.conn, aide, "list_manuscripts"))
        self.assertIn("that is not assigned to this login", text)

    def test_owner_without_a_practice_can_list_but_not_save(self):
        who = _payload(_call(self.conn, self.owner, "whoami"))
        self.assertEqual(who["role"], "owner")
        self.assertEqual(who["practice"]["name"], "Owner")
        self.assertIn("Owner", who["roles"])
        self.assertIn("Editor", who["roles"])
        self.assertEqual(who["mcp_url"], MCP_URL)
        practices = _payload(_call(self.conn, self.owner, "list_practices"))
        emails = {row["editor_email"] for row in practices}
        self.assertIn("editor@example.com", emails)
        self.assertIn("owner@example.com", emails)
        denied = _error(_call(self.conn, self.editor, "list_practices"))
        self.assertIn("only the platform owner can list practices", denied)

    def test_another_practices_persona_is_not_found(self):
        open_owner_practice(self.conn, self.owner["id"], "North Desk")
        text = _error(
            _call(
                self.conn,
                self.owner,
                "install_agent_files",
                {**_package(SKILL), "persona_id": self.persona["id"]},
            )
        )
        self.assertIn("persona not found", text)
        row = self.conn.execute("SELECT name FROM personas WHERE id = ?", (self.persona["id"],)).fetchone()
        self.assertNotEqual(row["name"], "Taken")

    def test_revoked_token_is_not_a_login(self):
        raw = issue_agent_token(self.conn, self.editor["id"], "laptop")
        self.assertIsNotNone(user_for_agent_token(self.conn, raw))
        token_id = list_agent_tokens(self.conn, self.editor["id"])[0]["id"]
        revoke_agent_token(self.conn, self.editor["id"], token_id)
        self.assertIsNone(user_for_agent_token(self.conn, raw))

    def test_a_workflow_without_steps_is_refused(self):
        text = _error(
            _call(
                self.conn,
                self.editor,
                "install_agent_files",
                {
                    "files": [
                        {"path": "manifest.json", "text": '{"harness": "workflow", "entry": "workflow.json"}\n'},
                        {"path": "workflow.json", "text": '{"title": "Empty", "steps": []}\n'},
                    ]
                },
            )
        )
        self.assertIn("at least one step", text)

    def test_publish_persona_makes_it_available(self):
        self.conn.execute(
            "UPDATE personas SET status = 'draft', price_cents = NULL WHERE id = ?",
            (self.persona["id"],),
        )
        self.conn.commit()
        published = _payload(_call(self.conn, self.editor, "publish_persona", {}))
        self.assertEqual(published["status"], "active")
        self.assertNotIn("price", published)

    def test_list_manuscripts_returns_metadata_only(self):
        author = author_for_user(self.conn, self.seeded["author_user_id"])
        prose = "The hidden river sentence should not leave the desk."
        key = f"manuscripts/{author['organization_id']}/{self.seeded['editor_id']}/{author['id']}/story.txt"
        write_manuscript(key, prose)
        create_job(
            self.conn,
            author,
            persona_id=self.persona["id"],
            title="After the river",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="The river does not return.",
            focus="",
            manuscript=prose,
            object_key=key,
            digest="hidden",
        )
        rows = _payload(_call(self.conn, self.editor, "list_manuscripts"))
        packed = json.dumps(rows)
        self.assertEqual(rows[0]["title"], "After the river")
        self.assertEqual(rows[0]["author_email"], "bea@example.com")
        self.assertNotIn(prose, packed)
        self.assertNotIn("The river does not return.", packed)

    def test_authors_cannot_mint_a_token(self):
        with self.assertRaises(ValueError) as caught:
            issue_agent_token(self.conn, self.seeded["author_user_id"], "laptop")
        self.assertIn("authors do not connect a local agent", str(caught.exception))

    def test_workloads_include_the_manuscript_for_this_practice_only(self):
        job_id, prose, _author = self._queue("After the river")
        self.assertEqual(dev_capture(self.conn, job_id, _author["id"]), "queued")
        listed = _payload(_call(self.conn, self.editor, "list_workloads"))
        self.assertEqual(listed[0]["id"], job_id)
        self.assertNotIn(prose, json.dumps(listed))
        detail = _payload(_call(self.conn, self.editor, "get_workload", {"workload_id": job_id}))
        self.assertEqual(detail["manuscript"], prose)
        self.assertEqual(detail["do_not_change"], "The river does not return.")
        self.assertIn("messages", detail)
        listed = _payload(_call(self.conn, self.owner, "list_workloads"))
        self.assertNotIn(job_id, [row["id"] for row in listed])
        missing = _error(_call(self.conn, self.owner, "get_workload", {"workload_id": job_id}))
        self.assertIn("workload not found", missing)
        self.assertNotIn(prose, missing)

    def test_run_workload_stays_local(self):
        job_id, _prose, author = self._queue("After the river")
        self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
        config.XAI_MODE = "sync"
        config.XAI_API_KEY = "not-a-real-key"
        config.XAI_BASE_URL = "http://127.0.0.1:9"
        try:
            ran = _payload(_call(self.conn, self.editor, "run_workload", {"workload_id": job_id}))
        finally:
            config.XAI_MODE = "local"
            config.XAI_API_KEY = ""
            config.XAI_BASE_URL = "https://api.x.ai/v1"
        self.assertEqual(ran["status"], "ready")
        self.assertTrue(ran["local"])
        letter = next(item["text"] for item in ran["documents"] if item["kind"] == "letter")
        self.assertIn("After the river", letter)
        again = _error(_call(self.conn, self.editor, "run_workload", {"workload_id": job_id}))
        self.assertIn("not queued", again)

    def test_demo_run_does_not_call_the_model(self):
        job_id, _prose, author = self._queue("Demo road")
        self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
        config.DEMO_MODE = True
        try:
            ran = _payload(_call(self.conn, self.editor, "run_workload", {"workload_id": job_id}))
        finally:
            config.DEMO_MODE = False
        self.assertEqual(ran["status"], "ready")
        letter = next(item["text"] for item in ran["documents"] if item["kind"] == "letter")
        self.assertIn("did not run", letter)

    def test_assistant_permissions_gate_workloads_and_messages(self):
        job_id, prose, _author = self._queue("After the river")
        reader = self._assistant("reader@example.com", {"manuscripts": True})
        detail = _payload(_call(self.conn, reader, "get_workload", {"workload_id": job_id}))
        self.assertEqual(detail["manuscript"], prose)
        self.assertNotIn("messages", detail)
        for name, arguments in (
            ("send_message", {"workload_id": job_id, "body": "Hello"}),
            ("search_manuscripts", {}),
            ("model_cost", {}),
            ("list_assistants", {}),
        ):
            text = _error(_call(self.conn, reader, name, arguments))
            self.assertIn("that is not assigned to this login", text)
        mail = self._assistant("mail@example.com", {"messages": True})
        sent = _payload(
            _call(self.conn, mail, "send_message", {"workload_id": job_id, "body": "I read the opening."})
        )
        self.assertEqual(sent["messages"][-1]["body"], "I read the opening.")
        self.assertEqual(sent["messages"][-1]["sender"], "editor")
        hidden = _error(_call(self.conn, mail, "get_workload", {"workload_id": job_id}))
        self.assertIn("that is not assigned to this login", hidden)
        blocked = _error(_call(self.conn, mail, "run_workload", {"workload_id": job_id}))
        self.assertIn("that is not assigned to this login", blocked)

    def test_pipeline_payouts_and_assistants(self):
        job_id, _prose, author = self._queue("Human edit")
        before = _payload(
            _call(self.conn, self.editor, "search_manuscripts", {"human_requested": True})
        )
        self.assertNotIn(job_id, [row["id"] for row in before])
        request_human(self.conn, author["id"], job_id)
        asked = _payload(
            _call(self.conn, self.editor, "search_manuscripts", {"human_requested": True})
        )
        match = next(item for item in asked if item["id"] == job_id)
        self.assertEqual(match["title"], "Human edit")
        self.assertTrue(match["human_requested"])
        fantasy = _payload(_call(self.conn, self.editor, "search_manuscripts", {"genre": "fantasy"}))
        self.assertIn(job_id, [row["id"] for row in fantasy])
        job = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "queued")
        cost = _payload(_call(self.conn, self.editor, "model_cost"))
        self.assertEqual(cost["model_cost"], "$0")
        invited = _payload(
            _call(
                self.conn,
                self.editor,
                "invite_assistant",
                {"email": "new@example.com", "permissions": {"manuscripts": True}},
            )
        )
        self.assertTrue(invited["invite_path"].startswith("/invite/"))
        accept_editor_invite(self.conn, invited["invite_path"].rsplit("/", 1)[-1], "Nell")
        pending = _payload(
            _call(self.conn, self.editor, "invite_assistant", {"email": "later@example.com"})
        )
        self.assertTrue(pending["invite_path"].startswith("/invite/"))
        roster = _payload(_call(self.conn, self.editor, "list_assistants"))
        aide = next(row for row in roster["assistants"] if row["email"] == "new@example.com")
        self.assertTrue(aide["permissions"]["manuscripts"])
        self.assertFalse(aide["permissions"]["offers"])
        invite_id = next(row["id"] for row in roster["invites"] if row["email"] == "later@example.com")
        changed = _payload(
            _call(
                self.conn,
                self.editor,
                "set_assistant_permissions",
                {"editor_id": aide["id"], "permissions": {"messages": True}},
            )
        )
        aide = next(row for row in changed["assistants"] if row["email"] == "new@example.com")
        self.assertTrue(aide["permissions"]["messages"])
        self.assertFalse(aide["permissions"]["manuscripts"])
        revoked = _payload(
            _call(self.conn, self.editor, "revoke_assistant_invite", {"invite_id": invite_id})
        )
        self.assertFalse(any(row["email"] == "later@example.com" for row in revoked["invites"]))
        removed = _payload(_call(self.conn, self.editor, "remove_assistant", {"editor_id": aide["id"]}))
        self.assertFalse(any(row["email"] == "new@example.com" for row in removed["assistants"]))

    def test_the_practice_editor_connects_their_own_patreon_client(self):
        aide = self._assistant("aide@example.com", {"manuscripts": True, "assistants": True})
        aide_names = {
            tool["name"]
            for tool in handle_message(
                self.conn,
                aide,
                {"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}},
                mcp_url=MCP_URL,
            )["result"]["tools"]
        }
        self.assertNotIn("patreon_integration", aide_names)
        self.assertNotIn("set_patreon_integration", aide_names)
        self.assertNotIn("practice_settings", aide_names)
        self.assertNotIn("set_practice_settings", aide_names)
        refused = _error(
            _call(
                self.conn,
                aide,
                "set_patreon_integration",
                {"campaign_id": "12345", "client_id": "client", "client_secret": "nope"},
            )
        )
        self.assertIn("only the editor can connect Patreon", refused)
        untouched = self.conn.execute(
            "SELECT patreon_client_secret FROM editors WHERE id = ?",
            (self.seeded["editor_id"],),
        ).fetchone()["patreon_client_secret"]
        self.assertEqual(untouched, "")

        current = _payload(_call(self.conn, self.editor, "patreon_integration"))
        self.assertEqual(current["status"], "Not connected")
        self.assertEqual(current["redirect_uri"], "http://127.0.0.1:8080/patreon/callback")
        self.assertEqual(current["webhook_url"], "http://127.0.0.1:8080/patreon/webhook")
        self.assertEqual(current["client_secret"], "not saved")

        bad = _error(
            _call(
                self.conn,
                self.editor,
                "set_patreon_integration",
                {"campaign_id": "12ab", "client_id": "ada-client", "client_secret": "editor-secret-value"},
            )
        )
        self.assertEqual(bad, "a Patreon campaign id is required")
        missing = _error(
            _call(
                self.conn,
                self.editor,
                "set_patreon_integration",
                {"campaign_id": "12345", "client_id": "ada-client"},
            )
        )
        self.assertEqual(missing, "a Patreon client secret is required")

        self.conn.execute(
            "UPDATE authors SET membership_checked_at = ? WHERE editor_id = ?",
            ("2026-11-18T00:00:00+00:00", self.seeded["editor_id"]),
        )
        self.conn.commit()
        saved = _payload(
            _call(
                self.conn,
                self.editor,
                "set_patreon_integration",
                {
                    "campaign_id": "12345",
                    "client_id": "ada-client",
                    "client_secret": "editor-secret-value",
                    "webhook_secret": "editor-hook-value",
                },
            )
        )
        packed = json.dumps(saved)
        self.assertNotIn("editor-secret-value", packed)
        self.assertNotIn("editor-hook-value", packed)
        self.assertEqual(saved["status"], "Connected")
        self.assertEqual(saved["campaign_id"], "12345")
        self.assertEqual(saved["client_id"], "ada-client")
        self.assertEqual(saved["client_secret"], "saved")
        self.assertEqual(saved["webhook_secret"], "saved")
        row = self.conn.execute(
            """
            SELECT patreon_client_secret, patreon_webhook_secret
            FROM editors WHERE id = ?
            """,
            (self.seeded["editor_id"],),
        ).fetchone()
        self.assertEqual(row["patreon_client_secret"], "editor-secret-value")
        self.assertEqual(row["patreon_webhook_secret"], "editor-hook-value")
        checked = self.conn.execute(
            "SELECT membership_checked_at FROM authors WHERE editor_id = ?",
            (self.seeded["editor_id"],),
        ).fetchone()["membership_checked_at"]
        self.assertEqual(checked, "")

        kept = _payload(
            _call(
                self.conn,
                self.editor,
                "set_patreon_integration",
                {"campaign_id": "54321", "client_id": "ada-client"},
            )
        )
        self.assertEqual(kept["campaign_id"], "54321")
        self.assertEqual(kept["client_secret"], "saved")
        self.assertNotIn("editor-secret-value", json.dumps(kept))
        row = self.conn.execute(
            "SELECT patreon_campaign_id, patreon_client_secret FROM editors WHERE id = ?",
            (self.seeded["editor_id"],),
        ).fetchone()
        self.assertEqual(row["patreon_campaign_id"], "54321")
        self.assertEqual(row["patreon_client_secret"], "editor-secret-value")

        owner = _payload(
            _call(
                self.conn,
                self.owner,
                "set_patreon_integration",
                {
                    "campaign_id": "999",
                    "client_id": "owner-client",
                    "client_secret": "owner-secret-value",
                },
            )
        )
        self.assertEqual(owner["status"], "Connected")
        self.assertEqual(owner["campaign_id"], "999")
        self.assertNotIn("owner-secret-value", json.dumps(owner))
        ada = self.conn.execute(
            "SELECT patreon_campaign_id FROM editors WHERE id = ?",
            (self.seeded["editor_id"],),
        ).fetchone()["patreon_campaign_id"]
        self.assertEqual(ada, "54321")

    def test_the_practice_editor_sets_name_and_colors(self):
        aide = self._assistant("aide@example.com", {"manuscripts": True})
        aide_names = {
            tool["name"]
            for tool in handle_message(
                self.conn,
                aide,
                {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}},
                mcp_url=MCP_URL,
            )["result"]["tools"]
        }
        self.assertNotIn("practice_settings", aide_names)
        self.assertNotIn("set_practice_settings", aide_names)
        self.assertNotIn("author_notices", aide_names)
        self.assertNotIn("set_author_notices", aide_names)
        self.assertNotIn("inference", aide_names)
        self.assertNotIn("set_inference", aide_names)
        self.assertNotIn("set_inference_defaults", aide_names)
        self.assertNotIn("deployment", aide_names)
        refused = _error(_call(self.conn, aide, "set_practice_settings", {"mode": "dark"}))
        self.assertEqual(refused, "only the editor can change settings")
        refused = _error(_call(self.conn, aide, "practice_settings", {}))
        self.assertEqual(refused, "only the editor can change settings")
        refused = _error(_call(self.conn, aide, "set_inference", {"label": "Desk", "provider": "ollama"}))
        self.assertEqual(refused, "only the editor can change settings")
        refused = _error(_call(self.conn, aide, "set_inference_defaults", {"models": ["grok-4.3"]}))
        self.assertEqual(refused, "only the editor can change settings")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM inference_defaults").fetchone()["n"], 0)

        current = _payload(_call(self.conn, self.editor, "practice_settings"))
        self.assertEqual(current["name"], "Ada")
        self.assertEqual(
            current["appearance"],
            {"profile": "desk", "mode": "light", "accent": "", "paper": "", "ink": ""},
        )
        self.assertEqual(current["patreon"]["status"], "Not connected")
        self.assertEqual(current["patreon"]["client_secret"], "not saved")

        empty = _error(_call(self.conn, self.editor, "set_practice_settings", {}))
        self.assertEqual(empty, "nothing to save")
        bad_type = _error(_call(self.conn, self.editor, "set_practice_settings", {"profile": 1}))
        self.assertEqual(bad_type, "a theme is required")

        forest = _payload(
            _call(self.conn, self.editor, "set_practice_settings", {"profile": "forest", "mode": "dark"})
        )
        self.assertEqual(forest["appearance"]["profile"], "forest")
        self.assertEqual(forest["appearance"]["mode"], "dark")
        self.assertEqual(forest["appearance"]["accent"], "")
        self.assertEqual(forest["name"], "Ada")

        kept = _payload(_call(self.conn, self.editor, "set_practice_settings", {"name": "Ada Desk"}))
        self.assertEqual(kept["name"], "Ada Desk")
        self.assertEqual(kept["appearance"]["profile"], "forest")
        self.assertEqual(kept["appearance"]["mode"], "dark")

        invalid = _error(
            _call(
                self.conn,
                self.editor,
                "set_practice_settings",
                {"profile": "custom", "accent": "blue", "paper": "#010203", "ink": "#fefefe"},
            )
        )
        self.assertEqual(invalid, "a accent color is required")
        still = _payload(_call(self.conn, self.editor, "practice_settings"))
        self.assertEqual(still["appearance"]["profile"], "forest")

        custom = _payload(
            _call(
                self.conn,
                self.editor,
                "set_practice_settings",
                {
                    "profile": "custom",
                    "accent": "#112233",
                    "paper": "#010203",
                    "ink": "#fefefe",
                },
            )
        )
        self.assertEqual(custom["appearance"]["profile"], "custom")
        self.assertEqual(custom["appearance"]["mode"], "dark")
        self.assertEqual(custom["appearance"]["accent"], "#112233")
        self.assertEqual(custom["appearance"]["paper"], "#010203")
        self.assertEqual(custom["appearance"]["ink"], "#fefefe")
        self.assertEqual(custom["patreon"]["client_secret"], "not saved")

        owner = _payload(
            _call(self.conn, self.owner, "set_practice_settings", {"profile": "ink", "mode": "light"})
        )
        self.assertEqual(owner["appearance"]["profile"], "ink")
        self.assertEqual(owner["appearance"]["mode"], "light")
        ada = _payload(_call(self.conn, self.editor, "practice_settings"))
        self.assertEqual(ada["name"], "Ada Desk")
        self.assertEqual(ada["appearance"]["profile"], "custom")
        self.assertEqual(ada["appearance"]["paper"], "#010203")

    def test_deployment_names_what_this_process_still_needs(self):
        aide = self._assistant("aide@example.com", {"manuscripts": True})
        self.assertEqual(
            _error(_call(self.conn, aide, "deployment", {})),
            "only the editor can change settings",
        )
        previous = (
            config.DEV_LOGIN,
            config.DEMO_MODE,
            config.TRUST_PROXY,
            config.TLS_CERT,
            config.TLS_KEY,
            config.ACME_DOMAIN,
            config.ACME_EMAIL,
            config.XAI_MODE,
            config.XAI_API_KEY,
            config.XAI_BASE_URL,
            config.XAI_MODEL,
            config.LLM_PROVIDER,
            config.LLM_MODE,
            config.LLM_MODEL,
            config.LLM_API_KEY,
            config.LLM_BASE_URL,
            config.HOST,
            config.PATREON_REDIRECT_URI,
        )
        try:
            config.DEV_LOGIN = True
            config.DEMO_MODE = False
            config.TRUST_PROXY = False
            config.TLS_CERT = ""
            config.TLS_KEY = ""
            config.ACME_DOMAIN = ""
            config.ACME_EMAIL = ""
            config.XAI_MODE = "local"
            config.XAI_API_KEY = ""
            config.XAI_BASE_URL = "https://api.x.ai/v1"
            config.XAI_MODEL = "grok-4.3"
            config.LLM_PROVIDER = "xai"
            config.LLM_MODE = ""
            config.LLM_MODEL = ""
            config.LLM_API_KEY = ""
            config.LLM_BASE_URL = ""
            config.HOST = "127.0.0.1"
            config.PATREON_REDIRECT_URI = ""
            report = _payload(_call(self.conn, self.editor, "deployment"))
            self.assertEqual(report["guide"], "README.md Deploy")
            self.assertEqual(report["not_this_build"], ["local-docs/"])
            self.assertEqual(report["processes"]["work_runs"], "one queued manuscript, then exits")
            self.assertEqual(report["this_process"]["https"], "plain")
            self.assertFalse(report["this_process"]["xai_key_set"])
            text = " ".join(report["still_needed"])
            self.assertIn("DEV_LOGIN is on.", text)
            self.assertIn("plain HTTP", text)
            self.assertIn("not calling the model", text)
            self.assertIn("has not connected Patreon", text)
            blob = json.dumps(report).lower()
            self.assertNotIn("dev-secret-change-me", blob)
            self.assertFalse(hasattr(config, "SECRET"))
            for word in ("protected", "encrypted", "encryption", "cipher"):
                self.assertNotIn(word, blob)

            config.DEV_LOGIN = False
            config.TLS_CERT = "/tmp/cert.pem"
            config.TLS_KEY = "/tmp/key.pem"
            config.XAI_MODE = "sync"
            config.XAI_API_KEY = "test-key-value"
            config.HOST = "0.0.0.0"
            ready = _payload(_call(self.conn, self.editor, "deployment"))
            self.assertEqual(ready["this_process"]["https"], "tls_files")
            self.assertTrue(ready["this_process"]["xai_key_set"])
            self.assertEqual(
                ready["still_needed"],
                [
                    "This practice has not connected Patreon. After the public URL is HTTPS, "
                    "patreon_integration shows the redirect URI and the webhook URL."
                ],
            )
            self.assertNotIn("test-key-value", json.dumps(ready))

            config.TLS_CERT = ""
            config.TLS_KEY = ""
            config.ACME_DOMAIN = "desk.example.com"
            config.XAI_BASE_URL = "https://us.api.x.ai/v1"
            acme = _payload(_call(self.conn, self.editor, "deployment"))
            self.assertEqual(acme["this_process"]["https"], "acme")
            acme_text = " ".join(acme["still_needed"])
            self.assertIn("ACME_EMAIL is required", acme_text)
            self.assertIn("US regional host", acme_text)
            self.assertNotIn("plain HTTP", acme_text)

            config.LLM_PROVIDER = "ollama"
            config.LLM_MODE = "sync"
            config.LLM_MODEL = "llama3.2"
            config.XAI_API_KEY = ""
            config.XAI_MODEL = "other"
            local = _payload(_call(self.conn, self.editor, "deployment"))
            self.assertEqual(local["this_process"]["llm_provider"], "ollama")
            self.assertEqual(local["this_process"]["llm_model"], "llama3.2")
            self.assertFalse(local["this_process"]["llm_key_set"])
            local_text = " ".join(local["still_needed"])
            self.assertNotIn("grok-4.3", local_text)
            self.assertNotIn("US regional host", local_text)
            self.assertNotIn("not calling the model", local_text)
        finally:
            (
                config.DEV_LOGIN,
                config.DEMO_MODE,
                config.TRUST_PROXY,
                config.TLS_CERT,
                config.TLS_KEY,
                config.ACME_DOMAIN,
                config.ACME_EMAIL,
                config.XAI_MODE,
                config.XAI_API_KEY,
                config.XAI_BASE_URL,
                config.XAI_MODEL,
                config.LLM_PROVIDER,
                config.LLM_MODE,
                config.LLM_MODEL,
                config.LLM_API_KEY,
                config.LLM_BASE_URL,
                config.HOST,
                config.PATREON_REDIRECT_URI,
            ) = previous

    def test_install_agent_files_writes_the_package(self):
        from app.harness import default_workflow

        installed = _payload(
            _call(
                self.conn,
                self.editor,
                "install_agent_files",
                {
                    "files": [
                        {
                            "path": "manifest.json",
                            "text": '{"harness": "workflow", "entry": "workflow.json"}\n',
                        },
                        {"path": "workflow.json", "text": json.dumps(default_workflow())},
                        {"path": "skills/house-style/SKILL.md", "text": "Prefer the names.\n"},
                        {"path": "skills/house-style/references/names.md", "text": "Keep them.\n"},
                    ]
                },
            )
        )
        self.assertEqual(installed["harness"], "workflow")
        self.assertIn("workflow.json", installed["files"])
        self.assertIn("skills/house-style/references/names.md", installed["files"])
        detail = _payload(_call(self.conn, self.editor, "get_persona"))
        self.assertEqual(detail["harness"], "workflow")
        self.assertEqual(detail["entry"], "workflow.json")
        self.assertNotIn("code", detail)
        stored = self.conn.execute(
            "SELECT agent_code FROM personas WHERE id = ?",
            (self.persona["id"],),
        ).fetchone()
        self.assertIn("author-compendium", stored["agent_code"])
        refused = _error(
            _call(
                self.conn,
                self.editor,
                "write_agent_file",
                {"path": "../secret.txt", "text": "nope"},
            )
        )
        self.assertIn("inside the agent", refused)
        kept = _payload(_call(self.conn, self.editor, "list_agent_files"))
        self.assertEqual(kept["harness"], "workflow")

    def test_mail_tools_check_the_account_and_hide_the_secret(self):
        from unittest.mock import patch

        from app import mail

        aide = self._assistant("aide-mail@example.com", {"manuscripts": True})
        self.assertEqual(
            _error(_call(self.conn, aide, "author_notices")),
            "only the editor can change settings",
        )
        self.assertEqual(
            _error(
                _call(
                    self.conn,
                    self.editor,
                    "set_invitation_mail",
                    {
                        "transport": "https",
                        "from_address": "ada@example.com",
                        "https_provider": "resend",
                        "secret": "nope",
                    },
                )
            ),
            "only the owner can change invitation mail",
        )
        seen = []

        def probe(account):
            seen.append(account["secret"])

        with patch("app.mail.probe", probe):
            saved = _payload(
                _call(
                    self.conn,
                    self.editor,
                    "set_author_notices",
                    {
                        "transport": "https",
                        "from_address": "ada@example.com",
                        "https_provider": "resend",
                        "secret": "re_secret_value",
                    },
                )
            )
        self.assertEqual(saved["secret"], "saved")
        self.assertEqual(saved["from_address"], "ada@example.com")
        self.assertEqual(saved["notice_origin"], "http://127.0.0.1:8080")
        self.assertNotIn("re_secret_value", json.dumps(saved))
        with patch("app.mail.probe", probe):
            _payload(
                _call(
                    self.conn,
                    self.editor,
                    "set_author_notices",
                    {
                        "transport": "https",
                        "from_address": "ada@example.com",
                        "https_provider": "resend",
                        "secret": "",
                    },
                )
            )
        self.assertEqual(seen[-1], "re_secret_value")
        with patch("app.mail.probe", side_effect=mail.MailError(mail.API_AUTH)):
            refused = _error(
                _call(
                    self.conn,
                    self.editor,
                    "set_author_notices",
                    {
                        "transport": "https",
                        "from_address": "other@example.com",
                        "https_provider": "postmark",
                        "secret": "other-key",
                    },
                )
            )
        self.assertEqual(refused, mail.API_AUTH)
        still = _payload(_call(self.conn, self.editor, "author_notices"))
        self.assertEqual(still["from_address"], "ada@example.com")
        self.assertEqual(still["https_provider"], "resend")
        self.assertNotIn("other-key", json.dumps(still))
        with patch("app.mail.probe", probe):
            invited = _payload(
                _call(
                    self.conn,
                    self.owner,
                    "set_invitation_mail",
                    {
                        "transport": "https",
                        "from_address": "owner@example.com",
                        "https_provider": "sendgrid",
                        "secret": "sg_owner_secret",
                    },
                )
            )
        self.assertEqual(invited["https_provider"], "sendgrid")
        self.assertNotIn("sg_owner_secret", json.dumps(invited))
        ada = _payload(_call(self.conn, self.editor, "author_notices"))
        self.assertEqual(ada["https_provider"], "resend")
        with patch("app.mail.probe", lambda account: None):
            cleared = _payload(
                _call(self.conn, self.editor, "set_author_notices", {"remove": True})
            )
        self.assertFalse(cleared["configured"])
        self.assertEqual(cleared["secret"], "not saved")
        owner_mail = _payload(_call(self.conn, self.owner, "invitation_mail"))
        self.assertEqual(owner_mail["from_address"], "owner@example.com")

    def _queue(self, title: str):
        author = author_for_user(self.conn, self.seeded["author_user_id"])
        prose = f"{title}. The city kept its lights on after the river left. " * 20
        key = f"manuscripts/{author['organization_id']}/{self.seeded['editor_id']}/{author['id']}/{title}.txt"
        write_manuscript(key, prose)
        job_id = create_job(
            self.conn,
            author,
            persona_id=self.persona["id"],
            title=title,
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="The river does not return.",
            focus="",
            manuscript=prose,
            object_key=key,
            digest=title,
        )
        return job_id, prose, author

    def _assistant(self, email: str, flags: dict):
        editor = editor_for_user(self.conn, self.seeded["editor_user_id"])
        raw = invite_editor(self.conn, self.seeded["editor_user_id"], email, editor["organization_id"], flags)
        user_id = accept_editor_invite(self.conn, raw, "Aide")
        return _user(self.conn, user_id)


def csrf_of(page: str) -> str:
    match = re.search(r"name=['\"]csrf['\"] value=['\"]([^'\"]+)", page)
    if match is None:
        raise AssertionError(page[:800])
    return match.group(1)


class Client:
    def __init__(self, port: int):
        self.port = port
        self.sid = ""

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None, body: bytes | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = dict(headers or {})
        if self.sid and "Cookie" not in sent:
            sent["Cookie"] = f"sid={self.sid}"
        if form is not None:
            body = urlencode(form).encode()
            sent["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body, sent)
        resp = conn.getresponse()
        raw = resp.read()
        cookie = resp.getheader("Set-Cookie") or ""
        if "sid=" in cookie:
            self.sid = cookie.split("sid=", 1)[1].split(";", 1)[0]
        return resp.status, resp.getheader("Location"), raw, resp

    def follow(self, method: str, path: str, form: dict | None = None):
        status, loc, raw, _resp = self.request(method, path, form)
        if status == 303:
            status, _loc, raw, _resp = self.request("GET", loc)
        return status, raw.decode()

    def rpc(self, payload, token: str = "", *, cookie: bool = False):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if not cookie:
            headers["Cookie"] = ""
        status, _loc, raw, resp = self.request(
            "POST",
            "/mcp",
            headers=headers,
            body=json.dumps(payload).encode(),
        )
        return status, raw, resp


class McpHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-mcp-http-"))
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

    def sign_in(self, email: str) -> tuple[Client, str, str]:
        client = Client(self.port)
        status, page = client.follow("POST", "/dev/login", {"email": email})
        self.assertEqual(status, 200, page[:500])
        return client, page, csrf_of(page)

    def test_mcp_requires_a_bearer_and_ignores_the_cookie(self):
        client, _page, _csrf = self.sign_in("owner@example.com")
        status, raw, resp = client.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, cookie=True)
        self.assertEqual(status, 401)
        self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
        self.assertIn("a token is required", raw.decode())
        status, raw, _resp = client.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 401)
        self.assertIn("a token is required", raw.decode())

    def test_bearer_lists_tools_and_a_notification_is_accepted(self):
        client, _page, csrf = self.sign_in("owner@example.com")
        status, page = client.follow("POST", "/agent-tokens", {"csrf": csrf, "label": "laptop", "next": "/"})
        self.assertEqual(status, 200, page[:500])
        secret = re.search(r"<pre>([^<]+)</pre>", page).group(1)
        status, raw, _resp = client.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, secret)
        self.assertEqual(status, 200, raw[:500])
        names = {tool["name"] for tool in json.loads(raw)["result"]["tools"]}
        self.assertIn("install_agent_files", names)
        self.assertIn("set_patreon_integration", names)
        self.assertIn("practice_settings", names)
        self.assertIn("set_practice_settings", names)
        self.assertIn("author_notices", names)
        self.assertIn("set_author_notices", names)
        self.assertIn("inference", names)
        self.assertIn("set_inference", names)
        self.assertIn("set_inference_defaults", names)
        self.assertIn("invitation_mail", names)
        self.assertIn("deployment", names)
        self.assertNotIn("save_persona", names)
        status, raw, _resp = client.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, secret)
        self.assertEqual(status, 202)
        self.assertEqual(raw, b"")

    def test_get_mcp_is_not_the_call(self):
        status, _loc, raw, resp = Client(self.port).request("GET", "/mcp")
        self.assertEqual(status, 405)
        self.assertIn("use POST", raw.decode())
        self.assertEqual(resp.getheader("Allow"), "POST")

    def test_skill_download_is_the_desk_persona_file(self):
        status, _loc, raw, resp = Client(self.port).request("GET", "/skills/desk-persona")
        self.assertEqual(status, 200)
        self.assertEqual(raw, config.DESK_PERSONA_SKILL.read_bytes())
        text = raw.decode()
        self.assertIn("name: desk-persona", text)
        self.assertIn("workflow.json", text)
        self.assertIn("deployment", text)
        self.assertIn("README.md", text)
        self.assertIn("set_author_notices", text)
        self.assertIn("set_inference", text)
        self.assertIn("set_inference_defaults", text)
        self.assertIn("set_invitation_mail", text)
        self.assertNotIn("or mail,", text)
        self.assertNotIn("save_persona", text)
        self.assertIn("text/markdown", resp.getheader("Content-Type"))
        self.assertIn("SKILL.md", resp.getheader("Content-Disposition"))

    def test_token_is_shown_once_and_revoke_closes_mcp(self):
        client, _page, csrf = self.sign_in("owner@example.com")
        status, page = client.follow("POST", "/agent-tokens", {"csrf": csrf, "label": "laptop", "next": "/"})
        self.assertEqual(status, 200)
        secret = re.search(r"<pre>([^<]+)</pre>", page).group(1)
        status, desk = client.follow("GET", "/local-agent")
        self.assertEqual(status, 200)
        self.assertNotIn(secret, desk)
        self.assertIn("laptop", desk)
        token_id = re.search(r"action='/agent-tokens/([^']+)/revoke'", desk).group(1)
        status, raw, _resp = client.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}, secret)
        self.assertEqual(status, 200, raw[:300])
        status, _page = client.follow(
            "POST",
            f"/agent-tokens/{token_id}/revoke",
            {"csrf": csrf_of(desk), "next": "/"},
        )
        self.assertEqual(status, 200)
        status, raw, _resp = client.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}, secret)
        self.assertEqual(status, 401)
        self.assertIn("token is not valid", raw.decode())
