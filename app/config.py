import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", DATA_DIR / "editting.sqlite"))
OBJECT_DIR = DATA_DIR / "objects"
DESK_PERSONA_SKILL = ROOT / "share" / "desk-persona" / "SKILL.md"
SCHEMA_PATH = ROOT / "migrations" / "001_init.sql"

DEV_LOGIN = os.environ.get("DEV_LOGIN", "0") == "1"
# PoC switch. Submitting a manuscript stores it and skips the editing workflow.
# No prompt is built and no model is called. Sign-in lists every account.
# Either flag skips the Patreon membership check. Leave both unset on a real desk.
DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"
# Public callback for every editor's own Patreon client. Each editor registers
# this exact URI on their client. Leave it empty when the request already
# carries the public scheme and host. Set it only when that request does not.
PATREON_REDIRECT_URI = os.environ.get("PATREON_REDIRECT_URI", "")
# A proxy on this machine is trusted without this. Set it for a remote
# load balancer. The proxy replaces Forwarded and X-Forwarded-* headers.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
# In-process TLS. Leave both empty when a proxy terminates TLS, or when
# ACME_DOMAIN requests a Let's Encrypt certificate instead.
TLS_CERT = os.environ.get("TLS_CERT", "")
TLS_KEY = os.environ.get("TLS_KEY", "")
# Let's Encrypt for the desk's own listener. Empty leaves ACME off.
# A proxy that terminates TLS does not need this. Port 80 must reach the
# challenge, unless ACME_HTTP_PORT is 0 and a proxy forwards that path.
ACME_DOMAIN = os.environ.get("ACME_DOMAIN", "")
ACME_EMAIL = os.environ.get("ACME_EMAIL", "")
ACME_DIRECTORY = os.environ.get("ACME_DIRECTORY", "")
ACME_STAGING = os.environ.get("ACME_STAGING", "0") == "1"
ACME_HTTP_PORT = os.environ.get("ACME_HTTP_PORT", "80")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8080"))

XAI_API_KEY = os.environ.get("XAI_API_KEY", "")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
XAI_MODEL = os.environ.get("XAI_MODEL", "grok-4.3")
# local: no network, deterministic diagnoses. sync: one completion per call.
# Ignored while DEMO_MODE is on. LLM_MODE overrides this when it is set.
XAI_MODE = os.environ.get("XAI_MODE", "local")
# One provider for the worker. xai keeps the XAI_* variables. Other names use
# LLM_API_KEY and LLM_MODEL. ollama and compatible may set LLM_BASE_URL.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "xai")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "")
LLM_MODE = os.environ.get("LLM_MODE", "")
LLM_TIMEOUT = os.environ.get("LLM_TIMEOUT", "")

DAILY_STOP_MICROS = int(os.environ.get("DAILY_STOP_MICROS", "75000000"))
JOB_DOLLAR_CAP_MICROS = 8_000_000
JOB_TOKEN_CAP = 4_000_000
PAYOUT_MINIMUM_CENTS = 2500

MODEL_ID = XAI_MODEL
