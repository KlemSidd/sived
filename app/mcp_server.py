"""MCP for the owner, editors, and assistants.

A local agent calls these tools over HTTP on the desk. The caller can do what
that login can do in SIVED, including seeing a workload and running
a queued one locally. A call cannot reach another practice.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from urllib.parse import urlsplit

from app import config, llm, patreon
from app.agent import PLATFORM_SKILLS, parse_skills
from app.estimate import micros_to_cents
from app.harness import inspect_root
from app.money import usd
from app.package import ensure_persona_package
from app.services import (
    PERMISSIONS,
    account_roles,
    allowed,
    author_notices_account,
    clear_author_notices,
    clear_invitation_mail,
    clear_inference,
    editor_for_user,
    inference_defaults,
    inference_public,
    inference_rows,
    install_persona_files,
    invitation_mail_account,
    invite_editor,
    jobs_visible,
    mail_public,
    pending_editor_invites,
    post_message,
    practice_admin,
    practice_editors,
    practice_model_cost_micros,
    patreon_ready,
    practice_appearance,
    publish_persona,
    read_object,
    remove_editor,
    revoke_editor_invite,
    search_manuscripts,
    set_assistant_permissions,
    set_author_notices,
    set_inference,
    set_inference_defaults,
    set_invitation_mail,
    set_patreon_integration,
    set_practice_settings,
    user_is_owner,
)
from app.worker import run_job

class _MethodNotFound(Exception):
    pass


_VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTRUCTIONS = (
    "SIVED for this login. whoami shows the role and which permissions are on. "
    "A workload is one manuscript run. list_workloads and get_workload show it. "
    "run_workload runs a queued workload on this machine and does not call the model API. "
    "A persona is a workflow.json file. install_agent_files writes that file, manifest.json, "
    "and any custom skill files. "
    "Platform skills are author-dev, author-compendium, and author-readback. "
    "publish_persona makes a persona available. This desk does not charge an author. "
    "The practice editor connects their own Patreon client with set_patreon_integration. "
    "patreon_integration shows the redirect URI and webhook URL and does not return secrets. "
    "practice_settings shows the practice name, colors, and Patreon status. "
    "set_practice_settings changes the name and colors. It does not return secrets. "
    "author_notices is this practice's mailbox for telling an author a manuscript is ready "
    "or did not finish. set_author_notices checks SMTP or an HTTPS provider before it saves. "
    "It does not return the secret. "
    "inference lists the providers saved for this practice, the models each one offers, "
    "and the default models. set_inference saves one provider after it lists models. "
    "set_inference_defaults saves the default models. A workflow's named model is used "
    "when a provider lists it. Defaults are used otherwise, cheapest published price first. "
    "The owner uses invitation_mail and set_invitation_mail for the mailbox that emails "
    "editor and admin invites. Saving that mailbox does not send an invite. "
    "deployment reports what this process still needs before other people use the desk. "
    "It does not return a key or a secret. The checklist is README Deploy. "
    "The earlier design docs are not the checklist. "
    "search_manuscripts finds authors to edit by hand and does not change the automated run. "
    "Messages, model cost, and assistants match SIVED and follow this login's permissions."
)


def handle_message(conn, user, message, *, mcp_url: str) -> dict | None:
    """One JSON-RPC message. None means the client sent a notification."""
    if not isinstance(message, dict) or isinstance(message.get("method"), str) is False:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "invalid request")
    method = message["method"]
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "invalid params")
    try:
        result = _dispatch(conn, user, method, params, mcp_url)
    except _MethodNotFound:
        return _error(msg_id, -32601, "method not found")
    except ValueError as exc:
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": msg_id, "result": _tool_text(str(exc), error=True)}
        return _error(msg_id, -32602, str(exc))
    except Exception:
        return _error(msg_id, -32603, "the desk could not complete that")
    if "id" not in message or result is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _dispatch(conn, user, method: str, params: dict, mcp_url: str):
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if isinstance(asked, str) and _VERSION_RE.match(asked) else "2025-03-26"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sived", "version": "1"},
            "instructions": INSTRUCTIONS,
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": _tool_defs(conn, user)}
    if method == "tools/call":
        return _call_tool(conn, user, params, mcp_url)
    if method == "resources/list":
        return {"resources": []}
    if method == "prompts/list":
        return {"prompts": []}
    if method.startswith("notifications/"):
        return None
    raise _MethodNotFound()


def _tool_defs(conn, user) -> list[dict]:
    tools = [_WHOAMI]
    if user["role"] == "owner":
        tools.append(_LIST_PRACTICES)
    if user["role"] in ("owner", "editor"):
        tools.extend(
            (
                _LIST_PERSONAS,
                _GET_PERSONA,
                _PUBLISH_PERSONA,
                _LIST_AGENT_FILES,
                _WRITE_AGENT_FILE,
                _INSTALL_AGENT_FILES,
                _LIST_MANUSCRIPTS,
                _LIST_WORKLOADS,
                _GET_WORKLOAD,
                _RUN_WORKLOAD,
                _SEND_MESSAGE,
                _SEARCH_MANUSCRIPTS,
                _MODEL_COST,
                _LIST_ASSISTANTS,
                _INVITE_ASSISTANT,
                _REVOKE_ASSISTANT_INVITE,
                _SET_ASSISTANT_PERMISSIONS,
                _REMOVE_ASSISTANT,
            )
        )
    editor = editor_for_user(conn, user["id"])
    if (
        editor is not None
        and editor["status"] == "active"
        and editor["is_org_admin"]
        and user["role"] in ("owner", "editor")
    ):
        tools.extend(
            (
                _PATREON_INTEGRATION,
                _SET_PATREON_INTEGRATION,
                _PRACTICE_SETTINGS,
                _SET_PRACTICE_SETTINGS,
                _AUTHOR_NOTICES,
                _SET_AUTHOR_NOTICES,
                _INFERENCE,
                _SET_INFERENCE,
                _SET_INFERENCE_DEFAULTS,
                _DEPLOYMENT,
            )
        )
    if user_is_owner(user):
        tools.extend((_INVITATION_MAIL, _SET_INVITATION_MAIL))
    return tools


_WHOAMI = {
    "name": "whoami",
    "description": "This login: role, practice, and which desk permissions are on.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_LIST_PRACTICES = {
    "name": "list_practices",
    "description": "Practices on the platform. Owner only. This does not open another editor's persona.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_LIST_PERSONAS = {
    "name": "list_personas",
    "description": "Personas on this login's practice. Requires Personas.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_GET_PERSONA = {
    "name": "get_persona",
    "description": "One persona: workflow.json, the package files, and custom skills. Requires Personas.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "persona_id": {"type": "string", "description": "Omit to read the practice persona."},
        },
        "additionalProperties": False,
    },
}
_LIST_AGENT_FILES = {
    "name": "list_agent_files",
    "description": "Files in this persona's package. Paths only. Requires Personas.",
    "inputSchema": {
        "type": "object",
        "properties": {"persona_id": {"type": "string", "description": "Omit to read the practice persona."}},
        "additionalProperties": False,
    },
}
_WRITE_AGENT_FILE = {
    "name": "write_agent_file",
    "description": (
        "Write one text file into this persona's package. "
        "The path stays inside the package. Requires Personas."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "persona_id": {"type": "string"},
            "path": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["path", "text"],
        "additionalProperties": False,
    },
}
_INSTALL_AGENT_FILES = {
    "name": "install_agent_files",
    "description": (
        "Write many text files into this persona's package in one step. "
        "The persona file is workflow.json. Include manifest.json with entry workflow.json, "
        "that file, and any custom skill files. "
        "Custom skills are skills/{name}/SKILL.md. "
        "The built-in names are author-dev, author-compendium, and author-readback. "
        "Do not add files for those names. "
        "Requires Personas."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "persona_id": {"type": "string"},
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["path", "text"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["files"],
        "additionalProperties": False,
    },
}
_PUBLISH_PERSONA = {
    "name": "publish_persona",
    "description": "Make this persona available to authors. This desk does not charge them. Requires Personas.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "persona_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
}
_LIST_MANUSCRIPTS = {
    "name": "list_manuscripts",
    "description": "Titles on this practice. Does not return manuscript text. Requires Manuscripts.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_WORKLOAD_ID = {
    "workload_id": {"type": "string", "description": "Workload id from list_workloads."},
}
_LIST_WORKLOADS = {
    "name": "list_workloads",
    "description": "Manuscript runs on this practice. Does not return manuscript text. Requires Manuscripts.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_GET_WORKLOAD = {
    "name": "get_workload",
    "description": (
        "One workload: the form, the manuscript text, and documents already stored. "
        "Messages are included when this login has Messages. Requires Manuscripts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": _WORKLOAD_ID,
        "required": ["workload_id"],
        "additionalProperties": False,
    },
}
_RUN_WORKLOAD = {
    "name": "run_workload",
    "description": (
        "Run one queued workload on this machine. Does not call the model API. "
        "Requires Manuscripts."
    ),
    "inputSchema": {
        "type": "object",
        "properties": _WORKLOAD_ID,
        "required": ["workload_id"],
        "additionalProperties": False,
    },
}
_SEND_MESSAGE = {
    "name": "send_message",
    "description": "Write on a workload's message thread. Requires Messages.",
    "inputSchema": {
        "type": "object",
        "properties": {
            **_WORKLOAD_ID,
            "body": {"type": "string"},
        },
        "required": ["workload_id", "body"],
        "additionalProperties": False,
    },
}
_SEARCH_MANUSCRIPTS = {
    "name": "search_manuscripts",
    "description": (
        "Search this practice's manuscripts for authors to edit by hand. "
        "Omit a field to leave it open. human_requested true or false, or omit for either. "
        "Does not pause or change the automated run. Requires Pipeline."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "genre": {"type": "string"},
            "audience": {"type": "string"},
            "comps_contains": {"type": "string"},
            "word_count_min": {"type": "integer"},
            "word_count_max": {"type": "integer"},
            "human_requested": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
}
_MODEL_COST = {
    "name": "model_cost",
    "description": "Model spend for this practice. This is an operating expense. Authors do not see it. Requires Costs.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_PERMISSION_PROPERTIES = {key: {"type": "boolean"} for key, _label in PERMISSIONS}
_LIST_ASSISTANTS = {
    "name": "list_assistants",
    "description": "Pending invites and assistants on this practice. Requires Assistants.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_INVITE_ASSISTANT = {
    "name": "invite_assistant",
    "description": (
        "Invite a person onto this desk. permissions uses the desk names. "
        "Omit permissions for Manuscripts, Messages, and Pipeline. "
        "The invite path is shown once. Requires Assistants."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "email": {"type": "string"},
            "permissions": {
                "type": "object",
                "properties": _PERMISSION_PROPERTIES,
                "additionalProperties": False,
            },
        },
        "required": ["email"],
        "additionalProperties": False,
    },
}
_REVOKE_ASSISTANT_INVITE = {
    "name": "revoke_assistant_invite",
    "description": "Cancel an assistant invite that has not been accepted. Requires Assistants.",
    "inputSchema": {
        "type": "object",
        "properties": {"invite_id": {"type": "string"}},
        "required": ["invite_id"],
        "additionalProperties": False,
    },
}
_SET_ASSISTANT_PERMISSIONS = {
    "name": "set_assistant_permissions",
    "description": "Replace an assistant's permissions. Requires Assistants. You cannot change your own.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "editor_id": {"type": "string", "description": "Assistant id from list_assistants."},
            "permissions": {
                "type": "object",
                "properties": _PERMISSION_PROPERTIES,
                "additionalProperties": False,
            },
        },
        "required": ["editor_id", "permissions"],
        "additionalProperties": False,
    },
}
_REMOVE_ASSISTANT = {
    "name": "remove_assistant",
    "description": "Close an assistant login. Authors and manuscripts stay. Requires Assistants.",
    "inputSchema": {
        "type": "object",
        "properties": {"editor_id": {"type": "string"}},
        "required": ["editor_id"],
        "additionalProperties": False,
    },
}
_PATREON_INTEGRATION = {
    "name": "patreon_integration",
    "description": (
        "This practice's Patreon client: status, campaign id, client id, "
        "whether each secret is saved, the redirect URI, and the webhook URL. "
        "Does not return secrets. Practice editor only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_SET_PATREON_INTEGRATION = {
    "name": "set_patreon_integration",
    "description": (
        "Save this practice's own Patreon client. "
        "campaign_id is the numeric campaign id. "
        "client_id and client_secret come from that editor's API v2 client. "
        "webhook_secret is optional. Leave a secret empty to keep the saved one. "
        "The reply does not include secrets. Practice editor only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "campaign_id": {"type": "string"},
            "client_id": {"type": "string"},
            "client_secret": {"type": "string"},
            "webhook_secret": {"type": "string"},
        },
        "required": ["campaign_id", "client_id"],
        "additionalProperties": False,
    },
}
_PRACTICE_SETTINGS = {
    "name": "practice_settings",
    "description": (
        "This practice's name, color profile, color mode, and Patreon status. "
        "Does not return secrets. Practice editor only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_SET_PRACTICE_SETTINGS = {
    "name": "set_practice_settings",
    "description": (
        "Change this practice's name, color profile, or color mode. "
        "profile is desk, ink, forest, sea, or custom. mode is light or dark. "
        "custom uses accent, paper, and ink as hex colors. "
        "Leave a field out to keep the saved value. "
        "The reply does not include secrets. Practice editor only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "profile": {"type": "string", "enum": ["desk", "ink", "forest", "sea", "custom"]},
            "mode": {"type": "string", "enum": ["light", "dark"]},
            "accent": {"type": "string"},
            "paper": {"type": "string"},
            "ink": {"type": "string"},
        },
        "additionalProperties": False,
    },
}
_MAIL_FIELDS = {
    "transport": {"type": "string", "enum": ["smtp", "https"]},
    "from_address": {"type": "string"},
    "smtp_host": {"type": "string"},
    "smtp_port": {"type": "integer"},
    "smtp_security": {"type": "string", "enum": ["starttls", "tls"]},
    "smtp_username": {"type": "string"},
    "secret": {
        "type": "string",
        "description": "SMTP password or API key. Empty keeps the saved secret.",
    },
    "https_provider": {
        "type": "string",
        "enum": ["resend", "postmark", "sendgrid", "mailgun"],
    },
    "mailgun_domain": {"type": "string"},
    "mailgun_region": {"type": "string", "enum": ["us", "eu"]},
    "remove": {"type": "boolean"},
}
_AUTHOR_NOTICES = {
    "name": "author_notices",
    "description": (
        "This practice's mailbox for notices when a manuscript is ready or did not finish. "
        "Shows the from address and whether the secret is saved. "
        "Does not return the secret. Practice editor only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_SET_AUTHOR_NOTICES = {
    "name": "set_author_notices",
    "description": (
        "Save this practice's mailbox for author notices. "
        "transport is smtp or https. An https provider is resend, postmark, sendgrid, or mailgun. "
        "Mailgun also needs mailgun_domain and mailgun_region us or eu. "
        "SMTP needs smtp_host, smtp_port, smtp_security starttls or tls, and smtp_username. "
        "secret is the SMTP password or the API key. Leave secret empty to keep the saved one. "
        "The desk checks the account before saving and does not send mail to do it. "
        "remove true deletes the mailbox. The reply does not include the secret. Practice editor only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": dict(_MAIL_FIELDS),
        "additionalProperties": False,
    },
}
_INVITATION_MAIL = {
    "name": "invitation_mail",
    "description": (
        "The owner's mailbox for editor and admin invites. "
        "Shows the from address and whether the secret is saved. "
        "Does not return the secret. Owner only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_SET_INVITATION_MAIL = {
    "name": "set_invitation_mail",
    "description": (
        "Save the owner's mailbox for editor and admin invites. "
        "The fields match set_author_notices. "
        "The desk checks the account before saving. "
        "remove true deletes the mailbox. Saving this does not send an invite. "
        "The reply does not include the secret. Owner only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": dict(_MAIL_FIELDS),
        "additionalProperties": False,
    },
}
_INFERENCE = {
    "name": "inference",
    "description": (
        "Providers saved for this practice, the models each one lists, and the default models. "
        "prices is micros per million tokens when a provider published both sides. "
        "A missing price is not free. Does not return a key. Practice editor only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
_SET_INFERENCE = {
    "name": "set_inference",
    "description": (
        "Save a provider for this practice. "
        "provider is xai, openai, anthropic, gemini, groq, openrouter, ollama, or compatible. "
        "label names the row. Saving the same label replaces it. "
        "secret is the API key. Leave secret empty to keep the saved one. "
        "base_url is for ollama and compatible. Named providers ignore it. "
        "The desk lists models before it saves and does not store a failed check. "
        "remove true deletes that label. The reply does not include the key. Practice editor only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "provider": {
                "type": "string",
                "enum": ["xai", "openai", "anthropic", "gemini", "groq", "openrouter", "ollama", "compatible"],
            },
            "base_url": {"type": "string"},
            "secret": {"type": "string", "description": "API key. Empty keeps the saved key."},
            "remove": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
}
_SET_INFERENCE_DEFAULTS = {
    "name": "set_inference_defaults",
    "description": (
        "Save the default models for this practice, in order. "
        "A workflow's named model is used when any saved provider lists it. "
        "These models are used when the file names none, or when none list the named one. "
        "The cheapest published price is tried first. "
        "A provider that did not list a price is used after the ones that did. "
        "models is a list of model ids. An empty list clears the defaults. "
        "The reply does not include a key. Practice editor only."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "models": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "additionalProperties": False,
    },
}
_DEPLOYMENT = {
    "name": "deployment",
    "description": (
        "What this process still needs before other people use the desk. "
        "Reports the flags, the HTTPS choice, and whether a model key is set. "
        "Does not return the key or a secret. The checklist is README Deploy. "
        "Practice editor only."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _call_tool(conn, user, params: dict, mcp_url: str) -> dict:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(arguments, dict):
        raise ValueError("invalid tool call")
    if name == "whoami":
        payload = _whoami(conn, user, mcp_url)
    elif name == "list_practices":
        payload = _list_practices(conn, user)
    elif name == "list_personas":
        payload = _list_personas(conn, user)
    elif name == "get_persona":
        payload = _persona_detail(conn, user, arguments.get("persona_id"))
    elif name == "publish_persona":
        payload = _publish_persona(conn, user, arguments)
    elif name == "list_agent_files":
        payload = _agent_files(conn, user, arguments.get("persona_id"))
    elif name == "write_agent_file":
        payload = _write_agent_file(conn, user, arguments)
    elif name == "install_agent_files":
        payload = _install_agent_files(conn, user, arguments)
    elif name == "list_manuscripts":
        payload = _list_manuscripts(conn, user)
    elif name == "list_workloads":
        payload = _list_workloads(conn, user)
    elif name == "get_workload":
        payload = _get_workload(conn, user, arguments.get("workload_id"))
    elif name == "run_workload":
        payload = _run_workload(conn, user, arguments.get("workload_id"))
    elif name == "send_message":
        payload = _send_message(conn, user, arguments)
    elif name == "search_manuscripts":
        payload = _search_manuscripts(conn, user, arguments)
    elif name == "model_cost":
        payload = _model_cost(conn, user)
    elif name == "list_assistants":
        payload = _list_assistants(conn, user)
    elif name == "invite_assistant":
        payload = _invite_assistant(conn, user, arguments)
    elif name == "revoke_assistant_invite":
        payload = _revoke_assistant_invite(conn, user, arguments.get("invite_id"))
    elif name == "set_assistant_permissions":
        payload = _set_assistant_permissions(conn, user, arguments)
    elif name == "remove_assistant":
        payload = _remove_assistant(conn, user, arguments.get("editor_id"))
    elif name == "patreon_integration":
        payload = _patreon_integration(conn, user, mcp_url)
    elif name == "set_patreon_integration":
        payload = _set_patreon_integration(conn, user, arguments, mcp_url)
    elif name == "practice_settings":
        payload = _practice_settings(conn, user, mcp_url)
    elif name == "set_practice_settings":
        payload = _set_practice_settings(conn, user, arguments, mcp_url)
    elif name == "deployment":
        payload = _deployment(conn, user)
    elif name == "author_notices":
        payload = _author_notices(conn, user)
    elif name == "set_author_notices":
        payload = _set_author_notices(conn, user, arguments, mcp_url)
    elif name == "inference":
        payload = _inference(conn, user)
    elif name == "set_inference":
        payload = _set_inference(conn, user, arguments)
    elif name == "set_inference_defaults":
        payload = _set_inference_defaults(conn, user, arguments)
    elif name == "invitation_mail":
        payload = _invitation_mail(conn, user)
    elif name == "set_invitation_mail":
        payload = _set_invitation_mail(conn, user, arguments)
    else:
        raise ValueError(f"unknown tool {name}")
    return _tool_text(json.dumps(payload, indent=2))


def _whoami(conn, user, mcp_url: str) -> dict:
    editor = editor_for_user(conn, user["id"])
    practice = None
    permissions = {key: False for key, _label in PERMISSIONS}
    if editor is not None and editor["status"] != "removed":
        practice = {"name": editor["org_name"], "status": editor["status"]}
        if editor["status"] == "active":
            permissions = {key: bool(allowed(editor, key)) for key, _label in PERMISSIONS}
    return {
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "roles": account_roles(conn, user),
        "mcp_url": mcp_url,
        "practice": practice,
        "permissions": permissions,
        "platform_skills": list(PLATFORM_SKILLS),
    }


def _list_practices(conn, user) -> list[dict]:
    if user["role"] != "owner":
        raise ValueError("only the platform owner can list practices")
    rows = conn.execute(
        """
        SELECT o.name, u.email, e.status, e.id AS editor_id
        FROM organizations o
        JOIN editors e ON e.organization_id = o.id AND e.is_org_admin = 1
        JOIN users u ON u.id = e.user_id
        ORDER BY o.created_at DESC
        """
    ).fetchall()
    return [
        {"name": row["name"], "editor_email": row["email"], "status": row["status"], "editor_id": row["editor_id"]}
        for row in rows
    ]


def _list_personas(conn, user) -> list[dict]:
    desk = _offers_desk(conn, user)
    rows = conn.execute(
        "SELECT * FROM personas WHERE editor_id = ? ORDER BY created_at",
        (desk["id"],),
    ).fetchall()
    return [_summary(row) for row in rows]


def _persona_detail(conn, user, persona_id) -> dict:
    desk = _offers_desk(conn, user)
    persona = _persona(conn, desk, persona_id)
    detail = _summary(persona)
    detail["skills"] = parse_skills(persona["agent_skills"])
    detail["platform_skills"] = list(PLATFORM_SKILLS)
    info = _package_info(persona)
    detail["harness"] = info["harness"]
    detail["entry"] = info["entry"]
    detail["files"] = info["files"]
    return detail


def _agent_files(conn, user, persona_id) -> dict:
    desk = _offers_desk(conn, user)
    persona = _persona(conn, desk, persona_id)
    info = _package_info(persona)
    info["persona_id"] = persona["id"]
    return info


def _write_agent_file(conn, user, arguments: dict) -> dict:
    desk = _offers_desk(conn, user)
    persona = _persona(conn, desk, arguments.get("persona_id"))
    path = arguments.get("path")
    text = arguments.get("text")
    if not isinstance(path, str) or not isinstance(text, str):
        raise ValueError("a package file needs a path and text")
    install_persona_files(conn, desk, persona["id"], {path: text})
    return _agent_files(conn, user, persona["id"])


def _install_agent_files(conn, user, arguments: dict) -> dict:
    desk = _offers_desk(conn, user)
    persona = _persona(conn, desk, arguments.get("persona_id"))
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("a package needs files")
    mapping = {}
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("text"), str):
            raise ValueError("each file needs a path and text")
        mapping[item["path"]] = item["text"]
    install_persona_files(conn, desk, persona["id"], mapping)
    return _agent_files(conn, user, persona["id"])


def _package_info(persona) -> dict:
    root = ensure_persona_package(persona)
    return inspect_root(root)


def _publish_persona(conn, user, arguments: dict) -> dict:
    desk = _offers_desk(conn, user)
    persona = _persona(conn, desk, arguments.get("persona_id"))
    publish_persona(conn, desk, persona["id"])
    saved = conn.execute("SELECT * FROM personas WHERE id = ?", (persona["id"],)).fetchone()
    return _summary(saved)


def _list_manuscripts(conn, user) -> list[dict]:
    editor = _active_editor(conn, user)
    if not allowed(editor, "manuscripts"):
        raise ValueError("that is not assigned to this login")
    rows = jobs_visible(conn, editor)[:50]
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "model_cost": _shown_model_cost(row["cost_usd_micros"]),
            "author_email": row["author_email"],
            "word_count": row["word_count"],
        }
        for row in rows
    ]


def _list_workloads(conn, user) -> list[dict]:
    editor = _perm(conn, user, "manuscripts")
    return [_workload_summary(row) for row in jobs_visible(conn, editor)[:50]]


def _get_workload(conn, user, workload_id) -> dict:
    editor = _perm(conn, user, "manuscripts")
    job = _job_in_practice(conn, editor, workload_id)
    detail = _workload_summary(job)
    detail["comps"] = job["comps"] or ""
    detail["do_not_change"] = job["do_not_change"] or ""
    detail["focus"] = job["focus"] or ""
    detail["manuscript"] = read_object(job["manuscript_object_key"])
    detail["documents"] = _documents(conn, job["id"])
    if allowed(editor, "messages"):
        detail["messages"] = _thread(conn, job["id"])
    return detail


def _run_workload(conn, user, workload_id) -> dict:
    editor = _perm(conn, user, "manuscripts")
    job = _job_in_practice(conn, editor, workload_id)
    if job["status"] != "queued":
        raise ValueError("that workload is not queued")
    _run_local(job["id"])
    saved = _job_in_practice(conn, editor, job["id"])
    return {
        "id": saved["id"],
        "status": saved["status"],
        "error_code": saved["error_code"],
        "local": True,
        "documents": _documents(conn, saved["id"]),
    }


def _send_message(conn, user, arguments: dict) -> dict:
    editor = _perm(conn, user, "messages")
    job = _job_in_practice(conn, editor, arguments.get("workload_id"))
    body = arguments.get("body")
    if not isinstance(body, str):
        raise ValueError("empty message")
    post_message(
        conn,
        job_id=job["id"],
        sender_role="editor",
        sender_id=editor["id"],
        body=body,
        actor_editor=editor,
    )
    return {"workload_id": job["id"], "messages": _thread(conn, job["id"])}


def _search_manuscripts(conn, user, arguments: dict) -> list[dict]:
    editor = _perm(conn, user, "pipeline")
    human = arguments.get("human_requested")
    if human is None:
        human_requested = None
    elif isinstance(human, bool):
        human_requested = 1 if human else 0
    else:
        raise ValueError("human request filter is invalid")
    rows = search_manuscripts(
        conn,
        editor,
        q=_text(arguments.get("q")),
        genre=_text(arguments.get("genre")),
        audience=_text(arguments.get("audience")),
        comps_contains=_text(arguments.get("comps_contains")),
        word_count_min=_optional_count(arguments.get("word_count_min"), "minimum words"),
        word_count_max=_optional_count(arguments.get("word_count_max"), "maximum words"),
        human_requested=human_requested,
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "author_email": row["author_email"],
            "genre": row["genre"] or "",
            "audience": row["audience"] or "",
            "comps": row["comps"] or "",
            "word_count": row["word_count"] or 0,
            "human_requested": bool(row["human_requested"]),
            "status": row["status"],
        }
        for row in rows
    ]


def _model_cost(conn, user) -> dict:
    editor = _perm(conn, user, "payouts")
    micros = practice_model_cost_micros(conn, editor["organization_id"])
    return {"model_cost": _shown_model_cost(micros)}


def _list_assistants(conn, user) -> dict:
    editor = _perm(conn, user, "assistants")
    invites = [{"id": row["id"], "email": row["email"]} for row in pending_editor_invites(conn, editor)]
    assistants = []
    for row in practice_editors(conn, editor):
        if row["is_org_admin"]:
            continue
        assistants.append(
            {
                "id": row["id"],
                "email": row["email"],
                "permissions": {key: bool(row[f"perm_{key}"]) for key, _label in PERMISSIONS},
            }
        )
    return {"invites": invites, "assistants": assistants}


def _invite_assistant(conn, user, arguments: dict) -> dict:
    editor = _perm(conn, user, "assistants")
    email = arguments.get("email")
    if not isinstance(email, str) or not email.strip():
        raise ValueError("an email is required")
    permissions = None
    if "permissions" in arguments:
        permissions = _permission_args(arguments.get("permissions"))
    raw = invite_editor(conn, user["id"], email, editor["organization_id"], permissions)
    return {"email": email.strip().lower(), "invite_path": f"/invite/{raw}"}


def _revoke_assistant_invite(conn, user, invite_id) -> dict:
    editor = _perm(conn, user, "assistants")
    if not isinstance(invite_id, str):
        raise ValueError("invite is not valid")
    revoke_editor_invite(conn, editor, invite_id)
    return _list_assistants(conn, user)


def _set_assistant_permissions(conn, user, arguments: dict) -> dict:
    editor = _perm(conn, user, "assistants")
    editor_id = arguments.get("editor_id")
    if not isinstance(editor_id, str):
        raise ValueError("that assistant cannot be changed")
    set_assistant_permissions(conn, editor, editor_id, _permission_args(arguments.get("permissions")))
    return _list_assistants(conn, user)


def _remove_assistant(conn, user, editor_id) -> dict:
    editor = _perm(conn, user, "assistants")
    if not isinstance(editor_id, str):
        raise ValueError("that editor cannot be removed")
    remove_editor(conn, editor, editor_id)
    return _list_assistants(conn, user)


def _patreon_integration(conn, user, mcp_url: str) -> dict:
    editor = _practice_editor(conn, user)
    return _patreon_status(editor, mcp_url)


def _set_patreon_integration(conn, user, arguments: dict, mcp_url: str) -> dict:
    editor = _practice_editor(conn, user)
    set_patreon_integration(
        conn,
        editor,
        campaign_id=_patreon_arg(arguments, "campaign_id"),
        client_id=_patreon_arg(arguments, "client_id"),
        client_secret=_patreon_arg(arguments, "client_secret"),
        webhook_secret=_patreon_arg(arguments, "webhook_secret"),
    )
    return _patreon_status(editor_for_user(conn, user["id"]), mcp_url)


def _practice_editor(conn, user):
    editor = _active_editor(conn, user)
    if not editor["is_org_admin"]:
        raise ValueError("only the editor can connect Patreon")
    return editor


def _patreon_arg(arguments: dict, key: str) -> str:
    if key not in arguments or arguments[key] is None:
        return ""
    value = arguments[key]
    if not isinstance(value, str):
        label = {
            "campaign_id": "campaign id",
            "client_id": "client id",
            "client_secret": "client secret",
            "webhook_secret": "webhook secret",
        }[key]
        raise ValueError(f"a Patreon {label} is required")
    return value


def _practice_settings(conn, user, mcp_url: str) -> dict:
    editor = _settings_editor(conn, user)
    return _settings_payload(conn, editor, mcp_url)


def _set_practice_settings(conn, user, arguments: dict, mcp_url: str) -> dict:
    editor = _settings_editor(conn, user)
    set_practice_settings(
        conn,
        editor,
        name=_optional_setting(arguments, "name"),
        profile=_optional_setting(arguments, "profile"),
        mode=_optional_setting(arguments, "mode"),
        accent=_optional_setting(arguments, "accent"),
        paper=_optional_setting(arguments, "paper"),
        ink=_optional_setting(arguments, "ink"),
    )
    return _settings_payload(conn, editor_for_user(conn, user["id"]), mcp_url)


def _author_notices(conn, user) -> dict:
    editor = _settings_editor(conn, user)
    return mail_public(author_notices_account(conn, editor["organization_id"]))


def _inference(conn, user) -> dict:
    editor = _settings_editor(conn, user)
    return _inference_payload(conn, editor)


def _inference_payload(conn, editor) -> dict:
    org = editor["organization_id"]
    return {
        "providers": inference_public(inference_rows(conn, org)),
        "defaults": inference_defaults(conn, org),
    }


def _set_inference(conn, user, arguments: dict) -> dict:
    editor = _settings_editor(conn, user)
    if arguments.get("remove") is True:
        label = arguments.get("label")
        if not isinstance(label, str):
            raise ValueError("the provider label is not valid")
        clear_inference(conn, editor, label)
    else:
        label = arguments.get("label")
        provider = arguments.get("provider")
        base_url = arguments.get("base_url") or ""
        secret = arguments.get("secret") or ""
        if not isinstance(label, str) or not isinstance(provider, str):
            raise ValueError("a provider is required")
        if not isinstance(base_url, str) or not isinstance(secret, str):
            raise ValueError("a provider is required")
        set_inference(
            conn,
            editor,
            label=label,
            provider=provider,
            base_url=base_url,
            secret=secret,
        )
    fresh = _settings_editor(conn, user)
    return _inference_payload(conn, fresh)


def _set_inference_defaults(conn, user, arguments: dict) -> dict:
    editor = _settings_editor(conn, user)
    models = arguments.get("models")
    if not isinstance(models, list):
        raise ValueError("a default model is not valid")
    set_inference_defaults(conn, editor, models)
    return _inference_payload(conn, _settings_editor(conn, user))


def _set_author_notices(conn, user, arguments: dict, mcp_url: str) -> dict:
    editor = _settings_editor(conn, user)
    if arguments.get("remove") is True:
        clear_author_notices(conn, editor)
    else:
        _reject_bad_remove(arguments)
        set_author_notices(
            conn,
            editor,
            notice_origin=_notice_origin(mcp_url),
            **_mail_arguments(arguments),
        )
    fresh = editor_for_user(conn, user["id"])
    return mail_public(author_notices_account(conn, fresh["organization_id"]))


def _invitation_mail(conn, user) -> dict:
    if not user_is_owner(user):
        raise ValueError("only the owner can change invitation mail")
    return mail_public(invitation_mail_account(conn, user["id"]))


def _set_invitation_mail(conn, user, arguments: dict) -> dict:
    if arguments.get("remove") is True:
        clear_invitation_mail(conn, user)
    else:
        _reject_bad_remove(arguments)
        set_invitation_mail(conn, user, **_mail_arguments(arguments))
    return mail_public(invitation_mail_account(conn, user["id"]))


def _notice_origin(mcp_url: str) -> str:
    parts = urlsplit(mcp_url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _reject_bad_remove(arguments: dict) -> None:
    if "remove" in arguments and arguments["remove"] is not False:
        raise ValueError("a mail transport is required")


def _mail_arguments(arguments: dict) -> dict:
    fields = {}
    for key in (
        "transport",
        "from_address",
        "smtp_host",
        "smtp_security",
        "smtp_username",
        "secret",
        "https_provider",
        "mailgun_domain",
        "mailgun_region",
    ):
        if key not in arguments or arguments[key] is None:
            fields[key] = ""
            continue
        value = arguments[key]
        if not isinstance(value, str):
            if key == "secret" and fields.get("transport") == "smtp":
                raise ValueError("an SMTP password is required")
            raise ValueError(_mail_type_message(key))
        fields[key] = value
    if "smtp_port" not in arguments or arguments["smtp_port"] is None or arguments["smtp_port"] == "":
        fields["smtp_port"] = ""
    else:
        port = arguments["smtp_port"]
        if isinstance(port, bool) or not isinstance(port, (int, str)):
            raise ValueError("an SMTP port is required")
        fields["smtp_port"] = port
    return fields


def _mail_type_message(key: str) -> str:
    return {
        "transport": "a mail transport is required",
        "from_address": "a from address is required",
        "smtp_host": "an SMTP host is required",
        "smtp_security": "SMTP security must be STARTTLS or TLS",
        "smtp_username": "an SMTP username is required",
        "secret": "a mail API key is required",
        "https_provider": "a mail provider is required",
        "mailgun_domain": "a Mailgun domain is required",
        "mailgun_region": "a Mailgun region is required",
    }[key]


def _deployment(conn, user) -> dict:
    editor = _settings_editor(conn, user)
    https = _https_mode()
    host = config.HOST
    llm_spec = llm.settings()
    providers = inference_rows(conn, editor["organization_id"])
    return {
        "guide": "README.md Deploy",
        "not_this_build": [
            "local-docs/",
        ],
        "requirements": {
            "python": "3.15",
            "packages": "stdlib only",
            "openssl": "required for Let's Encrypt",
            "pdftotext": "optional, for PDF text",
            "soffice": "optional, for legacy .doc",
        },
        "processes": {
            "web": "python -m app web",
            "work": "python -m app work",
            "work_runs": "one queued manuscript, then exits",
            "bootstrap": "python -m app bootstrap",
        },
        "this_process": {
            "dev_login": config.DEV_LOGIN,
            "demo_mode": config.DEMO_MODE,
            "host": host,
            "port": config.PORT,
            "https": https,
            "https_note": _https_note(https),
            "trust_proxy": config.TRUST_PROXY,
            "tls_files_set": bool((config.TLS_CERT or "").strip() and (config.TLS_KEY or "").strip()),
            "acme_domain": (config.ACME_DOMAIN or "").strip(),
            "acme_email_set": bool((config.ACME_EMAIL or "").strip()),
            "acme_staging": config.ACME_STAGING,
            "acme_http_port": config.ACME_HTTP_PORT,
            "patreon_redirect_uri": config.PATREON_REDIRECT_URI,
            "xai_mode": config.XAI_MODE,
            "xai_base_url": config.XAI_BASE_URL,
            "xai_model": config.XAI_MODEL,
            "xai_key_set": bool(config.XAI_API_KEY),
            "llm_provider": llm_spec["provider"],
            "llm_mode": llm_spec["mode"],
            "llm_model": llm_spec["model"],
            "llm_base_url": llm_spec["base"],
            "llm_key_set": llm_spec["key_set"],
            "openssl": shutil.which("openssl") is not None,
            "pdftotext": shutil.which("pdftotext") is not None,
            "soffice": shutil.which("soffice") is not None,
            "data_dir": str(config.DATA_DIR),
            "database_path": str(config.DATABASE_PATH),
            "daily_stop_micros": config.DAILY_STOP_MICROS,
        },
        "inference": inference_public(providers),
        "still_needed": _still_needed(
            https, host, patreon_ready(editor), llm_spec, providers_saved=bool(providers)
        ),
    }


def _https_mode() -> str:
    if (config.TLS_CERT or "").strip() and (config.TLS_KEY or "").strip():
        return "tls_files"
    if (config.ACME_DOMAIN or "").strip():
        return "acme"
    if config.TRUST_PROXY:
        return "proxy"
    return "plain"


def _https_note(https: str) -> str:
    if https == "tls_files" and (config.ACME_DOMAIN or "").strip():
        return "TLS_CERT and TLS_KEY are set, so ACME is not called."
    if https == "acme":
        return (
            "Port 80 on ACME_DOMAIN must reach this process, unless ACME_HTTP_PORT is 0 "
            "and a proxy forwards /.well-known/acme-challenge/."
        )
    if https == "proxy":
        return (
            "The proxy replaces Forwarded or X-Forwarded-Proto and the public host. "
            "It does not append a client value."
        )
    return ""


def _still_needed(
    https: str,
    host: str,
    connected: bool,
    llm_spec: dict | None = None,
    providers_saved: bool = False,
) -> list[str]:
    gaps = []
    llm_spec = llm_spec or llm.settings()
    if config.DEV_LOGIN:
        gaps.append(
            "DEV_LOGIN is on. It skips the Patreon check. "
            "Leave it unset before a real author uses the desk."
        )
    if config.DEMO_MODE:
        gaps.append(
            "DEMO_MODE is on. A submitted manuscript is stored and the workflow does not run. "
            "Leave it unset when letters should be written."
        )
    if https == "plain":
        gaps.append(
            "This process is plain HTTP. Choose TLS_CERT and TLS_KEY, or ACME_DOMAIN and ACME_EMAIL, "
            "or a proxy that terminates TLS. A proxy on this machine is trusted. "
            "A remote proxy needs TRUST_PROXY=1."
        )
    if host in ("127.0.0.1", "localhost", "::1") and https != "proxy":
        gaps.append(f"HOST is {host}. Other machines cannot open this listener.")
    if https == "acme" and not (config.ACME_EMAIL or "").strip():
        gaps.append("ACME_EMAIL is required with ACME_DOMAIN.")
    if https == "acme" and shutil.which("openssl") is None:
        gaps.append("openssl is not on PATH. Let's Encrypt needs it.")
    if not providers_saved and not config.DEMO_MODE and not llm.ready(llm_spec):
        if llm_spec["provider"] == "xai":
            gaps.append(
                "Letters are not calling the model. Set XAI_MODE=sync and XAI_API_KEY. "
                "Confirm that key does not train on manuscripts before a customer manuscript. "
                "Keep XAI_BASE_URL at https://api.x.ai/v1 and XAI_MODEL at grok-4.3."
            )
        elif not llm_spec["known"]:
            gaps.append(
                "LLM_PROVIDER must be xai, openai, anthropic, gemini, groq, openrouter, ollama, or compatible."
            )
        else:
            gaps.append(llm.gap(llm_spec))
    if not providers_saved and llm_spec["provider"] == "xai" and "us.api.x.ai" in (llm_spec["base"] or ""):
        gaps.append("XAI_BASE_URL points at the US regional host. Use https://api.x.ai/v1.")
    if not providers_saved and llm_spec["provider"] == "xai" and llm_spec["model"] != "grok-4.3":
        gaps.append("XAI_MODEL is not grok-4.3.")
    if not connected:
        gaps.append(
            "This practice has not connected Patreon. After the public URL is HTTPS, "
            "patreon_integration shows the redirect URI and the webhook URL."
        )
    return gaps


def _settings_editor(conn, user):
    editor = _active_editor(conn, user)
    if not editor["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    return editor


def _settings_payload(conn, editor, mcp_url: str) -> dict:
    return {
        "name": editor["org_name"],
        "appearance": practice_appearance(conn, editor["organization_id"]),
        "patreon": _patreon_status(editor, mcp_url),
    }


_SETTING_LABELS = {
    "name": "a name is required",
    "profile": "a theme is required",
    "mode": "a color mode is required",
    "accent": "a accent color is required",
    "paper": "a background color is required",
    "ink": "a text color is required",
}


def _optional_setting(arguments: dict, key: str):
    if key not in arguments or arguments[key] is None:
        return None
    value = arguments[key]
    if not isinstance(value, str):
        raise ValueError(_SETTING_LABELS[key])
    return value


def _patreon_status(row, mcp_url: str) -> dict:
    keys = row.keys()
    campaign = row["patreon_campaign_id"] if "patreon_campaign_id" in keys else ""
    client_id = row["patreon_client_id"] if "patreon_client_id" in keys else ""
    has_secret = bool(row["patreon_client_secret"]) if "patreon_client_secret" in keys else False
    has_hook = bool(row["patreon_webhook_secret"]) if "patreon_webhook_secret" in keys else False
    parts = urlsplit(mcp_url)
    host = parts.netloc
    scheme = parts.scheme or "http"
    return {
        "status": "Connected" if campaign and client_id and has_secret else "Not connected",
        "campaign_id": campaign,
        "client_id": client_id,
        "client_secret": "saved" if has_secret else "not saved",
        "webhook_secret": "saved" if has_hook else "not saved",
        "redirect_uri": patreon.redirect_uri(host, scheme),
        "webhook_url": patreon.webhook_url(host, scheme),
    }


def _perm(conn, user, perm: str):
    editor = _active_editor(conn, user)
    if not allowed(editor, perm):
        raise ValueError("that is not assigned to this login")
    return editor


def _job_in_practice(conn, editor, workload_id):
    if not isinstance(workload_id, str) or not workload_id:
        raise ValueError("workload not found")
    row = conn.execute(
        """
        SELECT j.*, u.email AS author_email
        FROM jobs j
        JOIN authors a ON a.id = j.author_id
        JOIN users u ON u.id = a.user_id
        WHERE j.id = ? AND j.organization_id = ?
        """,
        (workload_id, editor["organization_id"]),
    ).fetchone()
    if row is None:
        raise ValueError("workload not found")
    return row


def _workload_summary(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "model_cost": _shown_model_cost(row["cost_usd_micros"]),
        "author_email": row["author_email"],
        "word_count": row["word_count"],
        "genre": row["genre"],
        "audience": row["audience"],
        "human_requested": bool(row["human_requested"]),
        "error_code": row["error_code"],
    }


def _documents(conn, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, object_key FROM artifacts WHERE job_id = ? ORDER BY created_at",
        (job_id,),
    ).fetchall()
    return [{"kind": row["kind"], "text": read_object(row["object_key"])} for row in rows]


def _thread(conn, job_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT sender_role, body FROM messages WHERE job_id = ? ORDER BY created_at",
        (job_id,),
    ).fetchall()
    return [{"sender": row["sender_role"], "body": row["body"]} for row in rows]


def _shown_model_cost(micros) -> str:
    return usd(micros_to_cents(int(micros or 0)))


def _permission_args(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("permissions must list the desk permissions")
    flags = {}
    for key, _label in PERMISSIONS:
        item = value.get(key, False)
        if not isinstance(item, bool):
            raise ValueError("permissions must be yes or no")
        flags[key] = item
    return flags


def _text(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("a search field must be text")
    return value


def _optional_count(value, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{label} must be a number")
    if isinstance(value, float):
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"{label} must be a number")
    return value


def _run_local(job_id: str) -> None:
    """The agent runs on a worker thread, not the MCP request thread."""
    box: dict = {}

    def target():
        try:
            run_job(job_id)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=target, name="workload")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]


def _offers_desk(conn, user):
    editor = _active_editor(conn, user)
    if not allowed(editor, "offers"):
        raise ValueError("that is not assigned to this login")
    return practice_admin(conn, editor)


def _active_editor(conn, user):
    if user["role"] not in ("owner", "editor"):
        raise ValueError("authors do not connect a local agent")
    editor = editor_for_user(conn, user["id"])
    if editor is None:
        raise ValueError("this login has no practice")
    if editor["status"] == "removed":
        raise ValueError("you are no longer an editor on this practice")
    if editor["status"] != "active":
        raise ValueError("this practice is suspended")
    return editor


def _persona(conn, desk, persona_id):
    if persona_id:
        if not isinstance(persona_id, str):
            raise ValueError("persona not found")
        row = conn.execute(
            "SELECT * FROM personas WHERE id = ? AND editor_id = ?",
            (persona_id, desk["id"]),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM personas WHERE editor_id = ? ORDER BY created_at LIMIT 1",
            (desk["id"],),
        ).fetchone()
    if row is None:
        raise ValueError("persona not found")
    return row


def _summary(persona) -> dict:
    skills = parse_skills(persona["agent_skills"])
    return {
        "id": persona["id"],
        "name": persona["name"],
        "status": persona["status"],
        "max_words": persona["max_words"],
        "skills": [item["name"] for item in skills],
    }


def _tool_text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
