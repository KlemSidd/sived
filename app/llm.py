"""One model call for the worker.

The desk talks to one provider per process. Named hosts are fixed. A local
server is ollama, or compatible for any other OpenAI-style /chat/completions
endpoint. The key stays in the environment.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app import config

# style openai posts {base}/chat/completions. anthropic posts {base}/messages.
PROVIDERS = {
    "xai": {"base": "https://api.x.ai/v1", "style": "openai", "key": True},
    "openai": {"base": "https://api.openai.com/v1", "style": "openai", "key": True},
    "anthropic": {"base": "https://api.anthropic.com/v1", "style": "anthropic", "key": True},
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "style": "openai",
        "key": True,
    },
    "groq": {"base": "https://api.groq.com/openai/v1", "style": "openai", "key": True},
    "openrouter": {"base": "https://openrouter.ai/api/v1", "style": "openai", "key": True},
    "ollama": {"base": "http://127.0.0.1:11434/v1", "style": "openai", "key": False},
    "compatible": {"base": "", "style": "openai", "key": False},
}

NAMES = tuple(PROVIDERS)
# Letters, digits, and the punctuation a vendor puts in a model id.
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\-+]{1,160}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}$")
DEFAULT_LIMIT = 20
# Dollars per token, times this, is integer micros per million tokens.
_MICROS_PER_MILLION = Decimal("1000000000000")


class InferenceError(Exception):
    """The workflow model cannot be run on the providers saved for this practice."""


def settings() -> dict:
    """The process provider. The key is included for the call and omitted from reports."""
    provider = (config.LLM_PROVIDER or "xai").strip().lower()
    known = provider in PROVIDERS
    spec = PROVIDERS.get(provider, {})
    mode = (config.LLM_MODE or config.XAI_MODE or "local").strip()
    model = (config.LLM_MODEL or "").strip()
    if not model and provider == "xai":
        model = (config.XAI_MODEL or "grok-4.3").strip()
    key = (config.LLM_API_KEY or "").strip()
    if not key and provider == "xai":
        key = (config.XAI_API_KEY or "").strip()
    base = _base(provider, spec)
    return {
        "provider": provider,
        "known": known,
        "style": spec.get("style", "openai"),
        "mode": mode,
        "model": model,
        "base": base,
        "key": key,
        "key_set": bool(key),
        "needs_key": bool(spec.get("key", True)) if known else True,
        "timeout": _timeout(provider),
    }


def public(spec: dict | None = None) -> dict:
    spec = settings() if spec is None else spec
    return {
        "provider": spec["provider"],
        "mode": spec["mode"],
        "model": spec["model"],
        "base": spec["base"],
        "key_set": spec["key_set"],
        "needs_key": spec["needs_key"],
    }


def ready(spec: dict | None = None) -> bool:
    spec = settings() if spec is None else spec
    if not spec["known"] or spec["mode"] != "sync" or not spec["model"] or not spec["base"]:
        return False
    if spec["needs_key"] and not spec["key_set"]:
        return False
    return True


def model_id() -> str:
    spec = settings()
    if spec["model"]:
        return spec["model"]
    if spec["provider"] == "xai":
        return "grok-4.3"
    return spec["provider"]


def gap(spec: dict | None = None) -> str:
    """What sync mode still needs. Empty when this provider is ready or is xAI."""
    spec = settings() if spec is None else spec
    if spec["provider"] == "xai" or not spec["known"] or ready(spec):
        return ""
    missing = []
    if spec["mode"] != "sync":
        missing.append("Set LLM_MODE=sync.")
    if spec["needs_key"] and not spec["key_set"]:
        missing.append("Set LLM_API_KEY.")
    if not spec["model"]:
        missing.append("Set LLM_MODEL.")
    if not spec["base"]:
        missing.append("Set LLM_BASE_URL to an http or https host with no password in the URL.")
    return f"Letters are not calling the model. Provider is {spec['provider']}. " + " ".join(missing)


def complete(prompt: str, max_tokens: int) -> tuple[str, dict]:
    spec = settings()
    url, headers, body = request(spec, prompt, max_tokens)
    payload = _post(url, headers, body, spec["timeout"])
    return _parse(spec["style"], payload, prompt)


def request(spec: dict, prompt: str, max_tokens: int) -> tuple[str, dict, dict]:
    if spec["style"] == "anthropic":
        url = spec["base"].rstrip("/") + "/messages"
        headers = {
            "x-api-key": spec["key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": spec["model"],
            "max_tokens": max_tokens,
            "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}],
        }
        return url, headers, body
    url = spec["base"].rstrip("/") + "/chat/completions"
    headers = {"content-type": "application/json"}
    if spec["key"]:
        headers["authorization"] = f"Bearer {spec['key']}"
    body = {
        "model": spec["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    return url, headers, body


def _base(provider: str, spec: dict) -> str:
    if provider == "xai":
        return (config.XAI_BASE_URL or spec.get("base") or "").strip().rstrip("/")
    if provider == "ollama":
        override = (config.LLM_BASE_URL or "").strip()
        return _clean(override) if override else spec.get("base", "")
    if provider == "compatible":
        return _clean(config.LLM_BASE_URL or "")
    if provider in PROVIDERS:
        return spec.get("base", "")
    return ""


def _clean(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return url


def _timeout(provider: str) -> int:
    raw = (config.LLM_TIMEOUT or "").strip()
    if raw.isdigit():
        return min(3600, max(1, int(raw)))
    if provider in ("ollama", "compatible"):
        return 600
    return 120


def _post(url: str, headers: dict, body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError:
        raise RuntimeError("the model service refused the call") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise RuntimeError("the model service could not be reached") from None
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("the model service refused the call") from None
    if not isinstance(payload, dict):
        raise RuntimeError("the model service refused the call")
    return payload


def clean_label(label: str) -> str:
    label = " ".join((label or "").split())
    if _LABEL_RE.fullmatch(label) is None:
        raise ValueError("the provider label is not valid")
    return label


def prepare_account(*, provider: str, base_url: str, secret: str, saved_secret: str = "") -> dict:
    """One saved provider, ready to list models or take a call. The key stays in the dict."""
    name = (provider or "").strip().lower()
    if name not in PROVIDERS:
        raise ValueError("that provider is not available")
    spec = PROVIDERS[name]
    key = (secret or "").strip() or (saved_secret or "").strip()
    if spec["key"] and not key:
        raise ValueError("an API key is required")
    base = _account_base(name, base_url)
    if not base:
        raise ValueError("set the base URL to an http or https host with no password in the URL")
    return {
        "provider": name,
        "style": spec["style"],
        "base": base,
        "key": key,
        "needs_key": bool(spec["key"]),
        "timeout": _timeout(name),
        "models": [],
    }


def account_from_row(row) -> dict:
    account = prepare_account(
        provider=row["provider"],
        base_url=row["base_url"],
        secret=row["secret"],
    )
    account["id"] = row["id"]
    account["label"] = row["label"]
    account["models"] = stored_catalog(row["models_json"])
    return account


def probe_models(account: dict) -> list[dict]:
    """GET /models. A failure raises ValueError and does not include the reply."""
    url = account["base"].rstrip("/") + "/models"
    payload = _get(url, _list_headers(account), account["timeout"])
    models = catalog_from_payload(payload)
    if not models:
        raise ValueError("that provider did not list a model")
    return models


def refreshed_models(account: dict) -> list[dict] | None:
    """The live catalog, or None when the list could not be read."""
    try:
        return probe_models(account)
    except ValueError:
        return None


def candidates(wanted: str, accounts: list[dict]) -> list[dict]:
    """Providers that list `wanted`, cheapest published price first.

    An exact id beats a same-price id that merely ends with `/{model}`.
    A provider that did not publish a price stays in list order, after every priced one.
    Each provider contributes one target: the exact id, or else its shortest suffix.
    """
    found = []
    needle = "/" + wanted
    for index, account in enumerate(accounts):
        entries = _as_entries(account.get("models"))
        exact = next((entry for entry in entries if entry["id"] == wanted), None)
        if exact is not None:
            found.append((_order_key(exact, wanted, index), _target(account, exact)))
            continue
        matches = [entry for entry in entries if entry["id"].endswith(needle)]
        if not matches:
            continue
        chosen = min(matches, key=lambda entry: (len(entry["id"]), entry["id"]))
        found.append((_order_key(chosen, wanted, index), _target(account, chosen)))
    found.sort(key=lambda item: item[0])
    return [item[1] for item in found]


def fallback_targets(defaults: list[str], accounts: list[dict]) -> list[tuple[str, list[dict]]]:
    """Defaults a provider lists, cheapest published price first.

    A default with no published price stays in list order, after every priced default.
    The price of a default is the cheapest published price among providers that list it.
    """
    priced = []
    plain = []
    for index, name in enumerate(defaults):
        picked = candidates(name, accounts)
        if not picked:
            continue
        price = picked[0].get("price")
        if price is None:
            plain.append((index, name, picked))
        else:
            priced.append((price, index, name, picked))
    priced.sort(key=lambda item: (item[0], item[1]))
    plain.sort(key=lambda item: item[0])
    ordered = [(name, picked) for _price, _index, name, picked in priced]
    ordered.extend((name, picked) for _index, name, picked in plain)
    return ordered


def complete_routed(targets: list[dict], prompt: str, max_tokens: int, wanted: str) -> tuple[str, dict]:
    """Try each provider that lists the model. The next one is used when a call fails."""
    for target in targets:
        spec = {
            "style": target["style"],
            "base": target["base"],
            "key": target["key"],
            "model": target["model"],
            "timeout": target["timeout"],
        }
        try:
            url, headers, body = request(spec, prompt, max_tokens)
            payload = _post(url, headers, body, spec["timeout"])
            return _parse(spec["style"], payload, prompt)
        except RuntimeError:
            continue
    raise InferenceError(f"no configured provider could run {wanted}")


def _account_base(provider: str, base_url: str) -> str:
    spec = PROVIDERS[provider]
    if provider == "compatible":
        return _clean(base_url)
    if provider == "ollama":
        override = (base_url or "").strip()
        return _clean(override) if override else spec["base"]
    return spec.get("base", "")


def _stored_model_ids(raw) -> list[str]:
    return [entry["id"] for entry in stored_catalog(raw)]


def stored_catalog(raw) -> list[dict]:
    """A saved catalog. Entries are id strings or objects with an optional price."""
    try:
        models = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(models, list):
        return []
    return _as_entries(models)


def catalog_payload(entries: list[dict]) -> list:
    """The JSON shape stored for a catalog. A price is kept only when both sides are known."""
    stored = []
    seen = set()
    for entry in entries:
        name = entry.get("id")
        if not isinstance(name, str) or name in seen or not MODEL_RE.fullmatch(name):
            continue
        seen.add(name)
        prompt = _stored_micros(entry.get("prompt"))
        completion = _stored_micros(entry.get("completion"))
        if prompt is None or completion is None:
            stored.append(name)
        else:
            stored.append({"id": name, "prompt": prompt, "completion": completion})
    return stored


def catalog_from_payload(payload: dict) -> list[dict]:
    """Model ids from a provider list. A price is kept only when both sides were published."""
    data = payload.get("data")
    if not isinstance(data, list):
        data = payload.get("models") if isinstance(payload.get("models"), list) else []
    found = []
    seen = set()
    for item in data:
        price = None
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = item.get("id") or item.get("name") or ""
            price = _published_pair(item)
        else:
            continue
        if not isinstance(name, str) or not MODEL_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        entry = {"id": name}
        if price is not None:
            entry["prompt"], entry["completion"] = price
        found.append(entry)
    return found


def price_label(prompt: int, completion: int) -> str:
    """Dollars per million tokens, input then output."""
    return f"{_dollars(prompt)}, {_dollars(completion)}"


def clean_default_models(models) -> list[str]:
    """The practice default list. Blank entries are skipped. Order is kept."""
    if not isinstance(models, list):
        raise ValueError("a default model is not valid")
    cleaned = []
    for item in models:
        if not isinstance(item, str):
            raise ValueError("a default model is not valid")
        name = item.strip()
        if not name:
            continue
        if not MODEL_RE.fullmatch(name):
            raise ValueError("a default model is not valid")
        if name in cleaned:
            raise ValueError("a default model is listed twice")
        cleaned.append(name)
    if len(cleaned) > DEFAULT_LIMIT:
        raise ValueError("at most 20 default models")
    return cleaned


def _as_entries(models) -> list[dict]:
    entries = []
    seen = set()
    for item in models or []:
        if isinstance(item, str):
            name = item
            price = None
        elif isinstance(item, dict):
            name = item.get("id") if isinstance(item.get("id"), str) else ""
            if not name and isinstance(item.get("name"), str):
                name = item["name"]
            prompt = _stored_micros(item.get("prompt"))
            completion = _stored_micros(item.get("completion"))
            price = None if prompt is None or completion is None else (prompt, completion)
        else:
            continue
        if not isinstance(name, str) or not MODEL_RE.fullmatch(name) or name in seen:
            continue
        seen.add(name)
        entry = {"id": name}
        if price is not None:
            entry["prompt"], entry["completion"] = price
        entries.append(entry)
    return entries


def _order_key(entry: dict, wanted: str, index: int) -> tuple:
    exact = 0 if entry["id"] == wanted else 1
    prompt = _stored_micros(entry.get("prompt"))
    completion = _stored_micros(entry.get("completion"))
    if prompt is None or completion is None:
        return (1, 0, 0, exact, index)
    return (0, prompt, completion, exact, index)


def _target(account: dict, entry: dict) -> dict:
    target = {**account, "model": entry["id"]}
    prompt = _stored_micros(entry.get("prompt"))
    completion = _stored_micros(entry.get("completion"))
    if prompt is not None and completion is not None:
        target["price"] = (prompt, completion)
    return target


def _published_pair(item: dict) -> tuple[int, int] | None:
    """OpenRouter-style pricing.prompt and pricing.completion, dollars per token.

    Both sides are required. A missing, negative, or non-decimal value is unknown.
    A fractional JSON number is not accepted: a binary float is not the published decimal.
    An integer is exact. Unknown is not free, and a local model is not given a zero price.
    """
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = _dollars_per_token(pricing.get("prompt"))
    completion = _dollars_per_token(pricing.get("completion"))
    if prompt is None or completion is None:
        return None
    return prompt, completion


def _dollars_per_token(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        raw = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 40:
            return None
        try:
            raw = Decimal(text)
        except InvalidOperation:
            return None
    else:
        return None
    if not raw.is_finite() or raw < 0:
        return None
    micros = raw * _MICROS_PER_MILLION
    integral = micros.to_integral_value()
    if micros != integral or integral > Decimal("1000000000000000000"):
        return None
    return int(integral)


def _stored_micros(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _dollars(micros: int) -> str:
    amount = Decimal(micros) / Decimal("1000000")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "$" + text


def _list_headers(account: dict) -> dict:
    if account["style"] == "anthropic":
        return {
            "x-api-key": account["key"],
            "anthropic-version": "2023-06-01",
        }
    headers = {}
    if account.get("key"):
        headers["authorization"] = f"Bearer {account['key']}"
    return headers


def _get(url: str, headers: dict, timeout: int) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("that provider refused the credential") from None
        raise ValueError("that provider could not be reached") from None
    except (TimeoutError, urllib.error.URLError, OSError):
        raise ValueError("that provider could not be reached") from None
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("that provider could not be reached") from None
    if not isinstance(payload, dict):
        raise ValueError("that provider did not list a model")
    return payload


def _parse(style: str, payload: dict, prompt: str) -> tuple[str, dict]:
    if style == "anthropic":
        parts = payload.get("content") or []
        text = "".join(
            part.get("text") or ""
            for part in parts
            if isinstance(part, dict) and part.get("type", "text") == "text"
        )
        usage = payload.get("usage") or {}
        in_tok = usage.get("input_tokens")
        out_tok = usage.get("output_tokens")
    else:
        choices = payload.get("choices") or []
        message = choices[0].get("message") if choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, list):
            text = "".join(
                part.get("text") or ""
                for part in content
                if isinstance(part, dict)
            )
        else:
            text = content or ""
        usage = payload.get("usage") or {}
        in_tok = usage.get("prompt_tokens")
        out_tok = usage.get("completion_tokens")
    if not str(text).strip():
        raise RuntimeError("the model service refused the call")
    return str(text), {
        "input_tokens": int(in_tok or max(1, len(prompt) // 4)),
        "output_tokens": int(out_tok or max(1, len(str(text).split()))),
    }
