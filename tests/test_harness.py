"""Packages land on disk. The worker stages one and runs the harness it names."""

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("XAI_MODE", "local")

from app import config  # noqa: E402
from app.agent import DEFAULT_AGENT, estimate_agent_words  # noqa: E402
from app.db import open_db  # noqa: E402
from app.harness import default_workflow, gate_source, run_workflow, stage  # noqa: E402
from app.package import files_from_zip, job_agent_dir, persona_dir  # noqa: E402
from app.prompts import build_prompt  # noqa: E402
from app.services import (  # noqa: E402
    accept_editor_invite,
    author_for_user,
    become_customer,
    create_job,
    create_owner,
    create_persona,
    dev_capture,
    editor_for_user,
    install_persona_files,
    install_persona_zip,
    invite_editor,
    publish_persona,
    read_object,
    write_manuscript,
)
from app.worker import run_once  # noqa: E402


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-harness-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        self.conn = open_db(config.DATABASE_PATH)
        owner = create_owner(self.conn, "owner@example.com")
        self.editor = editor_for_user(
            self.conn,
            accept_editor_invite(self.conn, invite_editor(self.conn, owner, "ada@example.com", None), "Ada"),
        )
        self.author = author_for_user(
            self.conn, become_customer(self.conn, self.editor["id"], "Bea", "bea@example.com")
        )

    def tearDown(self):
        self.conn.close()

    def test_workflow_matches_the_default_price_and_letter(self):
        self.assertEqual(
            estimate_agent_words(180_000, DEFAULT_AGENT)["cost_usd_micros"],
            estimate_agent_words(180_000, gate_source(default_workflow()))["cost_usd_micros"],
        )
        persona_id = create_persona(
            self.conn,
            self.editor,
            name="Shelf workflow",
            max_words=180000,
        )
        root = persona_dir(persona_id)
        called = []

        def call(skill, ask, attachment, name):
            called.append(name)
            if name == "compendium":
                return "- DRAFT: The city.\n"
            if name == "readback":
                return "- DRAFT: read back\n"
            return f"diagnosis {name}\n"

        quiet = run_workflow(root, {"focus": ""}, call)
        self.assertNotIn("focus", called)
        self.assertIn("## Promise and genre fit", quiet["letter"])
        self.assertIn("## Stakes and pacing", quiet["letter"])
        self.assertNotIn("## Author focus", quiet["letter"])
        called.clear()
        focused = run_workflow(root, {"focus": "the river"}, call)
        self.assertIn("focus", called)
        self.assertIn("## Author focus", focused["letter"])

        publish_persona(self.conn, self.editor, persona_id)
        manuscript = "The city kept its lights on after the river left. " * 30
        key = f"manuscripts/{self.editor['id']}/shelf.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            self.author,
            persona_id=persona_id,
            title="Shelf",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="The river does not return.",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest="shelf",
        )
        self.assertEqual(dev_capture(self.conn, job_id, self.author["id"]), "queued")
        work = stage(
            job_id,
            job_agent_dir(job_id),
            manuscript,
            {"title": "Shelf", "focus": ""},
        )
        self.assertFalse((work / "skills" / "author-dev").exists())
        for name in ("author-dev", "author-compendium", "author-readback"):
            self.assertFalse((config.ROOT / "skills" / name).exists())
        prompt = build_prompt(
            skill="author-dev",
            ask="promise",
            persona="",
            manuscript="Once.",
            form="Title: Shelf\n",
        )
        self.assertIn("Diagnose promise and genre fit", prompt)
        self.assertNotIn("SKILL.md", prompt)
        self.assertEqual((work / "inputs" / "manuscript.txt").read_text(), manuscript)
        self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        kinds = {
            row["kind"]: read_object(row["object_key"])
            for row in self.conn.execute("SELECT kind, object_key FROM artifacts WHERE job_id = ?", (job_id,))
        }
        self.assertIn("## Promise and genre fit", kinds["letter"])
        self.assertIn("## Plot and structure", kinds["letter"])
        self.assertIn("## Character", kinds["letter"])
        self.assertIn("## Stakes and pacing", kinds["letter"])
        self.assertIn("LOCKED: The river does not return.", kinds["compendium"])
        self.assertIn("readback", kinds)

    def test_checkout_keeps_the_package_it_copied(self):
        persona = self.conn.execute(
            "SELECT * FROM personas WHERE editor_id = ?",
            (self.editor["id"],),
        ).fetchone()
        publish_persona(self.conn, self.editor, persona["id"])
        manuscript = "The city kept its lights on after the river left. " * 20
        key = "manuscripts/kept.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            self.author,
            persona_id=persona["id"],
            title="Kept",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest="kept",
        )
        frozen = (job_agent_dir(job_id) / "workflow.json").read_text()
        install_persona_files(
            self.conn,
            self.editor,
            persona["id"],
            {"workflow.json": frozen.replace('"Developmental edit"', '"Later edit"', 1)},
        )
        self.assertEqual((job_agent_dir(job_id) / "workflow.json").read_text(), frozen)
        self.assertIn("Later edit", (persona_dir(persona["id"]) / "workflow.json").read_text())

    def test_package_paths_cannot_escape(self):
        persona = self.conn.execute(
            "SELECT id FROM personas WHERE editor_id = ?",
            (self.editor["id"],),
        ).fetchone()
        before = (persona_dir(persona["id"]) / "workflow.json").read_text()
        slipped = io.BytesIO()
        with zipfile.ZipFile(slipped, "w") as archive:
            archive.writestr("../secret.txt", "nope")
        with self.assertRaises(ValueError) as caught:
            files_from_zip(slipped.getvalue())
        self.assertIn("inside the agent", str(caught.exception))
        with self.assertRaises(ValueError):
            install_persona_zip(self.conn, self.editor, persona["id"], slipped.getvalue())
        self.assertEqual((persona_dir(persona["id"]) / "workflow.json").read_text(), before)
        too_many = {f"notes/n{index}.txt": "x\n" for index in range(401)}
        with self.assertRaises(ValueError) as caught:
            install_persona_files(self.conn, self.editor, persona["id"], too_many)
        self.assertIn("400", str(caught.exception))
        self.assertEqual((persona_dir(persona["id"]) / "workflow.json").read_text(), before)
