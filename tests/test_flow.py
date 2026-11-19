import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("XAI_MODE", "local")

from app import config  # noqa: E402
from app.db import open_db  # noqa: E402
from app.services import (  # noqa: E402
    accept_editor_invite,
    author_for_user,
    become_customer,
    create_job,
    create_owner,
    dev_capture,
    editor_for_user,
    account_roles,
    invite_admin,
    invite_editor,
    jobs_visible,
    org_balance_cents,
    practice_model_cost_micros,
    post_message,
    read_object,
    remove_admin,
    remove_editor,
    token_hash,
    request_human,
    revoke_editor_invite,
    search_manuscripts,
    suspend_practice,
    seed_local_accounts,
    publish_persona,
    set_assistant_permissions,
    write_manuscript,
)
from app.worker import run_once  # noqa: E402


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-test-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEMO_MODE = False
        self.conn = open_db(config.DATABASE_PATH)

    def tearDown(self):
        self.conn.close()

    def test_solo_desk_then_assistant_removed(self):
        owner = create_owner(self.conn, "owner@example.com")
        raw = invite_editor(self.conn, owner, "ada@example.com", None)
        editor_user = accept_editor_invite(self.conn, raw, "Ada")
        ada = editor_for_user(self.conn, editor_user)
        self.assertEqual(ada["is_org_admin"], 1)

        persona = self.conn.execute(
            "SELECT * FROM personas WHERE editor_id = ?", (ada["id"],)
        ).fetchone()
        publish_persona(self.conn, ada, persona["id"])

        author_user = become_customer(self.conn, ada["id"], "Bea", "bea@example.com")
        author = author_for_user(self.conn, author_user)
        manuscript = "The city kept its lights on after the river left. " * 40
        digest = "abc"
        key = f"manuscripts/{author['organization_id']}/{ada['id']}/{author['id']}/story.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            author,
            persona_id=persona["id"],
            title="After the river",
            genre="fantasy",
            audience="adult",
            comps="The City We Became",
            do_not_change="The river does not return.",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest=digest,
        )
        self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
        self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "ready")
        letter = self.conn.execute(
            "SELECT object_key FROM artifacts WHERE job_id = ? AND kind = 'letter'",
            (job_id,),
        ).fetchone()
        text = read_object(letter["object_key"])
        self.assertIn("## Promise and genre fit", text)
        self.assertIn("## Plot and structure", text)
        self.assertIn("## Character", text)
        self.assertIn("## Stakes and pacing", text)
        self.assertNotIn(manuscript[:80], text)
        compendium = read_object(
            self.conn.execute(
                "SELECT object_key FROM artifacts WHERE job_id = ? AND kind = 'compendium'",
                (job_id,),
            ).fetchone()["object_key"]
        )
        self.assertIn("LOCKED: The river does not return.", compendium)
        self.assertIn("DRAFT:", compendium)
        readback = read_object(
            self.conn.execute(
                "SELECT object_key FROM artifacts WHERE job_id = ? AND kind = 'readback'",
                (job_id,),
            ).fetchone()["object_key"]
        )
        self.assertIn("DRAFT:", readback)
        calls = [
            row["call_name"]
            for row in self.conn.execute(
                "SELECT call_name FROM provider_calls WHERE job_id = ?",
                (job_id,),
            )
        ]
        self.assertIn("readback", calls)
        self.assertEqual(org_balance_cents(self.conn, ada["organization_id"]), 0)
        self.assertGreater(int(job["cost_usd_micros"]), 0)
        self.assertGreater(practice_model_cost_micros(self.conn, ada["organization_id"]), 0)

        request_human(self.conn, author["id"], job_id)
        flagged = self.conn.execute(
            "SELECT human_requested, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(flagged["human_requested"], 1)
        self.assertEqual(flagged["status"], "ready")
        pipe = self.conn.execute(
            "SELECT * FROM pipeline_items WHERE job_id = ?", (job_id,)
        ).fetchall()
        self.assertEqual(len(pipe), 0)
        found = search_manuscripts(self.conn, ada, human_requested=1)
        self.assertEqual([row["id"] for row in found], [job_id])

        ada = editor_for_user(self.conn, editor_user)
        held = invite_editor(self.conn, editor_user, "later@example.com", ada["organization_id"])
        held_id = self.conn.execute(
            "SELECT id FROM editor_invites WHERE token_hash = ?",
            (token_hash(held),),
        ).fetchone()["id"]
        revoke_editor_invite(self.conn, ada, held_id)
        with self.assertRaises(ValueError):
            accept_editor_invite(self.conn, held, "Later")

        team_token = invite_editor(
            self.conn,
            editor_user,
            "cy@example.com",
            ada["organization_id"],
            {"manuscripts": True},
        )
        cy_user = accept_editor_invite(self.conn, team_token, "Cy")
        cy = editor_for_user(self.conn, cy_user)
        self.assertEqual(cy["is_org_admin"], 0)
        self.assertEqual(cy["organization_id"], ada["organization_id"])
        self.assertEqual(cy["perm_manuscripts"], 1)
        self.assertEqual(cy["perm_messages"], 0)
        self.assertEqual(cy["perm_offers"], 0)
        self.assertEqual(len(jobs_visible(self.conn, cy)), 1)
        self.assertEqual(jobs_visible(self.conn, cy)[0]["editor_id"], ada["id"])
        self.assertEqual(len(jobs_visible(self.conn, ada)), 1)
        with self.assertRaises(ValueError):
            invite_editor(self.conn, cy_user, "nope@example.com", None)
        with self.assertRaises(ValueError):
            invite_editor(self.conn, cy_user, "extra@example.com", ada["organization_id"])
        with self.assertRaises(ValueError):
            become_customer(self.conn, cy["id"], "Dee", "dee@example.com")
        with self.assertRaises(ValueError):
            post_message(
                self.conn,
                job_id=job_id,
                sender_role="editor",
                sender_id=cy["id"],
                body="I should not write yet.",
                actor_editor=cy,
            )

        set_assistant_permissions(
            self.conn,
            ada,
            cy["id"],
            {"manuscripts": True, "messages": True, "assistants": True},
        )
        cy = editor_for_user(self.conn, cy_user)
        self.assertEqual(cy["perm_messages"], 1)
        post_message(
            self.conn,
            job_id=job_id,
            sender_role="editor",
            sender_id=cy["id"],
            body="I can write on this desk now.",
            actor_editor=cy,
        )
        sam_token = invite_editor(
            self.conn,
            editor_user,
            "sam@example.com",
            ada["organization_id"],
            {"manuscripts": True},
        )
        sam = editor_for_user(
            self.conn,
            accept_editor_invite(self.conn, sam_token, "Sam"),
        )
        with self.assertRaises(ValueError) as caught:
            set_assistant_permissions(
                self.conn,
                cy,
                sam["id"],
                {"manuscripts": True, "payouts": True},
            )
        self.assertIn("you cannot grant a permission you do not have", str(caught.exception))
        with self.assertRaises(ValueError):
            set_assistant_permissions(self.conn, ada, cy["id"], {})

        remove_editor(self.conn, ada, cy["id"])
        cy = editor_for_user(self.conn, cy_user)
        self.assertEqual(cy["status"], "removed")
        self.assertEqual(len(jobs_visible(self.conn, ada)), 1)
        self.assertEqual(jobs_visible(self.conn, ada)[0]["editor_id"], ada["id"])
        self.assertEqual(len(jobs_visible(self.conn, sam)), 1)

    def test_author_cannot_cross_tenants(self):
        owner = create_owner(self.conn, "owner@example.com")
        ada_user = accept_editor_invite(self.conn, invite_editor(self.conn, owner, "ada@example.com", None), "Ada")
        bo_user = accept_editor_invite(self.conn, invite_editor(self.conn, owner, "bo@example.com", None), "Bo")
        ada = editor_for_user(self.conn, ada_user)
        bo = editor_for_user(self.conn, bo_user)
        bea = author_for_user(
            self.conn,
            become_customer(self.conn, ada["id"], "Bea", "bea@example.com"),
        )
        self.assertNotEqual(bea["editor_id"], bo["id"])
        with self.assertRaises(ValueError):
            become_customer(self.conn, bo["id"], "Bea", "bea@example.com")

    def test_daily_stop_writes_no_diagnosis(self):
        owner = create_owner(self.conn, "owner@example.com")
        ada = editor_for_user(
            self.conn,
            accept_editor_invite(self.conn, invite_editor(self.conn, owner, "ada@example.com", None), "Ada"),
        )
        persona = self.conn.execute("SELECT id FROM personas WHERE editor_id = ?", (ada["id"],)).fetchone()
        publish_persona(self.conn, ada, persona["id"])
        author = author_for_user(
            self.conn,
            become_customer(self.conn, ada["id"], "Bea", "bea@example.com"),
        )
        manuscript = "A short road through the trees. " * 20
        key = f"manuscripts/{ada['id']}/stop.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            author,
            persona_id=persona["id"],
            title="The short road",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest="stop",
        )
        self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
        previous = config.DAILY_STOP_MICROS
        config.DAILY_STOP_MICROS = 1
        try:
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        finally:
            config.DAILY_STOP_MICROS = previous
        job = self.conn.execute("SELECT status, error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "no_diagnosis")
        self.assertEqual(org_balance_cents(self.conn, ada["organization_id"]), 0)

    def test_demo_pay_and_worker_skip_the_workflow(self):
        previous = config.DEMO_MODE
        config.DEMO_MODE = False
        try:
            owner = create_owner(self.conn, "owner@example.com")
            ada = editor_for_user(
                self.conn,
                accept_editor_invite(
                    self.conn,
                    invite_editor(self.conn, owner, "ada@example.com", None),
                    "Ada",
                ),
            )
            persona = self.conn.execute(
                "SELECT id FROM personas WHERE editor_id = ?", (ada["id"],)
            ).fetchone()
            publish_persona(self.conn, ada, persona["id"])
            author = author_for_user(
                self.conn,
                become_customer(self.conn, ada["id"], "Bea", "bea@example.com"),
            )
            manuscript = "A short road through the trees. " * 20
            key = f"manuscripts/{ada['id']}/demo.txt"
            write_manuscript(key, manuscript)
            job_id = create_job(
                self.conn,
                author,
                persona_id=persona["id"],
                title="The short road",
                genre="fantasy",
                audience="adult",
                comps="",
                do_not_change="",
                focus="",
                manuscript=manuscript,
                object_key=key,
                digest="demo-queued",
            )
            self.assertEqual(dev_capture(self.conn, job_id, author["id"]), "queued")
            config.DEMO_MODE = True
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
            job = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            self.assertEqual(job["status"], "ready")
            self.assertEqual(job["tokens_used"], 0)
            self.assertEqual(job["cost_usd_micros"], 0)
            letter = read_object(
                self.conn.execute(
                    "SELECT object_key FROM artifacts WHERE job_id = ? AND kind = 'letter'",
                    (job_id,),
                ).fetchone()["object_key"]
            )
            self.assertIn("The editing workflow did not run.", letter)
            self.assertNotIn("## Promise and genre fit", letter)
            calls = self.conn.execute("SELECT COUNT(*) AS n FROM provider_calls").fetchone()["n"]
            self.assertEqual(calls, 0)
            self.assertEqual(org_balance_cents(self.conn, ada["organization_id"]), 0)
            filed = self.conn.execute(
                "SELECT COUNT(*) AS n FROM pipeline_items WHERE job_id = ?",
                (job_id,),
            ).fetchone()["n"]
            self.assertEqual(filed, 0)

            second_key = f"manuscripts/{ada['id']}/demo-pay.txt"
            write_manuscript(second_key, manuscript)
            second_id = create_job(
                self.conn,
                author,
                persona_id=persona["id"],
                title="The other road",
                genre="fantasy",
                audience="adult",
                comps="",
                do_not_change="",
                focus="",
                manuscript=manuscript,
                object_key=second_key,
                digest="demo-pay",
            )
            self.assertEqual(dev_capture(self.conn, second_id, author["id"]), "ready")
            self.assertIsNone(run_once(config.DATABASE_PATH))
            second = self.conn.execute(
                "SELECT status, tokens_used FROM jobs WHERE id = ?", (second_id,)
            ).fetchone()
            self.assertEqual(second["status"], "ready")
            self.assertEqual(second["tokens_used"], 0)
        finally:
            config.DEMO_MODE = previous

    def test_seed_local_accounts_are_ready_to_sign_in(self):
        first = seed_local_accounts(self.conn)
        second = seed_local_accounts(self.conn)
        self.assertEqual(first["editor_id"], second["editor_id"])
        self.assertEqual(first["editor_user_id"], first["owner_id"])
        self.assertEqual(first["author_user_id"], second["author_user_id"])
        editor = editor_for_user(self.conn, first["editor_user_id"])
        author = author_for_user(self.conn, first["author_user_id"])
        self.assertEqual(author["editor_id"], editor["id"])
        self.assertEqual(editor["is_org_admin"], 1)
        persona = self.conn.execute(
            "SELECT status, price_cents FROM personas WHERE editor_id = ?",
            (editor["id"],),
        ).fetchone()
        self.assertEqual(persona["status"], "active")
        self.assertIsNone(persona["price_cents"])
        owner = self.conn.execute(
            "SELECT id, is_owner, role FROM users WHERE email = 'owner@example.com'"
        ).fetchone()
        self.assertEqual(owner["role"], "owner")
        self.assertEqual(owner["is_owner"], 1)
        owner_editor = editor_for_user(self.conn, owner["id"])
        self.assertEqual(owner_editor["is_org_admin"], 1)
        self.assertEqual(owner_editor["org_name"], "Owner")
        baked_editor = self.conn.execute(
            "SELECT id FROM users WHERE email = 'editor@example.com' AND deleted_at IS NULL"
        ).fetchone()
        self.assertIsNone(baked_editor)
        author_user = self.conn.execute(
            "SELECT id FROM users WHERE email = 'author@example.com' AND deleted_at IS NULL"
        ).fetchone()
        self.assertIsNotNone(author_user)
        self.assertEqual(account_roles(self.conn, owner), ["Owner", "Editor"])

    def test_admin_cannot_change_the_owner(self):
        owner_id = create_owner(self.conn, "owner@example.com")
        owner = self.conn.execute("SELECT * FROM users WHERE id = ?", (owner_id,)).fetchone()
        raw = invite_admin(self.conn, owner_id, "admin@example.com")
        admin_id = accept_editor_invite(self.conn, raw, "Ann")
        admin = self.conn.execute("SELECT * FROM users WHERE id = ?", (admin_id,)).fetchone()
        self.assertEqual(account_roles(self.conn, admin), ["Admin"])
        owner_editor = editor_for_user(self.conn, owner_id)
        with self.assertRaises(ValueError) as caught:
            suspend_practice(self.conn, admin, owner_editor["id"], "no")
        self.assertIn("the owner cannot be changed", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            invite_admin(self.conn, admin_id, "other@example.com")
        self.assertIn("only the owner can add an admin", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            remove_admin(self.conn, admin, owner_id)
        self.assertIn("only the owner can change an admin", str(caught.exception))
        remove_admin(self.conn, owner, admin_id)
        admin = self.conn.execute("SELECT * FROM users WHERE id = ?", (admin_id,)).fetchone()
        self.assertEqual(admin["is_admin"], 0)
        self.assertEqual(account_roles(self.conn, admin), [])
        still = self.conn.execute("SELECT is_owner FROM users WHERE id = ?", (owner_id,)).fetchone()
        self.assertEqual(still["is_owner"], 1)

    def test_old_user_table_gains_owner_flag(self):
        path = self.tmp / "old-users.sqlite"
        raw = sqlite3.connect(path)
        raw.execute(
            """
            CREATE TABLE users (
              id text PRIMARY KEY,
              email text NOT NULL,
              name text,
              role text NOT NULL CHECK (role IN ('owner', 'editor', 'author')),
              auth_provider text NOT NULL,
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
              id, email, name, role, auth_provider, auth_subject, created_at
            ) VALUES ('u1', 'owner@example.com', 'Platform owner', 'owner', 'dev', 'dev:owner@example.com', '2026-01-01T00:00:00+00:00')
            """
        )
        raw.commit()
        raw.close()
        opened = open_db(path)
        row = opened.execute(
            "SELECT role, is_owner, is_admin, contact_email, phone FROM users"
        ).fetchone()
        self.assertEqual(row["role"], "owner")
        self.assertEqual(row["is_owner"], 1)
        self.assertEqual(row["is_admin"], 0)
        self.assertEqual(row["contact_email"], "")
        self.assertEqual(row["phone"], "")
        sql = opened.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
        self.assertIn("'admin'", sql)
        opened.close()

    def test_old_database_gains_permission_columns(self):
        path = self.tmp / "old.sqlite"
        conn = sqlite3.connect(path)
        script = config.SCHEMA_PATH.read_text()
        for column in (
            "perm_manuscripts",
            "perm_messages",
            "perm_pipeline",
            "perm_offers",
            "perm_payouts",
            "perm_assistants",
        ):
            script = script.replace(f"  {column} integer NOT NULL DEFAULT 0,\n", "")
        conn.executescript(script)
        conn.close()
        opened = open_db(path)
        for table in ("editors", "editor_invites"):
            have = {row[1] for row in opened.execute(f"PRAGMA table_info({table})")}
            self.assertIn("perm_manuscripts", have)
            self.assertIn("perm_assistants", have)
        seeded = seed_local_accounts(opened)
        editor = editor_for_user(opened, seeded["editor_user_id"])
        self.assertEqual(editor["perm_manuscripts"], 1)
        opened.close()

    def test_old_persona_table_can_be_active_without_a_price(self):
        path = self.tmp / "old-price.sqlite"
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE users (id text)")
        raw.execute(
            """
            CREATE TABLE personas (
              id text PRIMARY KEY,
              editor_id text NOT NULL,
              organization_id text NOT NULL,
              name text NOT NULL,
              instructions text NOT NULL DEFAULT '',
              agent_code text NOT NULL DEFAULT '',
              agent_skills text NOT NULL DEFAULT '',
              compendium_enabled integer NOT NULL DEFAULT 1,
              readback_enabled integer NOT NULL DEFAULT 1,
              price_cents integer,
              max_words integer NOT NULL DEFAULT 180000,
              status text NOT NULL CHECK (status IN ('draft', 'active', 'paused')),
              created_at text NOT NULL,
              updated_at text NOT NULL,
              CHECK (length(instructions) <= 2000),
              CHECK (readback_enabled = 0 OR compendium_enabled = 1),
              CHECK (price_cents IS NULL OR price_cents > 0),
              CHECK (status <> 'active' OR price_cents IS NOT NULL),
              CHECK (max_words > 0)
            )
            """
        )
        raw.execute(
            """
            INSERT INTO personas (
              id, editor_id, organization_id, name, agent_code, price_cents, status, created_at, updated_at
            ) VALUES ('p', 'e', 'o', 'Letter', 'def run(manuscript, form, skill):\n    return {}\n', 1500, 'active', 't', 't')
            """
        )
        raw.commit()
        raw.close()
        opened = open_db(path)
        sql = opened.execute("SELECT sql FROM sqlite_master WHERE name = 'personas'").fetchone()[0]
        self.assertNotIn("price_cents IS NOT NULL", sql)
        row = opened.execute("SELECT price_cents, status FROM personas").fetchone()
        self.assertIsNone(row["price_cents"])
        self.assertEqual(row["status"], "active")
        opened.close()

    def _queued_manuscript(self) -> tuple[str, str]:
        seeded = seed_local_accounts(self.conn)
        editor = editor_for_user(self.conn, seeded["editor_user_id"])
        author = author_for_user(self.conn, seeded["author_user_id"])
        persona = self.conn.execute(
            "SELECT id FROM personas WHERE editor_id = ?",
            (editor["id"],),
        ).fetchone()
        manuscript = "The city kept its lights on after the river left. " * 40
        key = f"manuscripts/{author['organization_id']}/{editor['id']}/{author['id']}/story.txt"
        write_manuscript(key, manuscript)
        job_id = create_job(
            self.conn,
            author,
            persona_id=persona["id"],
            title="After the river",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest="abc",
        )
        return job_id, persona["id"]

    def test_worker_claims_a_queued_job_after_the_database_opens(self):
        job_id, persona_id = self._queued_manuscript()
        self.conn.execute("UPDATE personas SET agent_code = '' WHERE id = ?", (persona_id,))
        self.conn.commit()
        self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        status = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(status["status"], "ready")

    def test_worker_leaves_a_job_it_did_not_claim(self):
        job_id, persona_id = self._queued_manuscript()
        self.conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
        self.conn.execute("UPDATE personas SET agent_code = '' WHERE id = ?", (persona_id,))
        self.conn.commit()

        class _ClaimSelect:
            def __init__(self, cursor):
                self._cursor = cursor

            def fetchone(self):
                self._cursor.fetchone()
                return {"id": job_id}

            def __getattr__(self, name):
                return getattr(self._cursor, name)

        class _Conn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                cursor = self._conn.execute(sql, *args)
                if (
                    isinstance(sql, str)
                    and sql.lstrip().startswith("SELECT")
                    and "FROM jobs" in sql
                    and "status = 'queued'" in sql
                ):
                    return _ClaimSelect(cursor)
                return cursor

            def __getattr__(self, name):
                return getattr(self._conn, name)

        real_open = open_db

        def open_claimed(path=None):
            return _Conn(real_open(path))

        with unittest.mock.patch("app.worker.open_db", open_claimed):
            self.assertIsNone(run_once(config.DATABASE_PATH))
        status = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(status["status"], "running")
        letter = self.conn.execute(
            "SELECT id FROM artifacts WHERE job_id = ? AND kind = 'letter'",
            (job_id,),
        ).fetchone()
        self.assertIsNone(letter)

    def test_schema_is_the_sqlite_file(self):
        text = config.SCHEMA_PATH.read_text(encoding="utf-8")
        self.assertIn("SQLite schema", text)
        self.assertNotIn("Postgres", text)
        self.assertNotIn("FORCE RLS", text)


if __name__ == "__main__":
    unittest.main()
