import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("XAI_MODE", "local")

from app import config  # noqa: E402
from app.agent import (  # noqa: E402
    DEFAULT_AGENT,
    estimate_agent_words,
    execute,
    static_calls,
    validate_agent,
)
from app.db import open_db  # noqa: E402
from app.harness import default_workflow, gate_source  # noqa: E402
from app.services import (  # noqa: E402
    accept_editor_invite,
    author_for_user,
    become_customer,
    create_job,
    create_owner,
    dev_capture,
    editor_for_user,
    install_persona_files,
    invite_editor,
    publish_persona,
    write_manuscript,
)
from app.worker import run_once  # noqa: E402


CODE_ONLY = """def run(manuscript, form, skill):
    return {"letter": "Noted in the margin.", "notes": "The river stays."}
"""

HOUSE = """def run(manuscript, form, skill):
    reply = skill("house-style", "Name one constraint.", name="house")
    return {"letter": reply}
"""

LOOPED = """def run(manuscript, form, skill):
    n = 0
    while n < 100000:
        n = n + 1
    return {"letter": "done"}
"""


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-agent-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEMO_MODE = False
        self.conn = open_db(config.DATABASE_PATH)

    def tearDown(self):
        self.conn.close()

    def test_default_agent_matches_the_letter_shape(self):
        validate_agent(DEFAULT_AGENT, [])
        calls = {(item["skill"], item["call"]) for item in static_calls(DEFAULT_AGENT)}
        self.assertIn(("author-compendium", "compendium"), calls)
        for name in ("promise", "plot", "character", "stakes", "focus"):
            self.assertIn(("author-dev", name), calls)
        self.assertIn(("author-readback", "readback"), calls)

        def skill_fn(skill_name, ask, attachment="", name=""):
            if name == "compendium":
                return "- LOCKED: The river does not return.\n- DRAFT: The city.\n"
            if name == "readback":
                return "- DRAFT: read back\n"
            if name == "focus":
                return "focus\n"
            return f"diagnosis {name}\n"

        docs = execute(DEFAULT_AGENT, [], "The city kept its lights on.", {"focus": ""}, skill_fn)
        self.assertIn("## Promise and genre fit", docs["letter"])
        self.assertIn("## Plot and structure", docs["letter"])
        self.assertIn("## Character", docs["letter"])
        self.assertIn("## Stakes and pacing", docs["letter"])
        self.assertNotIn("## Author focus", docs["letter"])
        self.assertIn("LOCKED: The river does not return.", docs["compendium"])
        self.assertIn("DRAFT:", docs["readback"])

    def test_import_open_and_private_names_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_agent("import os\ndef run(manuscript, form, skill):\n    return {}\n", [])
        with self.assertRaises(ValueError):
            validate_agent(
                "def run(manuscript, form, skill):\n    open('x')\n    return {}\n",
                [],
            )
        with self.assertRaises(ValueError):
            validate_agent(
                "def run(manuscript, form, skill):\n    return manuscript.__class__\n",
                [],
            )
        with self.assertRaises(ValueError):
            validate_agent(
                'def run(manuscript, form, skill):\n    skill("nope", "x")\n    return {}\n',
                [],
            )

    def test_code_only_agent_does_not_call_a_skill(self):
        seen = []

        def skill_fn(*args, **kwargs):
            seen.append(args)
            return "no"

        docs = execute(CODE_ONLY, [], "The city.", {}, skill_fn)
        self.assertEqual(docs["letter"], "Noted in the margin.\n")
        self.assertEqual(docs["notes"], "The river stays.\n")
        self.assertEqual(seen, [])

    def test_custom_skill_name_reaches_the_callback(self):
        captured = {}

        def skill_fn(skill_name, ask, attachment="", name=""):
            captured["skill"] = skill_name
            captured["ask"] = ask
            return "yes"

        skills = [{"name": "house-style", "body": "# House style\nKeep the names.\n"}]
        validate_agent(HOUSE, skills)
        docs = execute(HOUSE, skills, "ms", {}, skill_fn)
        self.assertEqual(captured["skill"], "house-style")
        self.assertEqual(captured["ask"], "Name one constraint.")
        self.assertEqual(docs["letter"], "yes\n")

    def test_a_long_loop_is_stopped(self):
        with self.assertRaises(RuntimeError):
            execute(LOOPED, [], "ms", {}, lambda *args, **kwargs: "")

    def test_a_call_inside_a_loop_counts_as_eight(self):
        code = (
            "def run(manuscript, form, skill):\n"
            "    for part in manuscript.split():\n"
            "        skill('author-dev', 'Diagnose this part.', name='promise')\n"
            "    return {'letter': 'done'}\n"
        )
        calls = static_calls(code)
        self.assertEqual(calls[0]["count"], 8)

    def test_default_estimate_matches_the_workflow_call_count(self):
        self.assertEqual(
            estimate_agent_words(180_000, DEFAULT_AGENT)["cost_usd_micros"],
            estimate_agent_words(180_000, gate_source(default_workflow()))["cost_usd_micros"],
        )

    def test_worker_runs_a_custom_skill_step(self):
        ada, author, persona = self._desk()
        spec = {
            "title": "House",
            "steps": [
                {
                    "name": "house",
                    "skill": "house-style",
                    "ask": "Name one constraint.",
                    "produces": "letter",
                }
            ],
        }
        install_persona_files(
            self.conn,
            ada,
            persona["id"],
            {
                "manifest.json": '{"harness": "workflow", "entry": "workflow.json"}\n',
                "workflow.json": json.dumps(spec) + "\n",
                "skills/house-style/SKILL.md": "# House style\nKeep the river.\n",
            },
        )
        publish_persona(self.conn, ada, persona["id"])
        house_id = self._queue(ada, author, persona["id"], "house")
        import app.worker as worker

        seen = []
        original = worker.build_prompt

        def spy(**kwargs):
            seen.append(kwargs)
            return original(**kwargs)

        worker.build_prompt = spy
        try:
            self.assertEqual(run_once(config.DATABASE_PATH), house_id)
        finally:
            worker.build_prompt = original
        self.assertTrue(any(item.get("skill_body") and "Keep the river." in item["skill_body"] for item in seen))
        house = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (house_id,)).fetchone()
        self.assertEqual(house["status"], "ready")
        names = [
            row["call_name"]
            for row in self.conn.execute("SELECT call_name FROM provider_calls WHERE job_id = ?", (house_id,))
        ]
        self.assertEqual(names, ["house"])

    def test_old_artifact_check_accepts_agent_documents(self):
        path = self.tmp / "old.sqlite"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE users (id text)")
        raw.execute(
            """
            CREATE TABLE artifacts (
              id text PRIMARY KEY,
              job_id text NOT NULL,
              editor_id text NOT NULL,
              organization_id text NOT NULL,
              kind text NOT NULL CHECK (kind IN ('letter', 'compendium', 'readback')),
              object_key text NOT NULL,
              created_at text NOT NULL
            )
            """
        )
        raw.commit()
        raw.close()
        conn = open_db(path)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'artifacts'").fetchone()[0]
        self.assertNotIn("kind IN ('letter', 'compendium', 'readback')", sql)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            INSERT INTO artifacts (id, job_id, editor_id, organization_id, kind, object_key, created_at)
            VALUES ('1', 'j', 'e', 'o', 'notes', 'k', 't')
            """
        )
        conn.commit()
        conn.close()

    def _desk(self):
        owner = create_owner(self.conn, "owner@example.com")
        ada = editor_for_user(
            self.conn,
            accept_editor_invite(self.conn, invite_editor(self.conn, owner, "ada@example.com", None), "Ada"),
        )
        author = author_for_user(self.conn, become_customer(self.conn, ada["id"], "Bea", "bea@example.com"))
        persona = self.conn.execute("SELECT * FROM personas WHERE editor_id = ?", (ada["id"],)).fetchone()
        return ada, author, persona

    def _queue(self, ada, author, persona_id: str, slug: str) -> str:
        manuscript = "The city kept its lights on after the river left. " * 20
        key = f"manuscripts/{ada['id']}/{slug}.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            author,
            persona_id=persona_id,
            title=slug,
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="The river does not return.",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest=slug,
        )
        self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
        return job_id
