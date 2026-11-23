"""A workflow names a model. Saved providers are tried until one runs it."""

import io
import json
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from app import config, llm
from app.db import open_db
from app.harness import parse_workflow
from app.mcp_server import handle_message
from app.services import (
    accept_editor_invite,
    author_for_user,
    become_customer,
    create_job,
    create_owner,
    create_persona,
    editor_for_user,
    inference_defaults,
    inference_public,
    inference_rows,
    invite_editor,
    publish_persona,
    save_workflow,
    set_inference,
    set_inference_defaults,
    write_manuscript,
)
from app.worker import run_job, run_once

MCP_URL = "http://127.0.0.1:8080/mcp"
ONE = {
    "title": "One",
    "model": "grok-4.3",
    "steps": [
        {
            "name": "promise",
            "skill": "author-dev",
            "ask": "Diagnose the promise.",
            "produces": "letter",
        }
    ],
}


def _models(ids):
    return {"data": [{"id": item} for item in ids]}


class InferenceRoutingTests(unittest.TestCase):
    def test_exact_ids_are_tried_before_the_shortest_suffix(self):
        accounts = [
            {"label": "later", "models": ["other/grok-4.3", "x-ai/grok-4.3"]},
            {"label": "exact", "models": ["grok-4.3"]},
            {"label": "none", "models": ["llama3.2"]},
        ]
        picked = llm.candidates("grok-4.3", accounts)
        self.assertEqual([item["label"] for item in picked], ["exact", "later"])
        self.assertEqual(picked[1]["model"], "x-ai/grok-4.3")

    def test_a_cheaper_published_price_beats_an_exact_id(self):
        accounts = [
            {
                "label": "dear-exact",
                "models": [{"id": "grok-4.3", "prompt": 5_000_000, "completion": 5_000_000}],
            },
            {
                "label": "cheap-suffix",
                "models": [{"id": "x-ai/grok-4.3", "prompt": 1_000_000, "completion": 1_000_000}],
            },
            {"label": "unpriced-exact", "models": ["grok-4.3"]},
        ]
        picked = llm.candidates("grok-4.3", accounts)
        self.assertEqual(
            [item["label"] for item in picked],
            ["cheap-suffix", "dear-exact", "unpriced-exact"],
        )
        self.assertNotIn("price", picked[2])

    def test_an_exact_id_wins_a_price_tie(self):
        accounts = [
            {
                "label": "suffix",
                "models": [{"id": "x-ai/grok-4.3", "prompt": 1_000_000, "completion": 2_000_000}],
            },
            {
                "label": "exact",
                "models": [{"id": "grok-4.3", "prompt": 1_000_000, "completion": 2_000_000}],
            },
        ]
        picked = llm.candidates("grok-4.3", accounts)
        self.assertEqual([item["label"] for item in picked], ["exact", "suffix"])

    def test_input_price_is_tried_before_output_price(self):
        accounts = [
            {"label": "in-dear", "models": [{"id": "grok-4.3", "prompt": 2, "completion": 1}]},
            {"label": "in-cheap", "models": [{"id": "grok-4.3", "prompt": 1, "completion": 100}]},
        ]
        picked = llm.candidates("grok-4.3", accounts)
        self.assertEqual([item["label"] for item in picked], ["in-cheap", "in-dear"])

    def test_the_shortest_suffix_keeps_its_own_price(self):
        accounts = [
            {
                "label": "one",
                "models": [
                    {"id": "long/vendor/grok-4.3", "prompt": 1, "completion": 1},
                    {"id": "x-ai/grok-4.3", "prompt": 9_000_000, "completion": 9_000_000},
                ],
            }
        ]
        picked = llm.candidates("grok-4.3", accounts)
        self.assertEqual(picked[0]["model"], "x-ai/grok-4.3")
        self.assertEqual(picked[0]["price"], (9_000_000, 9_000_000))

    def test_a_negative_price_is_unknown_and_not_free(self):
        account = llm.prepare_account(provider="openrouter", base_url="", secret="router-key")
        payload = {
            "data": [
                {"id": "known", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
                {"id": "unknown", "pricing": {"prompt": "-1", "completion": "0.000002"}},
                {"id": "free", "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "half", "pricing": {"prompt": "0.000001"}},
                {"id": "floaty", "pricing": {"prompt": 0.000001, "completion": 0.000002}},
            ]
        }
        with patch("app.llm._get", return_value=payload):
            models = llm.probe_models(account)
        by_id = {item["id"]: item for item in models}
        self.assertEqual(by_id["known"]["prompt"], 3_000_000)
        self.assertEqual(by_id["known"]["completion"], 15_000_000)
        self.assertNotIn("prompt", by_id["unknown"])
        self.assertEqual(by_id["free"]["prompt"], 0)
        self.assertEqual(by_id["free"]["completion"], 0)
        self.assertNotIn("prompt", by_id["half"])
        self.assertNotIn("prompt", by_id["floaty"])
        self.assertEqual(llm.price_label(3_000_000, 150_000), "$3, $0.15")
        ranked = llm.candidates(
            "m",
            [
                {"label": "unknown", "models": [{"id": "m"}]},
                {"label": "free", "models": [{"id": "m", "prompt": 0, "completion": 0}]},
                {"label": "known", "models": [{"id": "m", "prompt": 3_000_000, "completion": 15_000_000}]},
            ],
        )
        self.assertEqual([item["label"] for item in ranked], ["free", "known", "unknown"])

    def test_defaults_are_ranked_by_the_cheapest_published_price(self):
        accounts = [
            {
                "label": "desk",
                "models": [
                    {"id": "dear", "prompt": 9_000_000, "completion": 9_000_000},
                    {"id": "cheap", "prompt": 1_000_000, "completion": 1_000_000},
                    {"id": "zeta"},
                    {"id": "alpha"},
                ],
            }
        ]
        groups = llm.fallback_targets(["dear", "zeta", "missing", "cheap", "alpha"], accounts)
        self.assertEqual([name for name, _picked in groups], ["cheap", "dear", "zeta", "alpha"])

    def test_an_old_database_gains_the_provider_table(self):
        tmp = Path(tempfile.mkdtemp(prefix="editting-inference-old-"))
        path = tmp / "editting.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE users (id text)")
        conn.commit()
        conn.close()
        opened = open_db(path)
        names = {
            row[0]
            for row in opened.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name IN ('inference_providers', 'inference_defaults')
                """
            )
        }
        opened.close()
        self.assertEqual(names, {"inference_providers", "inference_defaults"})

    def test_a_refused_list_does_not_echo_the_body(self):
        def boom(req, timeout=0):
            raise urllib.error.HTTPError(
                req.full_url, 401, "no", {}, io.BytesIO(b"secret prompt leaked")
            )

        account = llm.prepare_account(provider="anthropic", base_url="", secret="anthropic-key")
        with patch("app.llm._response", boom):
            with self.assertRaises(ValueError) as caught:
                llm.probe_models(account)
        self.assertEqual(str(caught.exception), "that provider refused the credential")
        self.assertNotIn("prompt", str(caught.exception))
        self.assertNotIn("anthropic-key", str(caught.exception))

    def test_the_model_field_is_kept_and_a_provider_is_not(self):
        spec = parse_workflow({**ONE, "provider": "openai", "key": "nope"})
        self.assertEqual(spec["model"], "grok-4.3")
        self.assertNotIn("provider", spec)
        self.assertNotIn("key", spec)
        with self.assertRaises(ValueError):
            parse_workflow({**ONE, "model": "grok 4"})
        self.assertNotIn("model", parse_workflow({"title": "Plain", "steps": ONE["steps"]}))

    def test_a_compatible_url_cannot_carry_a_password(self):
        with self.assertRaises(ValueError):
            llm.prepare_account(
                provider="compatible",
                base_url="http://user:pass@127.0.0.1:8000/v1",
                secret="",
            )

    def test_a_metadata_address_is_refused_and_a_lan_address_is_kept(self):
        for base_url in (
            "http://169.254.169.254/latest",
            "http://metadata.google.internal/computeMetadata/v1",
            "http://2130706433/",
            "http://100.100.100.200/",
        ):
            with self.assertRaises(ValueError) as caught:
                llm.prepare_account(provider="compatible", base_url=base_url, secret="")
            self.assertEqual(str(caught.exception), "that provider address is not allowed")
        account = llm.prepare_account(
            provider="compatible",
            base_url="http://10.1.2.3:11434/v1",
            secret="",
        )
        self.assertEqual(account["base"], "http://10.1.2.3:11434/v1")

    def test_a_hostname_that_resolves_to_a_link_local_address_is_refused(self):
        def fake_getaddrinfo(host, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]

        with patch("app.llm.socket.getaddrinfo", fake_getaddrinfo):
            with self.assertRaises(ValueError) as caught:
                llm._get("https://models.example/v1/models", {}, 5)
        self.assertEqual(str(caught.exception), "that provider address is not allowed")

    def test_a_redirect_is_not_followed(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with self.assertRaises(ValueError) as caught:
                llm.probe_models(
                    {
                        "style": "openai",
                        "base": f"http://127.0.0.1:{port}/v1",
                        "key": "",
                        "timeout": 5,
                    }
                )
            self.assertEqual(str(caught.exception), "that provider could not be reached")
            self.assertEqual(seen, ["/v1/models"])
        finally:
            server.shutdown()
            server.server_close()

class InferenceRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="editting-inference-"))
        config.DATA_DIR = self.tmp
        config.DATABASE_PATH = self.tmp / "editting.sqlite"
        config.OBJECT_DIR = self.tmp / "objects"
        config.DEMO_MODE = False
        config.XAI_MODE = "local"
        config.LLM_MODE = ""
        self.conn = open_db(config.DATABASE_PATH)
        owner = create_owner(self.conn, "owner@example.com")
        self.editor = editor_for_user(
            self.conn,
            accept_editor_invite(self.conn, invite_editor(self.conn, owner, "ada@example.com", None), "Ada"),
        )
        self.author = author_for_user(
            self.conn, become_customer(self.conn, self.editor["id"], "Bea", "bea@example.com")
        )
        self.user = dict(
            self.conn.execute("SELECT * FROM users WHERE id = ?", (self.editor["user_id"],)).fetchone()
        )

    def tearDown(self):
        self.conn.close()

    def _persona(self, spec):
        persona_id = create_persona(self.conn, self.editor, name="One", max_words=180000)
        save_workflow(self.conn, self.editor, persona_id, spec)
        publish_persona(self.conn, self.editor, persona_id)
        return persona_id

    def _job(self, persona_id):
        manuscript = "The city kept its lights on after the river left. " * 20
        key = "manuscripts/one.txt"
        write_manuscript(key, manuscript)
        return create_job(
            self.conn,
            self.author,
            persona_id=persona_id,
            title="One",
            genre="fantasy",
            audience="adult",
            comps="",
            do_not_change="",
            focus="",
            manuscript=manuscript,
            object_key=key,
            digest="one",
        ), manuscript

    def _save(self, label, provider, models, secret="k"):
        self._save_catalog(label, provider, _models(models), secret=secret)

    def _save_catalog(self, label, provider, payload, secret="k"):
        with patch("app.llm._get", return_value=payload):
            set_inference(
                self.conn,
                self.editor,
                label=label,
                provider=provider,
                base_url="",
                secret=secret,
            )

    def test_no_provider_lists_the_model_and_nothing_is_sent(self):
        persona_id = self._persona(ONE)
        self._save("Local", "ollama", ["llama3.2"], secret="")
        job_id, manuscript = self._job(persona_id)
        with patch("app.llm._get", side_effect=ValueError("that provider could not be reached")):
            with patch("app.llm._post") as post:
                with self.assertRaises(llm.InferenceError) as caught:
                    run_once(config.DATABASE_PATH)
                post.assert_not_called()
        self.assertEqual(str(caught.exception), "no configured provider offers grok-4.3")
        row = self.conn.execute("SELECT status, error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "no configured provider offers grok-4.3")
        self.assertNotIn(manuscript[:40], row["error_code"])

    def test_a_workflow_without_a_model_fails_before_a_call(self):
        persona_id = self._persona({"title": "Plain", "steps": ONE["steps"]})
        self._save("Desk", "xai", ["grok-4.3"])
        job_id, _manuscript = self._job(persona_id)
        with patch("app.llm._get") as get, patch("app.llm._post") as post:
            with self.assertRaises(llm.InferenceError) as caught:
                run_once(config.DATABASE_PATH)
            get.assert_not_called()
            post.assert_not_called()
        self.assertEqual(str(caught.exception), "this workflow does not name a model")
        row = self.conn.execute("SELECT error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["error_code"], "this workflow does not name a model")

    def test_the_next_provider_runs_the_model_when_the_first_call_fails(self):
        persona_id = self._persona(ONE)
        self._save("A exact", "openai", ["grok-4.3"], secret="openai-key")
        self._save("B router", "openrouter", ["other/grok-4.3", "x-ai/grok-4.3"], secret="router-key")
        self.conn.execute(
            "UPDATE inference_providers SET created_at = ? WHERE label = ?",
            ("2020-01-02T00:00:00", "A exact"),
        )
        self.conn.execute(
            "UPDATE inference_providers SET created_at = ? WHERE label = ?",
            ("2020-01-01T00:00:00", "B router"),
        )
        self.conn.commit()
        job_id, manuscript = self._job(persona_id)
        posts = []

        def fake_get(url, headers, timeout):
            if urlsplit(url).hostname == "api.openai.com":
                return _models(["grok-4.3"])
            if urlsplit(url).hostname == "openrouter.ai":
                return _models(["other/grok-4.3", "x-ai/grok-4.3"])
            raise AssertionError(url)

        def fake_post(url, headers, body, timeout):
            posts.append((url, body["model"]))
            self.assertNotIn("openai-key", url)
            self.assertNotIn("router-key", body["messages"][0]["content"])
            if urlsplit(url).hostname == "api.openai.com":
                raise RuntimeError("the model service refused the call")
            return {
                "choices": [{"message": {"content": "The promise is late.\n"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            }

        with patch("app.llm._get", fake_get), patch("app.llm._post", fake_post):
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        self.assertEqual(
            posts,
            [
                ("https://api.openai.com/v1/chat/completions", "grok-4.3"),
                ("https://openrouter.ai/api/v1/chat/completions", "x-ai/grok-4.3"),
            ],
        )
        letter = self.conn.execute(
            "SELECT object_key FROM artifacts WHERE job_id = ? AND kind = 'letter'",
            (job_id,),
        ).fetchone()
        from app.services import read_object

        self.assertIn("The promise is late.", read_object(letter["object_key"]))
        self.assertNotIn(manuscript[:40], read_object(letter["object_key"]))

    def test_a_manual_run_does_not_call_a_saved_provider(self):
        persona_id = self._persona(ONE)
        self._save("Desk", "xai", ["grok-4.3"])
        job_id, _manuscript = self._job(persona_id)
        with patch("app.llm._post") as post:
            self.assertEqual(run_job(job_id, config.DATABASE_PATH), job_id)
            post.assert_not_called()
        row = self.conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "ready")

    def test_a_failed_probe_is_not_saved_and_a_blank_key_is_kept(self):
        with patch("app.llm._get", side_effect=ValueError("that provider could not be reached")):
            with self.assertRaises(ValueError):
                set_inference(
                    self.conn,
                    self.editor,
                    label="Desk",
                    provider="xai",
                    base_url="",
                    secret="first-key",
                )
        self.assertEqual(inference_rows(self.conn, self.editor["organization_id"]), [])
        self._save("Desk", "xai", ["grok-4.3"], secret="first-key")
        with patch("app.llm._get", return_value=_models(["grok-4.3", "grok-4"])):
            set_inference(
                self.conn,
                self.editor,
                label="Desk",
                provider="xai",
                base_url="",
                secret="",
            )
        row = inference_rows(self.conn, self.editor["organization_id"])[0]
        self.assertEqual(row["secret"], "first-key")
        public = inference_public([row])[0]
        self.assertEqual(public["secret"], "saved")
        self.assertEqual(public["models"], ["grok-4.3", "grok-4"])
        self.assertNotIn("first-key", str(public))

    def test_the_tool_returns_every_model_and_not_the_key(self):
        self._save("Desk", "xai", [f"model-{index}" for index in range(41)], secret="tool-key")
        response = handle_message(
            self.conn,
            self.user,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "inference", "arguments": {}}},
            mcp_url=MCP_URL,
        )
        text = response["result"]["content"][0]["text"]
        self.assertIn("model-40", text)
        self.assertNotIn("tool-key", text)
        self.assertIn('"secret": "saved"', text)
        self.assertEqual(json.loads(text)["defaults"], [])

    def _reply(self):
        return {
            "choices": [{"message": {"content": "The promise is late.\n"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
        }

    def test_a_listed_model_is_not_replaced_by_a_cheaper_default(self):
        persona_id = self._persona(ONE)
        catalog = {
            "data": [
                {"id": "x-ai/grok-4.3", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
                {"id": "llama3.2", "pricing": {"prompt": "0", "completion": "0"}},
            ]
        }
        self._save_catalog("Local", "ollama", {"data": [{"id": "grok-4.3"}, {"id": "llama3.2"}]}, secret="")
        self._save_catalog("Router", "openrouter", catalog, secret="router-key")
        set_inference_defaults(self.conn, self.editor, ["llama3.2"])
        job_id, _manuscript = self._job(persona_id)
        posts = []

        def fake_get(url, headers, timeout):
            if urlsplit(url).port == 11434:
                return {"data": [{"id": "grok-4.3"}, {"id": "llama3.2"}]}
            return catalog

        def fake_post(url, headers, body, timeout):
            posts.append((url, body["model"]))
            self.assertNotIn("router-key", body["messages"][0]["content"])
            return self._reply()

        with patch("app.llm._get", fake_get), patch("app.llm._post", fake_post):
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        self.assertEqual(posts, [("https://openrouter.ai/api/v1/chat/completions", "x-ai/grok-4.3")])
        model_id = self.conn.execute("SELECT model_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["model_id"]
        self.assertEqual(model_id, "x-ai/grok-4.3")

    def test_a_missing_model_uses_the_cheaper_default(self):
        persona_id = self._persona(ONE)
        catalog = {
            "data": [
                {"id": "dear-model", "pricing": {"prompt": "0.000005", "completion": "0.000005"}},
                {"id": "cheap-model", "pricing": {"prompt": "0.000001", "completion": "0.000001"}},
            ]
        }
        self._save_catalog("Desk", "openai", catalog, secret="openai-key")
        set_inference_defaults(self.conn, self.editor, ["dear-model", "cheap-model"])
        job_id, _manuscript = self._job(persona_id)
        posts = []

        def fake_post(url, headers, body, timeout):
            posts.append(body["model"])
            self.assertNotIn("grok-4.3", body["model"])
            if body["model"] == "cheap-model":
                raise RuntimeError("the model service refused the call")
            return self._reply()

        with patch("app.llm._get", return_value=catalog), patch("app.llm._post", fake_post):
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        self.assertEqual(posts, ["cheap-model", "dear-model"])
        model_id = self.conn.execute("SELECT model_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["model_id"]
        self.assertEqual(model_id, "cheap-model")

    def test_a_file_without_a_model_uses_a_default(self):
        persona_id = self._persona({"title": "Plain", "steps": ONE["steps"]})
        self._save("Desk", "xai", ["llama3.2"])
        set_inference_defaults(self.conn, self.editor, ["llama3.2"])
        job_id, _manuscript = self._job(persona_id)
        posts = []

        def fake_post(url, headers, body, timeout):
            posts.append(body["model"])
            return self._reply()

        with patch("app.llm._get", return_value=_models(["llama3.2"])), patch("app.llm._post", fake_post):
            self.assertEqual(run_once(config.DATABASE_PATH), job_id)
        self.assertEqual(posts, ["llama3.2"])
        model_id = self.conn.execute("SELECT model_id FROM jobs WHERE id = ?", (job_id,)).fetchone()["model_id"]
        self.assertEqual(model_id, "llama3.2")

    def test_a_failed_call_does_not_switch_to_a_default_model(self):
        persona_id = self._persona(ONE)
        catalog = {"data": [{"id": "grok-4.3"}, {"id": "llama3.2", "pricing": {"prompt": "0", "completion": "0"}}]}
        self._save_catalog("Desk", "xai", catalog)
        set_inference_defaults(self.conn, self.editor, ["llama3.2"])
        job_id, _manuscript = self._job(persona_id)
        posts = []

        def fake_post(url, headers, body, timeout):
            posts.append(body["model"])
            raise RuntimeError("the model service refused the call")

        with patch("app.llm._get", return_value=catalog), patch("app.llm._post", fake_post):
            with self.assertRaises(llm.InferenceError) as caught:
                run_once(config.DATABASE_PATH)
        self.assertEqual(posts, ["grok-4.3"])
        self.assertEqual(str(caught.exception), "no configured provider could run grok-4.3")
        row = self.conn.execute("SELECT error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["error_code"], "no configured provider could run grok-4.3")

    def test_missing_defaults_name_both_the_model_and_the_defaults(self):
        persona_id = self._persona(ONE)
        self._save("Desk", "ollama", ["llama3.2"], secret="")
        set_inference_defaults(self.conn, self.editor, ["missing-model"])
        job_id, _manuscript = self._job(persona_id)
        with patch("app.llm._get", return_value=_models(["llama3.2"])), patch("app.llm._post") as post:
            with self.assertRaises(llm.InferenceError) as caught:
                run_once(config.DATABASE_PATH)
            post.assert_not_called()
        self.assertEqual(str(caught.exception), "no configured provider offers grok-4.3 or a default model")
        row = self.conn.execute("SELECT error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["error_code"], "no configured provider offers grok-4.3 or a default model")

    def test_a_file_without_a_model_fails_when_no_default_is_listed(self):
        persona_id = self._persona({"title": "Plain", "steps": ONE["steps"]})
        self._save("Desk", "ollama", ["llama3.2"], secret="")
        set_inference_defaults(self.conn, self.editor, ["grok-4.3"])
        job_id, _manuscript = self._job(persona_id)
        with patch("app.llm._get") as get, patch("app.llm._post") as post:
            get.return_value = _models(["llama3.2"])
            with self.assertRaises(llm.InferenceError) as caught:
                run_once(config.DATABASE_PATH)
            get.assert_called()
            post.assert_not_called()
        self.assertEqual(str(caught.exception), "no configured provider offers a default model")
        row = self.conn.execute("SELECT error_code FROM jobs WHERE id = ?", (job_id,)).fetchone()
        self.assertEqual(row["error_code"], "no configured provider offers a default model")

    def test_an_empty_default_list_clears_the_saved_ones(self):
        set_inference_defaults(self.conn, self.editor, ["llama3.2", "grok-4.3"])
        self.assertEqual(inference_defaults(self.conn, self.editor["organization_id"]), ["llama3.2", "grok-4.3"])
        with self.assertRaises(ValueError) as twice:
            set_inference_defaults(self.conn, self.editor, ["llama3.2", "llama3.2"])
        self.assertEqual(str(twice.exception), "a default model is listed twice")
        with self.assertRaises(ValueError) as invalid:
            set_inference_defaults(self.conn, self.editor, ["not a model"])
        self.assertEqual(str(invalid.exception), "a default model is not valid")
        self.assertEqual(inference_defaults(self.conn, self.editor["organization_id"]), ["llama3.2", "grok-4.3"])
        with self.assertRaises(ValueError) as limited:
            set_inference_defaults(self.conn, self.editor, [f"m{index}" for index in range(21)])
        self.assertEqual(str(limited.exception), "at most 20 default models")
        set_inference_defaults(self.conn, self.editor, [])
        self.assertEqual(inference_defaults(self.conn, self.editor["organization_id"]), [])

    def test_the_tool_returns_defaults_and_a_published_price(self):
        catalog = {
            "data": [
                {"id": "cheap-model", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
            ]
        }
        self._save_catalog("Router", "openrouter", catalog, secret="tool-key")
        response = handle_message(
            self.conn,
            self.user,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "set_inference_defaults", "arguments": {"models": ["cheap-model", "grok-4.3"]}},
            },
            mcp_url=MCP_URL,
        )
        text = response["result"]["content"][0]["text"]
        payload = json.loads(text)
        self.assertEqual(payload["defaults"], ["cheap-model", "grok-4.3"])
        self.assertEqual(payload["providers"][0]["prices"]["cheap-model"], {"prompt": 1_000_000, "completion": 2_000_000})
        self.assertNotIn("tool-key", text)
        cleared = handle_message(
            self.conn,
            self.user,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "set_inference_defaults", "arguments": {"models": []}},
            },
            mcp_url=MCP_URL,
        )
        self.assertEqual(json.loads(cleared["result"]["content"][0]["text"])["defaults"], [])
        self.assertEqual(inference_defaults(self.conn, self.editor["organization_id"]), [])
