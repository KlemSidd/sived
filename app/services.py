"""One editor practice.

The editor who accepts the first invite owns the practice and its public page.
That editor can give other people logins to the same desk and choose what each
assistant can do. Removing an assistant leaves the authors and manuscripts
where they are. Author access is an active Patreon membership of that editor's campaign.
This desk asks Patreon. It does not charge an author.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from app import config
from app import llm
from app import mail
from app import patreon
from app.agent import (
    DEFAULT_AGENT,
    estimate_agent_words,
    static_calls,
    validate_agent,
)
from app.db import utcnow
from app.estimate import PASSES
from app.harness import compile_package, parse_workflow, starter_files
from app.package import (
    adopt_tree,
    discard_backup,
    ensure_persona_package,
    files_from_zip,
    persona_dir,
    prepare_tree,
    read_tree,
    replace_tree,
    restore_backup,
    safe_rel,
    snapshot_persona,
)

INVITE_DAYS = 14

PERMISSIONS = (
    ("manuscripts", "Manuscripts"),
    ("messages", "Messages"),
    ("pipeline", "Pipeline"),
    ("offers", "Personas"),
    ("payouts", "Costs"),
    ("assistants", "Assistants"),
)


def allowed(editor_row, perm: str) -> bool:
    if editor_row is None or editor_row["status"] != "active":
        return False
    if editor_row["is_org_admin"]:
        return True
    return bool(editor_row[f"perm_{perm}"])


def permission_flags(source) -> dict:
    """Read permission checkboxes from a form dict, or columns from a row.

    A checkbox is on only when the value is the integer 1 or the string "1".
    """
    flags = {}
    present = source.keys() if hasattr(source, "keys") else ()
    for key, _label in PERMISSIONS:
        column = f"perm_{key}"
        if column in present:
            value = source[column]
            flags[key] = value == 1 or value is True or str(value) == "1"
        else:
            flags[key] = str(source.get(column, "")) == "1"
    return flags


def new_id() -> str:
    return str(uuid.uuid4())


def token() -> str:
    return secrets.token_urlsafe(24)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_agent_token(conn, user_id: str, label: str) -> str:
    """Mint a bearer token for this login's local agent. The raw value is returned once."""
    user = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or user["role"] not in ("owner", "editor"):
        raise ValueError("authors do not connect a local agent")
    label = (label or "").strip()
    if len(label) > 80:
        raise ValueError("the token label is too long")
    raw = token()
    conn.execute(
        """
        INSERT INTO agent_tokens (id, user_id, token_hash, label, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (new_id(), user_id, token_hash(raw), label, utcnow()),
    )
    conn.commit()
    return raw


def revoke_agent_token(conn, user_id: str, token_id: str) -> None:
    row = conn.execute(
        "SELECT id FROM agent_tokens WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (token_id, user_id),
    ).fetchone()
    if row is None:
        raise ValueError("token not found")
    conn.execute("UPDATE agent_tokens SET revoked_at = ? WHERE id = ?", (utcnow(), token_id))
    conn.commit()


def user_for_agent_token(conn, raw: str):
    if not (raw or "").strip():
        return None
    return conn.execute(
        """
        SELECT u.* FROM agent_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.token_hash = ? AND t.revoked_at IS NULL AND u.deleted_at IS NULL
        """,
        (token_hash(raw.strip()),),
    ).fetchone()


def list_agent_tokens(conn, user_id: str):
    return conn.execute(
        """
        SELECT id, label, created_at FROM agent_tokens
        WHERE user_id = ? AND revoked_at IS NULL
        ORDER BY created_at DESC
        """,
        (user_id,),
    ).fetchall()


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "manuscript"


def word_count(text: str) -> int:
    return len(text.split())


def _editor(conn, user_id: str):
    return conn.execute(
        """
        SELECT e.*, o.plan, o.name AS org_name, o.payouts_paused, o.payouts_enabled
        FROM editors e
        JOIN organizations o ON o.id = e.organization_id
        WHERE e.user_id = ?
        """,
        (user_id,),
    ).fetchone()


def editor_for_user(conn, user_id: str):
    return _editor(conn, user_id)


def practice_admin(conn, editor_row):
    """The indie editor who owns the practice page, persona, and manuscripts."""
    if editor_row["is_org_admin"]:
        return editor_row
    row = conn.execute(
        """
        SELECT e.*, o.plan, o.name AS org_name, o.payouts_paused, o.payouts_enabled
        FROM editors e
        JOIN organizations o ON o.id = e.organization_id
        WHERE e.organization_id = ? AND e.is_org_admin = 1 AND e.status = 'active'
        """,
        (editor_row["organization_id"],),
    ).fetchone()
    if row is None:
        raise ValueError("this practice has no editor")
    return row


def author_for_user(conn, user_id: str):
    return conn.execute("SELECT * FROM authors WHERE user_id = ?", (user_id,)).fetchone()


def can_see_editor(actor_editor, target_editor_id: str) -> bool:
    if actor_editor is None or actor_editor["status"] != "active":
        return False
    if actor_editor["id"] == target_editor_id:
        return True
    return bool(actor_editor["is_org_admin"])


def editor_in_scope(conn, actor_editor, editor_id: str):
    if not can_see_editor(actor_editor, editor_id):
        return None
    row = conn.execute("SELECT * FROM editors WHERE id = ?", (editor_id,)).fetchone()
    if row is None or row["organization_id"] != actor_editor["organization_id"]:
        return None
    if not actor_editor["is_org_admin"] and row["id"] != actor_editor["id"]:
        return None
    return row


def user_is_owner(user) -> bool:
    if user is None:
        return False
    keys = user.keys()
    flagged = user["is_owner"] if "is_owner" in keys else 0
    return bool(flagged) or user["role"] == "owner"


def user_is_admin(user) -> bool:
    if user is None:
        return False
    keys = user.keys()
    if "is_admin" in keys:
        return bool(user["is_admin"])
    return user["role"] == "admin"


def user_is_system_admin(user) -> bool:
    """Owner or admin. Both use System Admin. An admin cannot change the owner."""
    return user_is_owner(user) or user_is_admin(user)


def account_roles(conn, user) -> list[str]:
    """The roles this login holds: Owner, Admin, Editor, Assistant, Author."""
    roles = []
    if user_is_owner(user):
        roles.append("Owner")
    if user_is_admin(user):
        roles.append("Admin")
    editor = editor_for_user(conn, user["id"])
    if editor is not None and editor["status"] != "removed":
        roles.append("Editor" if editor["is_org_admin"] else "Assistant")
    author = author_for_user(conn, user["id"])
    if author is not None and author["status"] == "active":
        roles.append("Author")
    return roles


def create_owner(conn, email: str, name: str = "Platform owner") -> str:
    """The first account is the owner and an editor. Nothing clears that owner flag."""
    existing = conn.execute(
        "SELECT id FROM users WHERE deleted_at IS NULL AND (role = 'owner' OR is_owner = 1)"
    ).fetchone()
    if existing:
        user_id = existing["id"]
        conn.execute("UPDATE users SET is_owner = 1 WHERE id = ?", (user_id,))
        conn.commit()
    else:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (
              id, email, name, role, is_owner, auth_provider, auth_subject, created_at
            ) VALUES (?, ?, ?, 'owner', 1, 'dev', ?, ?)
            """,
            (user_id, email.lower(), name, f"dev:{email.lower()}", utcnow()),
        )
        conn.commit()
    _ensure_owner_editor(conn, user_id)
    return user_id


def _ensure_owner_editor(conn, user_id: str) -> str:
    existing = editor_for_user(conn, user_id)
    if existing is not None and existing["status"] != "removed":
        return existing["id"]
    if existing is not None:
        raise ValueError("this login's practice was removed")
    user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    practice = (user["name"] or "").strip() if user else ""
    if not practice or practice == "Platform owner":
        practice = "Owner"
    return _create_practice(conn, user_id, practice)


