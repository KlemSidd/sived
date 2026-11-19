import json
import unittest
from unittest.mock import patch

from app import config, llm


class LlmProviderTests(unittest.TestCase):
    def setUp(self):
        self.previous = (
            config.LLM_PROVIDER,
            config.LLM_MODE,
            config.LLM_MODEL,
            config.LLM_API_KEY,
            config.LLM_BASE_URL,
            config.LLM_TIMEOUT,
            config.XAI_MODE,
            config.XAI_MODEL,
            config.XAI_API_KEY,
            config.XAI_BASE_URL,
            config.DEMO_MODE,
        )
        config.LLM_PROVIDER = "xai"
        config.LLM_MODE = ""
        config.LLM_MODEL = ""
        config.LLM_API_KEY = ""
        config.LLM_BASE_URL = ""
        config.LLM_TIMEOUT = ""
        config.XAI_MODE = "local"
        config.XAI_MODEL = "grok-4.3"
        config.XAI_API_KEY = ""
        config.XAI_BASE_URL = "https://api.x.ai/v1"
        config.DEMO_MODE = False

    def tearDown(self):
        (
            config.LLM_PROVIDER,
            config.LLM_MODE,
            config.LLM_MODEL,
            config.LLM_API_KEY,
            config.LLM_BASE_URL,
            config.LLM_TIMEOUT,
            config.XAI_MODE,
            config.XAI_MODEL,
            config.XAI_API_KEY,
            config.XAI_BASE_URL,
            config.DEMO_MODE,
        ) = self.previous

    def test_xai_stays_the_default(self):
        spec = llm.settings()
        self.assertEqual(spec["provider"], "xai")
        self.assertEqual(spec["model"], "grok-4.3")
        self.assertEqual(spec["base"], "https://api.x.ai/v1")
        self.assertFalse(llm.ready(spec))
        config.XAI_MODE = "sync"
        config.XAI_API_KEY = "xai-secret"
        spec = llm.settings()
        self.assertTrue(llm.ready(spec))
        self.assertEqual(llm.model_id(), "grok-4.3")
        url, headers, body = llm.request(spec, "Hello", 20)
        self.assertEqual(url, "https://api.x.ai/v1/chat/completions")
        self.assertEqual(headers["authorization"], "Bearer xai-secret")
        self.assertEqual(body["model"], "grok-4.3")
        self.assertNotIn("xai-secret", url)

    def test_ollama_needs_a_model_and_no_key(self):
        config.LLM_PROVIDER = "ollama"
        config.LLM_MODE = "sync"
        spec = llm.settings()
        self.assertIn("Set LLM_MODEL.", llm.gap(spec))
        self.assertNotIn("grok-4.3", llm.gap(spec))
        self.assertNotIn("XAI_API_KEY", llm.gap(spec))
        config.LLM_MODEL = "llama3.2"
        spec = llm.settings()
        self.assertTrue(llm.ready(spec))
        self.assertEqual(spec["base"], "http://127.0.0.1:11434/v1")
        self.assertFalse(spec["needs_key"])
        self.assertEqual(llm.model_id(), "llama3.2")

    def test_compatible_rejects_a_url_with_a_password(self):
        config.LLM_PROVIDER = "compatible"
        config.LLM_MODE = "sync"
        config.LLM_MODEL = "local"
        config.LLM_BASE_URL = "http://user:pass@127.0.0.1:8000/v1"
        spec = llm.settings()
        self.assertEqual(spec["base"], "")
        self.assertFalse(llm.ready(spec))
        self.assertIn("LLM_BASE_URL", llm.gap(spec))
        config.LLM_BASE_URL = "http://127.0.0.1:8000/v1"
        spec = llm.settings()
        self.assertTrue(llm.ready(spec))
        url, headers, _body = llm.request(spec, "Hello", 20)
        self.assertEqual(url, "http://127.0.0.1:8000/v1/chat/completions")
        self.assertNotIn("authorization", headers)

    def test_anthropic_uses_its_own_request(self):
        config.LLM_PROVIDER = "anthropic"
        config.LLM_MODE = "sync"
        config.LLM_MODEL = "claude"
        config.LLM_API_KEY = "ant-secret"
        spec = llm.settings()
        url, headers, body = llm.request(spec, "Hello", 20)
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "ant-secret")
        self.assertNotIn("authorization", headers)
        self.assertEqual(body["messages"][0]["content"], "Hello")
        self.assertNotIn("ant-secret", json.dumps(body))

    def test_unknown_provider_is_not_ready(self):
        config.LLM_PROVIDER = "somewhere"
        config.LLM_MODE = "sync"
        config.LLM_MODEL = "m"
        config.LLM_API_KEY = "k"
        spec = llm.settings()
        self.assertFalse(spec["known"])
        self.assertFalse(llm.ready(spec))
        self.assertEqual(llm.gap(spec), "")

    def test_complete_reads_openai_and_anthropic_usage(self):
        config.LLM_PROVIDER = "openai"
        config.LLM_MODE = "sync"
        config.LLM_MODEL = "gpt"
        config.LLM_API_KEY = "sk"
        with patch("app.llm._post", return_value={"choices": [{"message": {"content": "Noted"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}):
            text, usage = llm.complete("Hello", 20)
        self.assertEqual(text, "Noted")
        self.assertEqual(usage["input_tokens"], 4)
        self.assertEqual(usage["output_tokens"], 2)

        config.LLM_PROVIDER = "anthropic"
        with patch("app.llm._post", return_value={"content": [{"type": "text", "text": "Noted"}], "usage": {"input_tokens": 5, "output_tokens": 3}}):
            text, usage = llm.complete("Hello", 20)
        self.assertEqual(text, "Noted")
        self.assertEqual(usage["output_tokens"], 3)

    def test_a_refused_call_does_not_include_the_body(self):
        config.LLM_PROVIDER = "openai"
        config.LLM_MODE = "sync"
        config.LLM_MODEL = "gpt"
        config.LLM_API_KEY = "sk"

        def boom(*_args):
            raise RuntimeError("the model service refused the call")

        with patch("app.llm._post", boom):
            with self.assertRaises(RuntimeError) as caught:
                llm.complete("manuscript text", 20)
        self.assertNotIn("manuscript", str(caught.exception))