def _create_practice(conn, user_id: str, practice_name: str) -> str:
    org_id = new_id()
    editor_id = new_id()
    conn.execute(
        """
        INSERT INTO organizations (id, name, plan, created_at)
        VALUES (?, ?, 'indie', ?)
        """,
        (org_id, practice_name, utcnow()),
    )
    perm_values = [1 for _key, _label in PERMISSIONS]
    conn.execute(
        """
        INSERT INTO editors (
          id, user_id, organization_id, is_org_admin,
          perm_manuscripts, perm_messages, perm_pipeline, perm_offers, perm_payouts, perm_assistants,
          status, created_at
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (editor_id, user_id, org_id, *perm_values, utcnow()),
    )
    _default_persona(conn, editor_id, org_id)
    conn.commit()
    return editor_id


def seed_local_accounts(conn) -> dict:
    """Owner and one author for local sign-in and demo mode.

    The owner is the editor. A second editor is not created. The owner adds one
    from System Admin when a separate practice is needed. Safe to call again.
    The owner's persona is published, and the author is already that editor's customer.
    """
    owner_id = create_owner(conn, "owner@example.com")
    _retire_baked_editor(conn)
    editor = editor_for_user(conn, owner_id)
    if editor is None or editor["status"] != "active" or not editor["is_org_admin"]:
        raise ValueError("owner@example.com is not an active editor")
    persona = conn.execute(
        "SELECT * FROM personas WHERE editor_id = ? ORDER BY created_at LIMIT 1",
        (editor["id"],),
    ).fetchone()
    if persona is not None and persona["status"] == "draft":
        publish_persona(conn, editor, persona["id"])
    author_user_id = _seed_author(conn, editor)
    return {
        "owner_id": owner_id,
        "editor_user_id": owner_id,
        "editor_id": editor["id"],
        "author_user_id": author_user_id,
    }


def _retire_baked_editor(conn) -> None:
    """Older local seeds created editor@example.com. Dev and demo no longer do."""
    user = conn.execute(
        "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
        ("editor@example.com",),
    ).fetchone()
    if user is None:
        return
    conn.execute("UPDATE users SET deleted_at = ? WHERE id = ?", (utcnow(), user["id"]))
    conn.execute(
        "UPDATE editors SET status = 'removed' WHERE user_id = ? AND status != 'removed'",
        (user["id"],),
    )
    conn.commit()


def _seed_author(conn, editor) -> str:
    """The baked author is the owner's customer. An older seed left them on editor@example.com."""
    author = conn.execute(
        "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
        ("author@example.com",),
    ).fetchone()
    if author is not None:
        row = conn.execute(
            "SELECT * FROM authors WHERE user_id = ?",
            (author["id"],),
        ).fetchone()
        if row is not None and row["editor_id"] != editor["id"]:
            previous = conn.execute(
                """
                SELECT u.email FROM editors e
                JOIN users u ON u.id = e.user_id
                WHERE e.id = ?
                """,
                (row["editor_id"],),
            ).fetchone()
            if previous is None or previous["email"] != "editor@example.com":
                raise ValueError("author already belongs to an editor")
            conn.execute(
                "UPDATE authors SET editor_id = ?, organization_id = ? WHERE id = ?",
                (editor["id"], editor["organization_id"], row["id"]),
            )
            conn.commit()
    return become_customer(conn, editor["id"], "Bea", "author@example.com")


def open_owner_practice(conn, user_id: str, practice_name: str) -> str:
    """The owner already has a practice. A second call returns that practice.

    A name is used only when this login does not have one yet.
    """
    user = conn.execute(
        "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()
    if user is None or not user_is_owner(user):
        raise ValueError("only the platform owner can open a practice on this login")
    existing = editor_for_user(conn, user_id)
    if existing is not None and existing["status"] != "removed":
        return existing["id"]
    if existing is not None:
        raise ValueError("this login's practice was removed")
    name = (practice_name or "").strip()
    if not name:
        raise ValueError("a name is required")
    return _create_practice(conn, user_id, name)


def practice_appearance(conn, organization_id: str) -> dict:
    row = conn.execute(
        """
        SELECT theme_profile, theme_mode, theme_accent, theme_paper, theme_ink
        FROM organizations
        WHERE id = ?
        """,
        (organization_id,),
    ).fetchone()
    if row is None:
        return {"profile": "desk", "mode": "light", "accent": "", "paper": "", "ink": ""}
    return {
        "profile": row["theme_profile"] or "desk",
        "mode": row["theme_mode"] or "light",
        "accent": row["theme_accent"] or "",
        "paper": row["theme_paper"] or "",
        "ink": row["theme_ink"] or "",
    }


_APPEARANCE_PROFILES = {"desk", "ink", "forest", "sea", "custom"}
_APPEARANCE_MODES = {"light", "dark"}
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def set_practice_appearance(
    conn,
    editor_row,
    *,
    profile: str,
    mode: str,
    accent: str = "",
    paper: str = "",
    ink: str = "",
) -> None:
    """Save the colors for this practice. Custom uses the three hex colors."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    profile = (profile or "").strip().lower()
    mode = (mode or "").strip().lower()
    if profile not in _APPEARANCE_PROFILES:
        raise ValueError("a theme is required")
    if mode not in _APPEARANCE_MODES:
        raise ValueError("a color mode is required")
    accent = (accent or "").strip()
    paper = (paper or "").strip()
    ink = (ink or "").strip()
    if profile == "custom":
        for value, label in ((accent, "accent"), (paper, "background"), (ink, "text")):
            if not _HEX_COLOR.fullmatch(value):
                raise ValueError(f"a {label} color is required")
    else:
        accent = ""
        paper = ""
        ink = ""
    conn.execute(
        """
        UPDATE organizations
        SET theme_profile = ?, theme_mode = ?, theme_accent = ?, theme_paper = ?, theme_ink = ?
        WHERE id = ?
        """,
        (profile, mode, accent, paper, ink, editor_row["organization_id"]),
    )
    conn.commit()


def set_practice_settings(
    conn,
    editor_row,
    *,
    name: str | None = None,
    profile: str | None = None,
    mode: str | None = None,
    accent: str | None = None,
    paper: str | None = None,
    ink: str | None = None,
) -> None:
    """Change this practice's name, colors, or both. Omitted fields stay as they are."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    if name is not None:
        rename_own_practice(conn, editor_row["user_id"], name)
    changing_colors = any(value is not None for value in (profile, mode, accent, paper, ink))
    if not changing_colors:
        if name is None:
            raise ValueError("nothing to save")
        return
    current = practice_appearance(conn, editor_row["organization_id"])
    set_practice_appearance(
        conn,
        editor_row,
        profile=current["profile"] if profile is None else profile,
        mode=current["mode"] if mode is None else mode,
        accent=current["accent"] if accent is None else accent,
        paper=current["paper"] if paper is None else paper,
        ink=current["ink"] if ink is None else ink,
    )


def author_notices_account(conn, organization_id: str):
    return conn.execute(
        """
        SELECT * FROM mail_accounts
        WHERE purpose = 'author_notices' AND organization_id = ?
        """,
        (organization_id,),
    ).fetchone()


def invitation_mail_account(conn, user_id: str):
    return conn.execute(
        """
        SELECT * FROM mail_accounts
        WHERE purpose = 'invitations' AND user_id = ?
        """,
        (user_id,),
    ).fetchone()


def mail_public(row) -> dict:
    """Status for a page or a tool. The secret itself is never included."""
    if row is None:
        return {
            "configured": False,
            "transport": "",
            "from_address": "",
            "smtp_host": "",
            "smtp_port": 0,
            "smtp_security": "",
            "smtp_username": "",
            "https_provider": "",
            "mailgun_domain": "",
            "mailgun_region": "",
            "notice_origin": "",
            "secret": "not saved",
        }
    return {
        "configured": True,
        "transport": row["transport"],
        "from_address": row["from_address"],
        "smtp_host": row["smtp_host"],
        "smtp_port": row["smtp_port"],
        "smtp_security": row["smtp_security"],
        "smtp_username": row["smtp_username"],
        "https_provider": row["https_provider"],
        "mailgun_domain": row["mailgun_domain"],
        "mailgun_region": row["mailgun_region"],
        "notice_origin": row["notice_origin"] or "",
        "secret": "saved" if row["secret"] else "not saved",
    }


def set_author_notices(conn, editor_row, *, notice_origin: str = "", **fields) -> None:
    """Save this practice's mailbox for author notices. A failed check is not stored."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    current = author_notices_account(conn, editor_row["organization_id"])
    account = _checked_account(fields, current)
    origin = mail.clean_origin(notice_origin)
    if not origin and current is not None:
        origin = current["notice_origin"] or ""
    _save_mail(
        conn,
        current,
        purpose="author_notices",
        user_id=None,
        organization_id=editor_row["organization_id"],
        account=account,
        notice_origin=origin,
    )


def set_invitation_mail(conn, user, **fields) -> None:
    """Save the owner's mailbox for editor and admin invites. A failed check is not stored."""
    if not user_is_owner(user):
        raise ValueError("only the owner can change invitation mail")
    current = invitation_mail_account(conn, user["id"])
    account = _checked_account(fields, current)
    _save_mail(
        conn,
        current,
        purpose="invitations",
        user_id=user["id"],
        organization_id=None,
        account=account,
        notice_origin="",
    )


def clear_author_notices(conn, editor_row) -> None:
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    conn.execute(
        "DELETE FROM mail_accounts WHERE purpose = 'author_notices' AND organization_id = ?",
        (editor_row["organization_id"],),
    )
    conn.commit()


def clear_invitation_mail(conn, user) -> None:
    if not user_is_owner(user):
        raise ValueError("only the owner can change invitation mail")
    conn.execute(
        "DELETE FROM mail_accounts WHERE purpose = 'invitations' AND user_id = ?",
        (user["id"],),
    )
    conn.commit()


def inference_rows(conn, organization_id: str):
    return conn.execute(
        """
        SELECT * FROM inference_providers
        WHERE organization_id = ?
        ORDER BY created_at, label
        """,
        (organization_id,),
    ).fetchall()


def inference_public(rows) -> list[dict]:
    """Providers and the models they listed. The key itself is never included."""
    listed = []
    for row in rows:
        needs_key = llm.PROVIDERS.get(row["provider"], {}).get("key", True)
        if row["secret"]:
            secret = "saved"
        elif needs_key:
            secret = "not saved"
        else:
            secret = "not required"
        entries = llm.stored_catalog(row["models_json"])
        prices = {
            entry["id"]: {"prompt": entry["prompt"], "completion": entry["completion"]}
            for entry in entries
            if "prompt" in entry and "completion" in entry
        }
        listed.append(
            {
                "label": row["label"],
                "provider": row["provider"],
                "base_url": row["base_url"],
                "models": [entry["id"] for entry in entries],
                "prices": prices,
                "checked_at": row["checked_at"] or "",
                "secret": secret,
            }
        )
    return listed


def set_inference(conn, editor_row, *, label: str, provider: str, base_url: str = "", secret: str = "") -> None:
    """Save one provider after it lists models. A failed check is not stored."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    label = llm.clean_label(label)
    current = conn.execute(
        """
        SELECT * FROM inference_providers
        WHERE organization_id = ? AND label = ?
        """,
        (editor_row["organization_id"], label),
    ).fetchone()
    saved = current["secret"] if current is not None else ""
    account = llm.prepare_account(
        provider=provider,
        base_url=base_url,
        secret=secret,
        saved_secret=saved,
    )
    models = llm.probe_models(account)
    now = utcnow()
    payload = json.dumps(llm.catalog_payload(models))
    if current is None:
        conn.execute(
            """
            INSERT INTO inference_providers (
              id, organization_id, provider, label, base_url, secret,
              models_json, checked_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                editor_row["organization_id"],
                account["provider"],
                label,
                account["base"],
                account["key"],
                payload,
                now,
                now,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE inference_providers
            SET provider = ?, base_url = ?, secret = ?, models_json = ?, checked_at = ?
            WHERE id = ?
            """,
            (account["provider"], account["base"], account["key"], payload, now, current["id"]),
        )
    conn.commit()


def clear_inference(conn, editor_row, label: str) -> None:
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    label = llm.clean_label(label)
    conn.execute(
        """
        DELETE FROM inference_providers
        WHERE organization_id = ? AND label = ?
        """,
        (editor_row["organization_id"], label),
    )
    conn.commit()


def refresh_inference_catalog(conn, editor_row, label: str) -> int:
    """Ask one saved provider for its model list again. A failed check keeps the old list."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    label = llm.clean_label(label)
    row = conn.execute(
        """
        SELECT * FROM inference_providers
        WHERE organization_id = ? AND label = ?
        """,
        (editor_row["organization_id"], label),
    ).fetchone()
    if row is None:
        raise ValueError("that provider is not saved")
    models = llm.probe_models(llm.account_from_row(row))
    remember_inference_models(conn, row["id"], models)
    conn.commit()
    return len(models)


def remember_inference_models(conn, row_id: str, models: list) -> None:
    conn.execute(
        """
        UPDATE inference_providers
        SET models_json = ?, checked_at = ?
        WHERE id = ?
        """,
        (json.dumps(llm.catalog_payload(models)), utcnow(), row_id),
    )


def inference_defaults(conn, organization_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT model FROM inference_defaults
        WHERE organization_id = ?
        ORDER BY position
        """,
        (organization_id,),
    ).fetchall()
    return [row["model"] for row in rows]


def set_inference_defaults(conn, editor_row, models) -> None:
    """Replace this practice's default models. An empty list clears them."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can change settings")
    cleaned = llm.clean_default_models(models)
    org = editor_row["organization_id"]
    conn.execute("DELETE FROM inference_defaults WHERE organization_id = ?", (org,))
    now = utcnow()
    for position, model in enumerate(cleaned):
        conn.execute(
            """
            INSERT INTO inference_defaults (
              id, organization_id, model, position, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (new_id(), org, model, position, now),
        )
    conn.commit()


def send_invite(conn, to_email: str, token: str, origin: str) -> str:
    """Email one owner-issued invite. skipped when unset, failed when the send does not.

    The mailbox is the owner's, including when an admin created the invite.
    The caller still shows the link.
    """
    try:
        owner = conn.execute(
            "SELECT id FROM users WHERE is_owner = 1 AND deleted_at IS NULL"
        ).fetchone()
        if owner is None:
            return "skipped"
        row = invitation_mail_account(conn, owner["id"])
        if row is None or not row["secret"]:
            return "skipped"
        link_origin = mail.clean_origin(origin)
        link = f"{link_origin}/invite/{token}" if link_origin else f"/invite/{token}"
        mail.deliver(
            dict(row),
            to_email,
            "SIVED invitation",
            f"You are invited to SIVED.\n\n{link}\n",
        )
        return "sent"
    except Exception:
        return "failed"


def notify_author(conn, job_id: str) -> None:
    """Tell the author a manuscript is ready or did not finish.

    Uses that editor's mailbox and the author's sign-in email. A failure
    does not change the job. Nothing is sent when the editor has not
    connected a mailbox.
    """
    try:
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None or job["status"] not in ("ready", "failed"):
            return
        author = conn.execute(
            """
            SELECT u.email
            FROM authors a
            JOIN users u ON u.id = a.user_id
            WHERE a.id = ?
            """,
            (job["author_id"],),
        ).fetchone()
        if author is None or not author["email"]:
            return
        row = author_notices_account(conn, job["organization_id"])
        if row is None or not row["secret"]:
            return
        title = _notice_title(job["title"] or "")
        if job["status"] == "ready":
            sentence = f"{title} is ready."
        else:
            sentence = f"{title} did not finish."
        origin = mail.clean_origin(row["notice_origin"] or "")
        if origin:
            text = f"{sentence}\n\n{origin}/jobs/{job_id}\n"
        else:
            text = f"{sentence}\n"
        mail.deliver(dict(row), author["email"], sentence, text)
    except Exception:
        print("author notice was not sent", file=sys.stderr)


def _notice_title(title: str) -> str:
    cleaned = "".join(ch if ord(ch) >= 32 else " " for ch in title)
    cleaned = " ".join(cleaned.split())
    return cleaned[:200] or "Manuscript"


def _checked_account(fields: dict, current) -> dict:
    account = mail.normalize(fields)
    saved = (current["secret"] if current is not None else "") or ""
    account = mail.require_secret(account, saved)
    mail.probe(account)
    return account


def _save_mail(conn, current, *, purpose: str, user_id, organization_id, account: dict, notice_origin: str) -> None:
    values = (
        account["transport"],
        account["from_address"],
        account["smtp_host"],
        account["smtp_port"],
        account["smtp_security"],
        account["smtp_username"],
        account["secret"],
        account["https_provider"],
        account["mailgun_domain"],
        account["mailgun_region"],
        notice_origin,
    )
    if current is None:
        conn.execute(
            """
            INSERT INTO mail_accounts (
              id, purpose, user_id, organization_id, transport, from_address,
              smtp_host, smtp_port, smtp_security, smtp_username, secret,
              https_provider, mailgun_domain, mailgun_region, notice_origin, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id(), purpose, user_id, organization_id, *values, utcnow()),
        )
    else:
        conn.execute(
            """
            UPDATE mail_accounts
            SET transport = ?, from_address = ?, smtp_host = ?, smtp_port = ?,
                smtp_security = ?, smtp_username = ?, secret = ?, https_provider = ?,
                mailgun_domain = ?, mailgun_region = ?, notice_origin = ?
            WHERE id = ?
            """,
            (*values, current["id"]),
        )
    conn.commit()


def rename_own_practice(conn, user_id: str, practice_name: str) -> None:
    editor = editor_for_user(conn, user_id)
    if editor is None or editor["status"] != "active" or not editor["is_org_admin"]:
        raise ValueError("this login has no practice")
    name = (practice_name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("a name is required")
    conn.execute(
        "UPDATE organizations SET name = ? WHERE id = ?",
        (name, editor["organization_id"]),
    )
    conn.commit()


_PHONE = re.compile(r"^[0-9+().\-\s]{0,40}$")


def update_profile(conn, user_id: str, *, name: str, contact_email: str, phone: str) -> None:
    """Name and contact information. The sign-in email stays the account."""
    name = (name or "").strip()
    contact = (contact_email or "").strip().lower()
    phone = (phone or "").strip()
    if not name or len(name) > 80:
        raise ValueError("a name is required")
    if contact and (
        len(contact) > 120
        or "@" not in contact
        or contact.startswith("@")
        or contact.endswith("@")
    ):
        raise ValueError("contact email is not valid")
    if not _PHONE.fullmatch(phone):
        raise ValueError("phone is not valid")
    conn.execute(
        "UPDATE users SET name = ?, contact_email = ?, phone = ? WHERE id = ?",
        (name, contact, phone, user_id),
    )
    conn.commit()


def invite_editor(
    conn,
    invited_by: str,
    email: str,
    organization_id: str | None,
    permissions: dict | None = None,
) -> str:
    if organization_id:
        inviter = editor_for_user(conn, invited_by)
        if (
            inviter is None
            or inviter["organization_id"] != organization_id
            or not allowed(inviter, "assistants")
        ):
            raise ValueError("you cannot add assistants")
        flags = permissions if permissions is not None else {
            "manuscripts": True,
            "messages": True,
            "pipeline": True,
        }
        if not any(flags.get(key) for key, _label in PERMISSIONS):
            raise ValueError("choose at least one permission")
        if not inviter["is_org_admin"]:
            for key, _label in PERMISSIONS:
                if flags.get(key) and not allowed(inviter, key):
                    raise ValueError("you cannot grant a permission you do not have")
    else:
        inviter = conn.execute("SELECT * FROM users WHERE id = ?", (invited_by,)).fetchone()
        if inviter is None or not user_is_system_admin(inviter):
            raise ValueError("only an owner or an admin can open a new practice")
        flags = {}
    raw = token()
    kind = "assistant" if organization_id else "editor"
    perm_values = [1 if flags.get(key) else 0 for key, _label in PERMISSIONS]
    conn.execute(
        """
        INSERT INTO editor_invites (
          id, email, token_hash, invited_by, organization_id, kind,
          perm_manuscripts, perm_messages, perm_pipeline, perm_offers, perm_payouts, perm_assistants,
          expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+14 days'), ?)
        """,
        (new_id(), email.lower(), token_hash(raw), invited_by, organization_id, kind, *perm_values, utcnow()),
    )
    conn.commit()
    return raw


def accept_editor_invite(conn, raw_token: str, name: str) -> str:
    row = conn.execute(
        """
        SELECT * FROM editor_invites
        WHERE token_hash = ? AND accepted_at IS NULL AND revoked_at IS NULL
          AND expires_at > datetime('now')
        """,
        (token_hash(raw_token),),
    ).fetchone()
    if row is None:
        raise ValueError("invite is not valid")
    if row["kind"] == "admin":
        return _accept_admin_invite(conn, row, name)
    email = row["email"]
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL", (email,)
    ).fetchone()
    if user and user["role"] != "editor":
        raise ValueError("that email already has another role")
    if user is None:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, email, name, role, auth_provider, auth_subject, created_at)
            VALUES (?, ?, ?, 'editor', 'dev', ?, ?)
            """,
            (user_id, email, name, f"dev:{email}", utcnow()),
        )
    else:
        user_id = user["id"]
    if conn.execute("SELECT id FROM editors WHERE user_id = ?", (user_id,)).fetchone():
        raise ValueError("editor already exists")
    editor_id = new_id()
    if row["organization_id"]:
        org_id = row["organization_id"]
        is_admin = 0
        perm_values = [int(row[f"perm_{key}"]) for key, _label in PERMISSIONS]
    else:
        org_id = new_id()
        conn.execute(
            """
            INSERT INTO organizations (id, name, plan, created_at)
            VALUES (?, ?, 'indie', ?)
            """,
            (org_id, name or email, utcnow()),
        )
        is_admin = 1
        perm_values = [1 for _key, _label in PERMISSIONS]
    conn.execute(
        """
        INSERT INTO editors (
          id, user_id, organization_id, is_org_admin,
          perm_manuscripts, perm_messages, perm_pipeline, perm_offers, perm_payouts, perm_assistants,
          status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (editor_id, user_id, org_id, is_admin, *perm_values, utcnow()),
    )
    if is_admin:
        _default_persona(conn, editor_id, org_id)
    conn.execute("UPDATE editor_invites SET accepted_at = ? WHERE id = ?", (utcnow(), row["id"]))
    conn.commit()
    return user_id


def invite_admin(conn, invited_by: str, email: str) -> str:
    """The owner adds an admin. An admin cannot add one, and the owner is not an admin seat."""
    actor = conn.execute(
        "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL",
        (invited_by,),
    ).fetchone()
    if actor is None or not user_is_owner(actor):
        raise ValueError("only the owner can add an admin")
    email = (email or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("an email is required")
    existing = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL",
        (email,),
    ).fetchone()
    if existing is not None and user_is_owner(existing):
        raise ValueError("the owner cannot be changed")
    raw = token()
    conn.execute(
        """
        INSERT INTO editor_invites (
          id, email, token_hash, invited_by, organization_id, kind,
          perm_manuscripts, perm_messages, perm_pipeline, perm_offers, perm_payouts, perm_assistants,
          expires_at, created_at
        ) VALUES (?, ?, ?, ?, NULL, 'admin', 0, 0, 0, 0, 0, 0, datetime('now', '+14 days'), ?)
        """,
        (new_id(), email, token_hash(raw), invited_by, utcnow()),
    )
    conn.commit()
    return raw


def _accept_admin_invite(conn, row, name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("a name is required")
    email = row["email"]
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL",
        (email,),
    ).fetchone()
    if user is not None and user_is_owner(user):
        raise ValueError("the owner cannot be changed")
    if user is None:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (
              id, email, name, role, is_admin, auth_provider, auth_subject, created_at
            ) VALUES (?, ?, ?, 'admin', 1, 'dev', ?, ?)
            """,
            (user_id, email, name, f"dev:{email}", utcnow()),
        )
    else:
        user_id = user["id"]
        conn.execute(
            "UPDATE users SET is_admin = 1, name = COALESCE(NULLIF(name, ''), ?) WHERE id = ?",
            (name, user_id),
        )
    conn.execute("UPDATE editor_invites SET accepted_at = ? WHERE id = ?", (utcnow(), row["id"]))
    conn.commit()
    return user_id


def suspend_practice(conn, actor, editor_id: str, reason: str) -> None:
    """Suspend a practice. The owner's practice cannot be suspended."""
    if not user_is_system_admin(actor):
        raise ValueError("that is not assigned to this login")
    row = conn.execute(
        """
        SELECT e.id, u.is_owner, u.role
        FROM editors e
        JOIN users u ON u.id = e.user_id
        WHERE e.id = ?
        """,
        (editor_id,),
    ).fetchone()
    if row is None:
        raise ValueError("that practice was not found")
    if user_is_owner(row):
        raise ValueError("the owner cannot be changed")
    conn.execute("UPDATE editors SET status = 'suspended' WHERE id = ?", (editor_id,))
    conn.execute(
        """
        INSERT INTO admin_audit (
          id, actor_user_id, action, target_type, target_id, reason, created_at
        ) VALUES (?, ?, 'suspend', 'editor', ?, ?, ?)
        """,
        (new_id(), actor["id"], editor_id, reason or "", utcnow()),
    )
    conn.commit()


def remove_admin(conn, actor, user_id: str) -> None:
    """The owner can remove an admin. Nobody can clear the owner flag."""
    if not user_is_owner(actor):
        raise ValueError("only the owner can change an admin")
    target = conn.execute(
        "SELECT * FROM users WHERE id = ? AND deleted_at IS NULL",
        (user_id,),
    ).fetchone()
    if target is None:
        raise ValueError("that account was not found")
    if user_is_owner(target):
        raise ValueError("the owner cannot be changed")
    if not user_is_admin(target):
        raise ValueError("that account is not an admin")
    conn.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (user_id,))
    conn.commit()


def _default_persona(conn, editor_id: str, org_id: str) -> str:
    persona_id = new_id()
    now = utcnow()
    files, code = starter_files()
    conn.execute(
        """
        INSERT INTO personas (
          id, editor_id, organization_id, name, instructions, agent_code, agent_skills,
          compendium_enabled, readback_enabled, max_words, status, created_at, updated_at
        ) VALUES (?, ?, ?, 'Developmental editor', '', ?, '[]', 1, 1, 180000, 'draft', ?, ?)
        """,
        (persona_id, editor_id, org_id, code, now, now),
    )
    for ask, position, _title in PASSES:
        conn.execute(
            """
            INSERT INTO persona_passes (persona_id, editor_id, ask, position, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (persona_id, editor_id, ask, position),
        )
    root = persona_dir(persona_id)
    backup = replace_tree(root, files)
    discard_backup(backup)
    return persona_id


def revoke_editor_invite(conn, admin_row, invite_id: str) -> None:
    if not allowed(admin_row, "assistants"):
        raise ValueError("you cannot change assistants")
    row = conn.execute(
        """
        SELECT id FROM editor_invites
        WHERE id = ? AND organization_id = ? AND accepted_at IS NULL AND revoked_at IS NULL
        """,
        (invite_id, admin_row["organization_id"]),
    ).fetchone()
    if row is None:
        raise ValueError("invite is not valid")
    conn.execute("UPDATE editor_invites SET revoked_at = ? WHERE id = ?", (utcnow(), invite_id))
    conn.commit()


def pending_editor_invites(conn, editor_row):
    return conn.execute(
        """
        SELECT id, email FROM editor_invites
        WHERE organization_id = ? AND accepted_at IS NULL AND revoked_at IS NULL
          AND expires_at > datetime('now')
        ORDER BY created_at
        """,
        (editor_row["organization_id"],),
    ).fetchall()


def practice_editors(conn, editor_row):
    return conn.execute(
        """
        SELECT e.*, u.email
        FROM editors e JOIN users u ON u.id = e.user_id
        WHERE e.organization_id = ? AND e.status = 'active'
        ORDER BY e.is_org_admin DESC, u.email
        """,
        (editor_row["organization_id"],),
    ).fetchall()


def set_assistant_permissions(conn, actor, editor_id: str, permissions: dict) -> None:
    if not allowed(actor, "assistants"):
        raise ValueError("you cannot change assistants")
    other = conn.execute(
        """
        SELECT * FROM editors
        WHERE id = ? AND organization_id = ? AND status = 'active'
        """,
        (editor_id, actor["organization_id"]),
    ).fetchone()
    if other is None or other["is_org_admin"]:
        raise ValueError("that assistant cannot be changed")
    if other["id"] == actor["id"]:
        raise ValueError("you cannot change your own permissions")
    resolved = {}
    for key, _label in PERMISSIONS:
        want = bool(permissions.get(key))
        if not actor["is_org_admin"] and not allowed(actor, key):
            if want:
                raise ValueError("you cannot grant a permission you do not have")
            want = bool(other[f"perm_{key}"])
        resolved[key] = want
    if not any(resolved.values()):
        raise ValueError("choose at least one permission")
    assignments = ", ".join(f"perm_{key} = ?" for key, _label in PERMISSIONS)
    values = [1 if resolved[key] else 0 for key, _label in PERMISSIONS]
    conn.execute(
        f"UPDATE editors SET {assignments} WHERE id = ?",
        (*values, other["id"]),
    )
    conn.commit()


def remove_editor(conn, admin_row, editor_id: str) -> None:
    """Close an assistant login. Authors and manuscripts stay on the practice."""
    if not allowed(admin_row, "assistants"):
        raise ValueError("you cannot change assistants")
    if admin_row["id"] == editor_id:
        raise ValueError("you cannot remove yourself")
    other = conn.execute(
        """
        SELECT * FROM editors
        WHERE id = ? AND organization_id = ? AND status = 'active'
        """,
        (editor_id, admin_row["organization_id"]),
    ).fetchone()
    if other is None or other["is_org_admin"]:
        raise ValueError("that editor cannot be removed")
    admin_id = practice_admin(conn, admin_row)["id"]
    other_id = other["id"]
    conn.execute("UPDATE authors SET editor_id = ? WHERE editor_id = ?", (admin_id, other_id))
    conn.execute(
        """
        UPDATE author_invites SET editor_id = ?
        WHERE editor_id = ? AND accepted_at IS NULL AND revoked_at IS NULL
        """,
        (admin_id, other_id),
    )
    conn.execute("UPDATE jobs SET editor_id = ? WHERE editor_id = ?", (admin_id, other_id))
    conn.execute("UPDATE job_passes SET editor_id = ? WHERE editor_id = ?", (admin_id, other_id))
    conn.execute("UPDATE pipeline_items SET editor_id = ? WHERE editor_id = ?", (admin_id, other_id))
    conn.execute("UPDATE messages SET editor_id = ? WHERE editor_id = ?", (admin_id, other_id))
    conn.execute(
        """
        UPDATE personas SET status = 'paused', updated_at = ?
        WHERE editor_id = ? AND status != 'paused'
        """,
        (utcnow(), other_id),
    )
    conn.execute("UPDATE editors SET status = 'removed' WHERE id = ?", (other_id,))
    conn.commit()


def editor_public(conn, editor_id: str):
    return conn.execute(
        """
        SELECT e.id, e.status, e.is_org_admin, e.organization_id, e.patreon_campaign_id,
               e.patreon_client_id,
               CASE WHEN e.patreon_client_secret != '' THEN 1 ELSE 0 END AS patreon_has_secret,
               u.name, u.email, o.name AS org_name
        FROM editors e
        JOIN users u ON u.id = e.user_id
        JOIN organizations o ON o.id = e.organization_id
        WHERE e.id = ?
        """,
        (editor_id,),
    ).fetchone()


def become_customer(conn, editor_id: str, name: str, email: str) -> str:
    """An author finds an editor's page and becomes that editor's customer.

    The same email can sign in again for that editor. An author stays with
    one editor.
    """
    name = name.strip()
    email = email.strip().lower()
    if not name:
        raise ValueError("a name is required")
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("an email is required")
    editor = conn.execute("SELECT * FROM editors WHERE id = ?", (editor_id,)).fetchone()
    if editor is None or editor["status"] != "active" or not editor["is_org_admin"]:
        raise ValueError("this editor is not taking customers")
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL",
        (email,),
    ).fetchone()
    if user and user["role"] != "author":
        raise ValueError("that email already has another role")
    if user is None:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, email, name, role, auth_provider, auth_subject, created_at)
            VALUES (?, ?, ?, 'author', 'dev', ?, ?)
            """,
            (user_id, email, name, f"dev:{email}", utcnow()),
        )
    else:
        user_id = user["id"]
        existing = conn.execute("SELECT * FROM authors WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            if existing["editor_id"] == editor["id"]:
                return user_id
            raise ValueError("author already belongs to an editor")
    conn.execute(
        """
        INSERT INTO authors (id, user_id, editor_id, organization_id, status, created_at)
        VALUES (?, ?, ?, ?, 'active', ?)
        """,
        (new_id(), user_id, editor["id"], editor["organization_id"], utcnow()),
    )
    conn.commit()
    return user_id


class MembershipDenied(Exception):
    """This login is not an active member of the editor's Patreon campaign."""


def patreon_ready(row) -> bool:
    """True when this editor has saved their own client, secret, and campaign."""
    if row is None:
        return False
    keys = row.keys()
    campaign = row["patreon_campaign_id"] if "patreon_campaign_id" in keys else ""
    client_id = row["patreon_client_id"] if "patreon_client_id" in keys else ""
    if "patreon_has_secret" in keys:
        has_secret = bool(row["patreon_has_secret"])
    elif "patreon_client_secret" in keys:
        has_secret = bool(row["patreon_client_secret"])
    else:
        has_secret = False
    return bool(campaign and client_id and has_secret)


def _patreon_value(value: str, label: str, *, required: bool) -> str:
    value = (value or "").strip()
    if not value:
        if required:
            raise ValueError(f"a Patreon {label} is required")
        return ""
    if len(value) > 200 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError(f"a Patreon {label} is required")
    return value


def set_patreon_integration(
    conn,
    editor_row,
    *,
    campaign_id: str,
    client_id: str,
    client_secret: str = "",
    webhook_secret: str = "",
) -> None:
    """Save this editor's own Patreon client. A blank secret keeps the saved one."""
    if not editor_row["is_org_admin"]:
        raise ValueError("only the editor can connect Patreon")
    campaign_id = (campaign_id or "").strip()
    if not campaign_id.isdigit() or not 1 <= len(campaign_id) <= 20:
        raise ValueError("a Patreon campaign id is required")
    client_id = _patreon_value(client_id, "client id", required=True)
    current = conn.execute(
        "SELECT patreon_client_secret, patreon_webhook_secret FROM editors WHERE id = ?",
        (editor_row["id"],),
    ).fetchone()
    secret = _patreon_value(client_secret, "client secret", required=False)
    if not secret:
        secret = (current["patreon_client_secret"] if current else "") or ""
    if not secret:
        raise ValueError("a Patreon client secret is required")
    hook = _patreon_value(webhook_secret, "webhook secret", required=False)
    if not hook:
        hook = (current["patreon_webhook_secret"] if current else "") or ""
    conn.execute(
        """
        UPDATE editors
        SET patreon_campaign_id = ?, patreon_client_id = ?, patreon_client_secret = ?,
            patreon_webhook_secret = ?
        WHERE id = ?
        """,
        (campaign_id, client_id, secret, hook, editor_row["id"]),
    )
    # The next author request must ask Patreon with this client and campaign.
    conn.execute(
        "UPDATE authors SET membership_checked_at = '' WHERE editor_id = ?",
        (editor_row["id"],),
    )
    conn.commit()


def _patreon_creds(conn, editor_id: str):
    return conn.execute(
        """
        SELECT id, organization_id, status, is_org_admin, patreon_campaign_id,
               patreon_client_id, patreon_client_secret, patreon_webhook_secret
        FROM editors
        WHERE id = ?
        """,
        (editor_id,),
    ).fetchone()


def begin_patreon_login(conn, editor_id: str) -> str:
    editor = editor_public(conn, editor_id)
    if editor is None or editor["status"] != "active" or not editor["is_org_admin"]:
        raise ValueError("this editor is not taking customers")
    if not patreon_ready(editor):
        raise ValueError("this editor has not connected Patreon")
    state = secrets.token_urlsafe(32)
    now = utcnow()
    conn.execute("DELETE FROM patreon_oauth_states WHERE expires_at <= datetime('now')")
    conn.execute(
        """
        INSERT INTO patreon_oauth_states (state, editor_id, created_at, expires_at)
        VALUES (?, ?, ?, datetime('now', '+10 minutes'))
        """,
        (token_hash(state), editor_id, now),
    )
    conn.commit()
    return state


def exchange_patreon_code(conn, editor_id: str, code: str, host: str, scheme: str = "http") -> dict:
    """Trade the one-time code with this editor's client. The secret stays here."""
    creds = _patreon_creds(conn, editor_id)
    if not patreon_ready(creds):
        raise ValueError("this editor has not connected Patreon")
    try:
        return patreon.login_from_code(
            code,
            host,
            creds["patreon_client_id"],
            creds["patreon_client_secret"],
            scheme,
        )
    except patreon.PatreonError as exc:
        raise ValueError(str(exc)) from exc


def take_patreon_state(conn, state: str) -> str:
    digest = token_hash(state or "")
    row = conn.execute(
        """
        SELECT editor_id FROM patreon_oauth_states
        WHERE state = ? AND expires_at > datetime('now')
        """,
        (digest,),
    ).fetchone()
    conn.execute("DELETE FROM patreon_oauth_states WHERE state = ?", (digest,))
    conn.commit()
    if row is None:
        raise ValueError("that Patreon login expired")
    return row["editor_id"]


def accept_patreon_member(conn, editor_id: str, member: dict) -> str:
    """Open or reuse the author row after Patreon confirmed the campaign."""
    editor = _patreon_creds(conn, editor_id)
    if editor is None or editor["status"] != "active" or not editor["is_org_admin"]:
        raise ValueError("this editor is not taking customers")
    if not patreon_ready(editor):
        raise ValueError("this editor has not connected Patreon")
    campaign = editor["patreon_campaign_id"] or ""
    if campaign not in member["campaigns"]:
        raise ValueError("this Patreon account is not an active member of this editor's campaign")
    patreon_user_id = str(member["patreon_user_id"])
    email = member["email"]
    name = (member.get("name") or "").strip() or email
    existing = conn.execute(
        """
        SELECT * FROM users
        WHERE auth_provider = 'patreon' AND auth_subject = ? AND deleted_at IS NULL
        """,
        (patreon_user_id,),
    ).fetchone()
    by_email = conn.execute(
        "SELECT * FROM users WHERE email = ? AND deleted_at IS NULL",
        (email,),
    ).fetchone()
    if existing is not None and by_email is not None and existing["id"] != by_email["id"]:
        raise ValueError("that email already has another role")
    if existing is None and by_email is not None:
        if by_email["role"] != "author" or by_email["auth_provider"] != "patreon":
            raise ValueError("that email already has another role")
        existing = by_email
    if existing is None:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (
              id, email, name, role, auth_provider, auth_subject, created_at
            ) VALUES (?, ?, ?, 'author', 'patreon', ?, ?)
            """,
            (user_id, email, name, patreon_user_id, utcnow()),
        )
    else:
        if existing["role"] != "author":
            raise ValueError("that email already has another role")
        user_id = existing["id"]
        conn.execute(
            "UPDATE users SET email = ?, name = COALESCE(NULLIF(name, ''), ?) WHERE id = ?",
            (email, name, user_id),
        )
    author = conn.execute("SELECT * FROM authors WHERE user_id = ?", (user_id,)).fetchone()
    now = utcnow()
    refresh = member.get("refresh_token") or ""
    if author is None:
        conn.execute(
            """
            INSERT INTO authors (
              id, user_id, editor_id, organization_id, patreon_user_id, patron_status,
              membership_checked_at, patreon_refresh_token, status, created_at
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 'active', ?)
            """,
            (new_id(), user_id, editor["id"], editor["organization_id"], patreon_user_id, now, refresh, now),
        )
    else:
        if author["editor_id"] != editor["id"]:
            raise ValueError("author already belongs to an editor")
        conn.execute(
            """
            UPDATE authors
            SET patreon_user_id = ?, patron_status = 'active', membership_checked_at = ?,
                patreon_refresh_token = ?
            WHERE id = ?
            """,
            (patreon_user_id, now, refresh, author["id"]),
        )
    conn.commit()
    return user_id


def author_may_enter(conn, user) -> None:
    """Refuse an author whose Patreon membership is not active.

    DEV_LOGIN and DEMO_MODE skip this check so a local desk can sign in
    without a Patreon client. A real desk leaves both unset.
    """
    if config.DEV_LOGIN or config.DEMO_MODE:
        return
    if user is None or user["role"] != "author":
        author = author_for_user(conn, user["id"]) if user is not None else None
        if author is None:
            return
    else:
        author = author_for_user(conn, user["id"])
    if author is None:
        return
    if author["patron_status"] != "active" or not author["patreon_user_id"]:
        raise MembershipDenied("this Patreon membership is not active")
    if not _membership_stale(author["membership_checked_at"] or ""):
        return
    editor = _patreon_creds(conn, author["editor_id"])
    if not patreon_ready(editor):
        raise MembershipDenied("this editor has not connected Patreon")
    campaign = editor["patreon_campaign_id"]
    try:
        member = patreon.confirm_with_refresh(
            author["patreon_refresh_token"] or "",
            campaign,
            editor["patreon_client_id"],
            editor["patreon_client_secret"],
        )
    except patreon.PatreonError as exc:
        raise MembershipDenied(str(exc)) from exc
    now = utcnow()
    status = "active" if member["active"] else "inactive"
    refresh = member.get("refresh_token") or author["patreon_refresh_token"] or ""
    conn.execute(
        """
        UPDATE authors
        SET patron_status = ?, membership_checked_at = ?, patreon_refresh_token = ?
        WHERE id = ?
        """,
        (status, now, refresh, author["id"]),
    )
    conn.commit()
    if status != "active":
        raise MembershipDenied("this Patreon membership is not active")


def _editor_for_webhook(conn, body: bytes, signature: str):
    """The one editor whose webhook secret signs this body."""
    rows = conn.execute(
        """
        SELECT id, patreon_campaign_id, patreon_webhook_secret
        FROM editors
        WHERE is_org_admin = 1 AND patreon_webhook_secret != ''
        """
    ).fetchall()
    matched = None
    for row in rows:
        if patreon.signature_ok(body, signature, row["patreon_webhook_secret"]):
            if matched is not None:
                raise ValueError("Patreon webhook signature was not valid")
            matched = row
    if matched is None:
        raise ValueError("Patreon webhook signature was not valid")
    return matched


def apply_patreon_webhook(conn, body: bytes, signature: str, event: str) -> None:
    editor = _editor_for_webhook(conn, body, signature)
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Patreon webhook could not be read") from exc
    active = patreon.webhook_active(event, payload)
    if active is None:
        return
    data = payload.get("data") or {}
    rel = data.get("relationships") or {}
    patreon_user_id = str(((rel.get("user") or {}).get("data") or {}).get("id") or "")
    campaign_id = str(((rel.get("campaign") or {}).get("data") or {}).get("id") or "")
    if not patreon_user_id or not campaign_id:
        return
    if campaign_id != (editor["patreon_campaign_id"] or ""):
        return
    status = "active" if active else "inactive"
    conn.execute(
        """
        UPDATE authors
        SET patron_status = ?, membership_checked_at = ?
        WHERE patreon_user_id = ? AND editor_id = ?
        """,
        (status, utcnow(), patreon_user_id, editor["id"]),
    )
    conn.commit()


def _membership_stale(checked: str) -> bool:
    if not checked:
        return True
    try:
        when = datetime.fromisoformat(checked)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - when).total_seconds()
    return age > patreon.RECHECK_SECONDS


def invite_author(conn, editor_row, email: str) -> str:
    if editor_row["status"] != "active":
        raise ValueError("editor is suspended")
    raw = token()
    conn.execute(
        """
        INSERT INTO author_invites (
          id, editor_id, organization_id, email, token_hash, invited_by, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+14 days'), ?)
        """,
        (
            new_id(),
            editor_row["id"],
            editor_row["organization_id"],
            email.lower(),
            token_hash(raw),
            editor_row["user_id"],
            utcnow(),
        ),
    )
    conn.commit()
    return raw


def accept_author_invite(conn, raw_token: str, name: str) -> str:
    row = conn.execute(
        """
        SELECT * FROM author_invites
        WHERE token_hash = ? AND accepted_at IS NULL AND revoked_at IS NULL
          AND expires_at > datetime('now')
        """,
        (token_hash(raw_token),),
    ).fetchone()
    if row is None:
        raise ValueError("invite is not valid")
    email = row["email"]
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if user and user["role"] != "author":
        raise ValueError("that email already has another role")
    if user is None:
        user_id = new_id()
        conn.execute(
            """
            INSERT INTO users (id, email, name, role, auth_provider, auth_subject, created_at)
            VALUES (?, ?, ?, 'author', 'dev', ?, ?)
            """,
            (user_id, email, name, f"dev:{email}", utcnow()),
        )
    else:
        user_id = user["id"]
    if conn.execute("SELECT id FROM authors WHERE user_id = ?", (user_id,)).fetchone():
        raise ValueError("author already belongs to an editor")
    conn.execute(
        """
        INSERT INTO authors (id, user_id, editor_id, organization_id, status, created_at)
        VALUES (?, ?, ?, ?, 'active', ?)
        """,
        (new_id(), user_id, row["editor_id"], row["organization_id"], utcnow()),
    )
    conn.execute("UPDATE author_invites SET accepted_at = ? WHERE id = ?", (utcnow(), row["id"]))
    conn.commit()
    return user_id


def persona_asks(conn, persona_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT ask FROM persona_passes WHERE persona_id = ? AND enabled = 1 ORDER BY position",
        (persona_id,),
    ).fetchall()
    return [r["ask"] for r in rows]


def publish_persona(conn, editor_row, persona_id: str) -> None:
    """Make a persona available. Authors are not charged for a run."""
    persona = _own_persona(conn, editor_row, persona_id)
    code = persona["agent_code"] or DEFAULT_AGENT
    validate_agent(code, _stored_skills(persona["agent_skills"]))
    conn.execute(
        """
        UPDATE personas SET price_cents = NULL, status = 'active', updated_at = ?
        WHERE id = ? AND editor_id = ?
        """,
        (utcnow(), persona_id, editor_row["id"]),
    )
    conn.commit()


def save_workflow(conn, editor_row, persona_id: str, spec: dict) -> None:
    """Replace workflow.json. The worker runs that file."""
    persona = _own_persona(conn, editor_row, persona_id)
    cleaned = parse_workflow(spec)
    root = ensure_persona_package(persona)
    merged = read_tree(root)
    merged["manifest.json"] = (
        json.dumps({"harness": "workflow", "entry": "workflow.json"}, indent=2) + "\n"
    )
    merged["workflow.json"] = json.dumps(cleaned, indent=2) + "\n"
    _commit_files(conn, persona, merged)


def create_persona(conn, editor_row, *, name: str, max_words: int) -> str:
    """Open another persona. A new persona is a workflow.json."""
    name = _persona_name(name)
    max_words = _max_words(max_words)
    files, code = starter_files()
    validate_agent(code, [])
    persona_id = new_id()
    now = utcnow()
    conn.execute(
        """
        INSERT INTO personas (
          id, editor_id, organization_id, name, instructions, agent_code, agent_skills,
          compendium_enabled, readback_enabled, max_words, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, '', ?, '[]', 1, 1, ?, 'draft', ?, ?)
        """,
        (persona_id, editor_row["id"], editor_row["organization_id"], name, code, max_words, now, now),
    )
    for ask, position, _title in PASSES:
        conn.execute(
            """
            INSERT INTO persona_passes (persona_id, editor_id, ask, position, enabled)
            VALUES (?, ?, ?, ?, 1)
            """,
            (persona_id, editor_row["id"], ask, position),
        )
    root = persona_dir(persona_id)
    backup = replace_tree(root, files)
    _commit_package(conn, root, backup)
    return persona_id


def update_persona_profile(conn, editor_row, persona_id: str, *, name: str, max_words: int) -> None:
    """Rename a persona or change its word limit. The package stays where it is."""
    persona = _own_persona(conn, editor_row, persona_id)
    name = _persona_name(name)
    max_words = _max_words(max_words)
    conn.execute(
        "UPDATE personas SET name = ?, max_words = ?, updated_at = ? WHERE id = ?",
        (name, max_words, utcnow(), persona_id),
    )
    conn.commit()


def install_persona_zip(conn, editor_row, persona_id: str, blob: bytes) -> None:
    """Replace the package with a zip. The manifest selects the harness."""
    persona = _own_persona(conn, editor_row, persona_id)
    _commit_files(conn, persona, files_from_zip(blob))


def install_persona_files(conn, editor_row, persona_id: str, files: dict[str, str]) -> None:
    """Add or replace files in the package."""
    persona = _own_persona(conn, editor_row, persona_id)
    root = ensure_persona_package(persona)
    merged = read_tree(root)
    for path, text in files.items():
        if not isinstance(text, str):
            raise ValueError(f"{path} must be text")
        merged[safe_rel(path)] = text
    _commit_files(conn, persona, merged)


def _commit_files(conn, persona, files: dict[str, str]) -> None:
    root = persona_dir(persona["id"])
    incoming = prepare_tree(root, files)
    try:
        info = compile_package(incoming)
    except Exception:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise
    backup = adopt_tree(root, incoming)
    try:
        _write_agent_columns(
            conn,
            persona,
            name=persona["name"],
            code=info["code"],
            skills=info["skills"],
            max_words=persona["max_words"],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        restore_backup(root, backup)
        raise
    discard_backup(backup)


def _commit_package(conn, root, backup) -> None:
    try:
        conn.commit()
    except Exception:
        conn.rollback()
        restore_backup(root, backup)
        raise
    discard_backup(backup)


def _write_agent_columns(conn, persona, *, name: str, code: str, skills: list[dict], max_words: int) -> None:
    calls = static_calls(code)
    compendium = any(item["skill"] == "author-compendium" or item["call"] == "compendium" for item in calls)
    readback = any(item["skill"] == "author-readback" or item["call"] == "readback" for item in calls)
    if readback and not compendium:
        raise ValueError("readback requires the compendium skill")
    named = {item["call"] for item in calls if item["call"]}
    uses_dev = any(item["skill"] == "author-dev" for item in calls)
    conn.execute(
        """
        UPDATE personas
        SET name = ?, agent_code = ?, agent_skills = ?, compendium_enabled = ?, readback_enabled = ?,
            max_words = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            name,
            code,
            json.dumps(skills),
            int(compendium),
            int(readback),
            max_words,
            utcnow(),
            persona["id"],
        ),
    )
    for ask, _position, _title in PASSES:
        enabled = ask in named or (uses_dev and not named)
        conn.execute(
            "UPDATE persona_passes SET enabled = ? WHERE persona_id = ? AND ask = ?",
            (1 if enabled else 0, persona["id"], ask),
        )


def _persona_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("a name is required")
    if len(name) > 80:
        raise ValueError("the agent name is too long")
    return name


def _max_words(max_words) -> int:
    try:
        max_words = int(max_words)
    except (TypeError, ValueError) as exc:
        raise ValueError("max words must be a number") from exc
    if max_words <= 0:
        raise ValueError("max words must be a number")
    return max_words


def _stored_skills(raw: str | None) -> list[dict]:
    from app.agent import parse_skills

    return parse_skills(raw)


def _own_persona(conn, editor_row, persona_id: str):
    row = conn.execute(
        "SELECT * FROM personas WHERE id = ? AND editor_id = ?",
        (persona_id, editor_row["id"]),
    ).fetchone()
    if row is None:
        raise ValueError("persona not found")
    return row


def _workflow_model_name(root) -> str:
    path = root / "workflow.json"
    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return parse_workflow(data).get("model") or ""
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def create_job(conn, author_row, *, persona_id: str, title: str, genre: str, audience: str, comps: str, do_not_change: str, focus: str, manuscript: str, object_key: str, digest: str) -> str:
    persona = conn.execute(
        """
        SELECT * FROM personas
        WHERE id = ? AND editor_id = ? AND organization_id = ? AND status = 'active'
        """,
        (persona_id, author_row["editor_id"], author_row["organization_id"]),
    ).fetchone()
    if persona is None:
        raise ValueError("persona is not available")
    editor = conn.execute("SELECT status FROM editors WHERE id = ?", (author_row["editor_id"],)).fetchone()
    if editor["status"] != "active":
        raise ValueError("editor is suspended")
    words = word_count(manuscript)
    if words > persona["max_words"]:
        raise ValueError("manuscript is over this persona's word limit")
    if not title or not genre or not audience:
        raise ValueError("title, genre, and audience are required")
    asks = persona_asks(conn, persona_id)
    code = persona["agent_code"] or DEFAULT_AGENT
    estimate = estimate_agent_words(words, code)
    if estimate["cost_usd_micros"] > config.JOB_DOLLAR_CAP_MICROS:
        raise ValueError("this manuscript is over the length this desk will run")
    job_id = new_id()
    package = ensure_persona_package(persona)
    named_model = _workflow_model_name(package)
    snapshot_persona(persona_id, job_id)
    conn.execute(
        """
        INSERT INTO jobs (
          id, editor_id, organization_id, author_id, persona_id, price_cents,
          instructions_snapshot, agent_code_snapshot, agent_skills_snapshot,
          compendium_enabled_snapshot, readback_enabled_snapshot,
          max_words_snapshot, status, title, slug, genre, audience, comps, do_not_change,
          focus, manuscript_object_key, manuscript_bytes, manuscript_sha256, word_count,
          model_id, created_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            job_id,
            author_row["editor_id"],
            author_row["organization_id"],
            author_row["id"],
            persona_id,
            0,
            persona["instructions"],
            code,
            persona["agent_skills"] or "[]",
            persona["compendium_enabled"],
            persona["readback_enabled"],
            persona["max_words"],
            title,
            slugify(title),
            genre,
            audience,
            comps,
            do_not_change,
            focus,
            object_key,
            len(manuscript.encode()),
            digest,
            words,
            named_model or llm.model_id(),
            utcnow(),
        ),
    )
    copy_job_snapshot(conn, job_id, author_row["editor_id"], asks)
    conn.commit()
    if config.DEMO_MODE:
        finish_demo(conn, job_id)
        conn.commit()
        notify_author(conn, job_id)
    return job_id


def copy_job_snapshot(conn, job_id: str, editor_id: str, asks: list[str]) -> None:
    """The author session cannot insert job_passes. This function is the only writer."""
    for ask, position, _title in PASSES:
        conn.execute(
            """
            INSERT INTO job_passes (job_id, editor_id, ask, position, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (job_id, editor_id, ask, position, 1 if ask in asks else 0),
        )


def dev_capture(conn, job_id: str, author_id: str) -> str:
    """No-charge compatibility for a job that used to wait for payment.

    Does not insert a charge. A ready job stays ready. A queued job stays
    queued. A legacy awaiting_payment job is queued.
    """
    job = conn.execute(
        "SELECT status FROM jobs WHERE id = ? AND author_id = ?",
        (job_id, author_id),
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "ready":
        return "ready"
    if job["status"] == "queued":
        return "queued"
    if job["status"] != "awaiting_payment":
        raise ValueError("job is not queued")
    conn.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (job_id,))
    conn.commit()
    return "queued"


def finish_demo(conn, job_id: str) -> None:
    """Close a queued job without the editing workflow.

    No skill prompt is built and no model is called. Model cost stays zero.
    Nothing is owed to the practice.
    """
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "ready":
        return
    if job["status"] not in ("queued", "running"):
        raise ValueError("job is not queued")
    existing = conn.execute(
        "SELECT id FROM artifacts WHERE job_id = ? AND kind = 'letter'",
        (job_id,),
    ).fetchone()
    if existing is None:
        letter = (
            f"# Demo\n\n"
            f"{job['title']} is stored on this proof of concept. "
            "The editing workflow did not run. No model was called, and this is not a developmental letter.\n\n"
            "The manuscript is stored. Messages and a request for a human edit still work.\n"
        )
        key = (
            f"artifacts/{job['organization_id']}/{job['editor_id']}/"
            f"{job['author_id']}/{job_id}/letter.md"
        )
        write_object(key, letter)
        conn.execute(
            """
            INSERT INTO artifacts (id, job_id, editor_id, organization_id, kind, object_key, created_at)
            VALUES (?, ?, ?, ?, 'letter', ?, ?)
            """,
            (new_id(), job_id, job["editor_id"], job["organization_id"], key, utcnow()),
        )
    conn.execute(
        """
        UPDATE jobs
        SET status = 'ready', finished_at = ?, cost_usd_micros = 0, tokens_used = 0
        WHERE id = ?
        """,
        (utcnow(), job_id),
    )


def _like(value: str) -> str:
    cleaned = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{cleaned}%"


def search_manuscripts(
    conn,
    editor_row,
    *,
    q: str = "",
    genre: str = "",
    audience: str = "",
    comps_contains: str = "",
    word_count_min: int | None = None,
    word_count_max: int | None = None,
    human_requested: int | None = None,
):
    """Find manuscripts an editor may want to take on by hand.

    Reads the columns the author already supplied. Does not read the manuscript
    file, call the model, or change the automated run.
    """
    if not allowed(editor_row, "pipeline"):
        raise ValueError("that is not assigned to this login")
    if word_count_min is not None and word_count_min < 0:
        raise ValueError("word count cannot be negative")
    if word_count_max is not None and word_count_max < 0:
        raise ValueError("word count cannot be negative")
    if (
        word_count_min is not None
        and word_count_max is not None
        and word_count_min > word_count_max
    ):
        raise ValueError("word count range is backwards")
    if human_requested not in (None, 0, 1):
        raise ValueError("human request filter is invalid")
    clauses = ["j.organization_id = ?"]
    params: list = [editor_row["organization_id"]]
    text = (q or "").strip()
    if text:
        like = _like(text)
        clauses.append(
            """(
              lower(j.title) LIKE ? ESCAPE '\\'
              OR lower(u.email) LIKE ? ESCAPE '\\'
              OR lower(COALESCE(j.genre, '')) LIKE ? ESCAPE '\\'
              OR lower(COALESCE(j.audience, '')) LIKE ? ESCAPE '\\'
              OR lower(COALESCE(j.comps, '')) LIKE ? ESCAPE '\\'
            )"""
        )
        params.extend([like, like, like, like, like])
    genre = (genre or "").strip()
    if genre:
        clauses.append("lower(COALESCE(j.genre, '')) = ?")
        params.append(genre.lower())
    audience = (audience or "").strip()
    if audience:
        clauses.append("lower(COALESCE(j.audience, '')) = ?")
        params.append(audience.lower())
    comps = (comps_contains or "").strip()
    if comps:
        clauses.append("lower(COALESCE(j.comps, '')) LIKE ? ESCAPE '\\'")
        params.append(_like(comps))
    if word_count_min is not None:
        clauses.append("COALESCE(j.word_count, 0) >= ?")
        params.append(word_count_min)
    if word_count_max is not None:
        clauses.append("COALESCE(j.word_count, 0) <= ?")
        params.append(word_count_max)
    if human_requested is not None:
        clauses.append("j.human_requested = ?")
        params.append(human_requested)
    where = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT j.id, j.title, j.genre, j.audience, j.comps, j.word_count,
               j.human_requested, j.status, u.email AS author_email
        FROM jobs j
        JOIN authors a ON a.id = j.author_id
        JOIN users u ON u.id = a.user_id
        WHERE {where}
        ORDER BY j.human_requested DESC, j.created_at DESC
        """,
        params,
    ).fetchall()


def request_human(conn, author_id: str, job_id: str) -> None:
    job = conn.execute(
        "SELECT * FROM jobs WHERE id = ? AND author_id = ?",
        (job_id, author_id),
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    conn.execute("UPDATE jobs SET human_requested = 1 WHERE id = ?", (job_id,))
    conn.commit()


def post_message(conn, *, job_id: str, sender_role: str, sender_id: str, body: str, actor_editor=None) -> None:
    body = body.strip()
    if not body:
        raise ValueError("empty message")
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("job not found")
    if sender_role == "author" and sender_id != job["author_id"]:
        raise ValueError("not your job")
    if sender_role == "editor":
        if actor_editor is None:
            if sender_id != job["editor_id"]:
                raise ValueError("not your job")
        elif not allowed(actor_editor, "messages") or actor_editor["organization_id"] != job["organization_id"]:
            raise ValueError("that is not assigned to this login")
    conn.execute(
        """
        INSERT INTO messages (
          id, job_id, editor_id, author_id, organization_id, sender_role, body, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id(), job_id, job["editor_id"], job["author_id"], job["organization_id"], sender_role, body, utcnow()),
    )
    conn.commit()


def jobs_visible(conn, editor_row):
    if not allowed(editor_row, "manuscripts"):
        return []
    where = "j.organization_id = ?"
    key = editor_row["organization_id"]
    return conn.execute(
        f"""
        SELECT j.*, u.email AS author_email, eu.email AS editor_email
        FROM jobs j
        JOIN authors a ON a.id = j.author_id
        JOIN users u ON u.id = a.user_id
        JOIN editors e ON e.id = j.editor_id
        JOIN users eu ON eu.id = e.user_id
        WHERE {where}
        ORDER BY j.created_at DESC
        """,
        (key,),
    ).fetchall()


def practice_model_cost_micros(conn, organization_id: str) -> int:
    """Model spend recorded for this practice. An operating expense, not an author charge."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd_micros), 0) AS micros FROM token_costs WHERE organization_id = ?",
        (organization_id,),
    ).fetchone()
    return int(row["micros"])


def enable_payouts(conn, editor_row) -> None:
    if not allowed(editor_row, "payouts"):
        raise ValueError("that is not assigned to this login")
    conn.execute(
        "UPDATE organizations SET payouts_enabled = 1, stripe_country = 'US' WHERE id = ?",
        (editor_row["organization_id"],),
    )
    conn.commit()


def record_payout(conn, organization_id: str) -> tuple[int, int]:
    from app.money import split_payout

    balance = org_balance_cents(conn, organization_id)
    transfer, withheld = split_payout(balance, config.PAYOUT_MINIMUM_CENTS)
    if transfer <= 0:
        return 0, 0
    # The split can leave one cent. Keep it in the withhold so the payable
    # rows assigned to this payout sum to the transfer plus the withhold.
    residue = balance - transfer - withheld
    if residue < 0:
        raise ValueError("payout split exceeded the balance")
    withheld += residue
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (organization_id,)).fetchone()
    if org["payouts_paused"] or not org["payouts_enabled"]:
        raise ValueError("payouts are not enabled for this practice")
    payout_id = new_id()
    month = utcnow()[:7]
    attempt = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) + 1 AS n FROM payouts WHERE organization_id = ? AND period_month = ?",
        (organization_id, month),
    ).fetchone()["n"]
    conn.execute(
        """
        INSERT INTO payouts (
          id, organization_id, period_month, attempt, amount_cents, withheld_connect_cents,
          status, idempotency_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (payout_id, organization_id, month, attempt, transfer, withheld, f"{organization_id}:{month}:{attempt}", utcnow()),
    )
    conn.execute(
        """
        UPDATE editor_payables SET payout_id = ?
        WHERE organization_id = ? AND payout_id IS NULL
        """,
        (payout_id, organization_id),
    )
    conn.commit()
    return transfer, withheld


def org_balance_cents(conn, organization_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_cents), 0) AS bal
        FROM editor_payables
        WHERE organization_id = ? AND payout_id IS NULL
        """,
        (organization_id,),
    ).fetchone()
    return int(row["bal"])


def write_manuscript(key: str, text: str) -> None:
    path = config.OBJECT_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def read_object(key: str) -> str:
    return (config.OBJECT_DIR / key).read_text()


def write_object(key: str, text: str) -> None:
    path = config.OBJECT_DIR / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
