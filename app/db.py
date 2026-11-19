import sqlite3
from datetime import datetime, timezone

from app import config


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path=None) -> sqlite3.Connection:
    target = path or config.DATABASE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    script = config.SCHEMA_PATH.read_text()
    conn.executescript(script)
    conn.commit()


# Added after the first local databases were created. SQLite cannot rerun
# CREATE TABLE, so an existing file gets the columns here.
_PERMISSION_COLUMNS = (
    "perm_manuscripts",
    "perm_messages",
    "perm_pipeline",
    "perm_offers",
    "perm_payouts",
    "perm_assistants",
)


def _ensure_permission_columns(conn: sqlite3.Connection) -> None:
    for table in ("editors", "editor_invites"):
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if present is None:
            continue
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in _PERMISSION_COLUMNS:
            if column not in have:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} integer NOT NULL DEFAULT 0"
                )
    conn.commit()


def _ensure_agent_columns(conn: sqlite3.Connection) -> None:
    specs = {
        "personas": (
            ("agent_code", "text NOT NULL DEFAULT ''"),
            ("agent_skills", "text NOT NULL DEFAULT ''"),
        ),
        "jobs": (
            ("agent_code_snapshot", "text NOT NULL DEFAULT ''"),
            ("agent_skills_snapshot", "text NOT NULL DEFAULT ''"),
        ),
    }
    for table, columns in specs.items():
        present = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if present is None:
            continue
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'personas'"
    ).fetchone()
    if present is not None:
        from app.agent import DEFAULT_AGENT

        conn.execute(
            "UPDATE personas SET agent_code = ? WHERE agent_code = ''",
            (DEFAULT_AGENT,),
        )
    conn.commit()
    _ensure_artifact_kinds(conn)


def _ensure_artifact_kinds(conn: sqlite3.Connection) -> None:
    """Let an agent store the documents it returns, not only the original three."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'artifacts'"
    ).fetchone()
    if row is None or not row[0] or "kind IN ('letter', 'compendium', 'readback')" not in row[0]:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE artifacts_new (
              id text PRIMARY KEY,
              job_id text NOT NULL REFERENCES jobs(id),
              editor_id text NOT NULL,
              organization_id text NOT NULL,
              kind text NOT NULL CHECK (
                length(kind) <= 41
                AND kind GLOB '[a-z]*'
                AND kind NOT GLOB '*[^a-z0-9-]*'
              ),
              object_key text NOT NULL,
              created_at text NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO artifacts_new (id, job_id, editor_id, organization_id, kind, object_key, created_at)
            SELECT id, job_id, editor_id, organization_id, kind, object_key, created_at FROM artifacts
            """
        )
        conn.execute("DROP TABLE artifacts")
        conn.execute("ALTER TABLE artifacts_new RENAME TO artifacts")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def open_db(path=None) -> sqlite3.Connection:
    conn = connect(path)
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if existing is None:
        migrate(conn)
    _ensure_permission_columns(conn)
    _ensure_agent_columns(conn)
    _ensure_persona_membership(conn)
    _ensure_agent_tokens(conn)
    _ensure_user_roles(conn)
    _ensure_invite_kind(conn)
    _ensure_patreon(conn)
    _ensure_theme(conn)
    _ensure_mail(conn)
    _ensure_inference(conn)
    _ensure_session_hashes(conn)
    return conn


def _is_token_hash(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _ensure_session_hashes(conn: sqlite3.Connection) -> None:
    """Store session ids and Patreon login states as SHA-256 hashes.

    An existing session id is hashed in place, so a browser that already has
    the cookie stays signed in. A Patreon login state lasts 10 minutes, and
    an old raw row is deleted.
    """
    from app.services import token_hash

    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
    ).fetchone()
    if present is not None:
        raw_ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM sessions")
            if not _is_token_hash(row["id"])
        ]
        for sid in raw_ids:
            conn.execute("UPDATE sessions SET id = ? WHERE id = ?", (token_hash(sid), sid))
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'patreon_oauth_states'"
    ).fetchone()
    if present is not None:
        raw_states = [
            row["state"]
            for row in conn.execute("SELECT state FROM patreon_oauth_states")
            if not _is_token_hash(row["state"])
        ]
        for state in raw_states:
            conn.execute("DELETE FROM patreon_oauth_states WHERE state = ?", (state,))
    conn.commit()


def _ensure_persona_membership(conn: sqlite3.Connection) -> None:
    """An active persona does not need an author price. Access is membership."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'personas'"
    ).fetchone()
    if row is None or not row[0]:
        return
    sql = row[0]
    if "price_cents IS NOT NULL" not in sql and "price_cents > 0" not in sql:
        return
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE personas_new (
              id text PRIMARY KEY,
              editor_id text NOT NULL REFERENCES editors(id),
              organization_id text NOT NULL REFERENCES organizations(id),
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
              CHECK (max_words > 0)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO personas_new (
              id, editor_id, organization_id, name, instructions, agent_code, agent_skills,
              compendium_enabled, readback_enabled, price_cents, max_words, status,
              created_at, updated_at
            )
            SELECT
              id, editor_id, organization_id, name, instructions, agent_code, agent_skills,
              compendium_enabled, readback_enabled, NULL, max_words, status,
              created_at, updated_at
            FROM personas
            """
        )
        conn.execute("DROP TABLE personas")
        conn.execute("ALTER TABLE personas_new RENAME TO personas")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _ensure_agent_tokens(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_tokens (
          id text PRIMARY KEY,
          user_id text NOT NULL REFERENCES users(id),
          token_hash text NOT NULL UNIQUE,
          label text NOT NULL DEFAULT '',
          created_at text NOT NULL,
          revoked_at text
        )
        """
    )
    conn.commit()


def _ensure_user_roles(conn: sqlite3.Connection) -> None:
    """One email can be the owner and an editor. An admin is a separate flag.

    The owner flag cannot be cleared by a later login. Contact fields live on
    the same row so every account has a profile.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if row is None or not row[0]:
        return
    have = {info[1] for info in conn.execute("PRAGMA table_info(users)")}
    # Older databases used a users table that is not an account table.
    # Leave those alone. Persona and artifact migrations still run.
    identity = {
        "email",
        "name",
        "role",
        "auth_provider",
        "auth_subject",
        "created_at",
        "deleted_at",
    }
    if not identity.issubset(have):
        return
    if "'admin'" not in row[0]:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                CREATE TABLE users_new (
                  id text PRIMARY KEY,
                  email text NOT NULL,
                  name text,
                  role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'author')),
                  is_owner integer NOT NULL DEFAULT 0,
                  is_admin integer NOT NULL DEFAULT 0,
                  contact_email text NOT NULL DEFAULT '',
                  phone text NOT NULL DEFAULT '',
                  auth_provider text NOT NULL CHECK (auth_provider IN ('google', 'dev', 'patreon')),
                  auth_subject text NOT NULL,
                  created_at text NOT NULL,
                  deleted_at text,
                  UNIQUE (auth_provider, auth_subject)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO users_new (
                  id, email, name, role, is_owner, is_admin, contact_email, phone,
                  auth_provider, auth_subject, created_at, deleted_at
                )
                SELECT
                  id, email, name, role,
                  CASE WHEN role = 'owner' THEN 1 ELSE 0 END,
                  0, '', '',
                  auth_provider, auth_subject, created_at, deleted_at
                FROM users
                """
            )
            conn.execute("DROP TABLE users")
            conn.execute("ALTER TABLE users_new RENAME TO users")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        have = {info[1] for info in conn.execute("PRAGMA table_info(users)")}
    added = (
        ("is_owner", "integer NOT NULL DEFAULT 0"),
        ("is_admin", "integer NOT NULL DEFAULT 0"),
        ("contact_email", "text NOT NULL DEFAULT ''"),
        ("phone", "text NOT NULL DEFAULT ''"),
    )
    for column, ddl in added:
        if column not in have:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
    conn.execute("UPDATE users SET is_owner = 1 WHERE role = 'owner' AND is_owner = 0")
    conn.commit()


def _ensure_invite_kind(conn: sqlite3.Connection) -> None:
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'editor_invites'"
    ).fetchone()
    if present is None:
        return
    have = {info[1] for info in conn.execute("PRAGMA table_info(editor_invites)")}
    if "kind" not in have:
        conn.execute(
            "ALTER TABLE editor_invites ADD COLUMN kind text NOT NULL DEFAULT 'editor'"
        )
    conn.execute(
        """
        UPDATE editor_invites
        SET kind = 'assistant'
        WHERE organization_id IS NOT NULL AND kind = 'editor'
        """
    )
    conn.commit()


def _ensure_patreon(conn: sqlite3.Connection) -> None:
    """Author access is an active membership of the editor's Patreon campaign."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if row is not None and row[0] and "'patreon'" not in row[0]:
        have = {info[1] for info in conn.execute("PRAGMA table_info(users)")}
        identity = {
            "email",
            "name",
            "role",
            "auth_provider",
            "auth_subject",
            "created_at",
            "deleted_at",
        }
        if identity.issubset(have):
            _rebuild_users_for_patreon(conn, have)
    _add_columns(
        conn,
        "editors",
        (
            ("patreon_campaign_id", "text NOT NULL DEFAULT ''"),
            ("patreon_client_id", "text NOT NULL DEFAULT ''"),
            ("patreon_client_secret", "text NOT NULL DEFAULT ''"),
            ("patreon_webhook_secret", "text NOT NULL DEFAULT ''"),
        ),
    )
    _add_columns(
        conn,
        "authors",
        (
            ("patreon_user_id", "text NOT NULL DEFAULT ''"),
            ("patron_status", "text NOT NULL DEFAULT ''"),
            ("membership_checked_at", "text NOT NULL DEFAULT ''"),
            ("patreon_refresh_token", "text NOT NULL DEFAULT ''"),
        ),
    )
    if conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'editors'"
    ).fetchone():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS patreon_oauth_states (
              state text PRIMARY KEY,
              editor_id text NOT NULL,
              created_at text NOT NULL,
              expires_at text NOT NULL
            )
            """
        )
        conn.commit()


def _ensure_mail(conn: sqlite3.Connection) -> None:
    """Owner invitations and each editor's author notices. Existing databases gain the table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mail_accounts (
          id text PRIMARY KEY,
          purpose text NOT NULL CHECK (purpose IN ('invitations', 'author_notices')),
          user_id text,
          organization_id text,
          transport text NOT NULL CHECK (transport IN ('smtp', 'https')),
          from_address text NOT NULL,
          smtp_host text NOT NULL DEFAULT '',
          smtp_port integer NOT NULL DEFAULT 0,
          smtp_security text NOT NULL DEFAULT '' CHECK (smtp_security IN ('', 'starttls', 'tls')),
          smtp_username text NOT NULL DEFAULT '',
          secret text NOT NULL DEFAULT '',
          https_provider text NOT NULL DEFAULT '' CHECK (
            https_provider IN ('', 'resend', 'postmark', 'sendgrid', 'mailgun')
          ),
          mailgun_domain text NOT NULL DEFAULT '',
          mailgun_region text NOT NULL DEFAULT '' CHECK (mailgun_region IN ('', 'us', 'eu')),
          notice_origin text NOT NULL DEFAULT '',
          created_at text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS mail_invitations_user
        ON mail_accounts(user_id) WHERE purpose = 'invitations'
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS mail_notices_org
        ON mail_accounts(organization_id) WHERE purpose = 'author_notices'
        """
    )
    conn.commit()


def _ensure_inference(conn: sqlite3.Connection) -> None:
    """Providers a practice has connected. Existing databases gain the table."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inference_providers (
          id text PRIMARY KEY,
          organization_id text NOT NULL,
          provider text NOT NULL,
          label text NOT NULL,
          base_url text NOT NULL DEFAULT '',
          secret text NOT NULL DEFAULT '',
          models_json text NOT NULL DEFAULT '[]',
          checked_at text NOT NULL DEFAULT '',
          created_at text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS inference_providers_org_label
        ON inference_providers(organization_id, label)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inference_defaults (
          id text PRIMARY KEY,
          organization_id text NOT NULL,
          model text NOT NULL,
          position integer NOT NULL,
          created_at text NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS inference_defaults_org_model
        ON inference_defaults(organization_id, model)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS inference_defaults_org_position
        ON inference_defaults(organization_id, position)
        """
    )
    conn.commit()


def _ensure_theme(conn: sqlite3.Connection) -> None:
    """Each practice stores its own colors. Existing databases gain the columns here."""
    _add_columns(
        conn,
        "organizations",
        (
            ("theme_profile", "text NOT NULL DEFAULT 'desk'"),
            ("theme_mode", "text NOT NULL DEFAULT 'light'"),
            ("theme_accent", "text NOT NULL DEFAULT ''"),
            ("theme_paper", "text NOT NULL DEFAULT ''"),
            ("theme_ink", "text NOT NULL DEFAULT ''"),
        ),
    )


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple) -> None:
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if present is None:
        return
    have = {info[1] for info in conn.execute(f"PRAGMA table_info({table})")}
    for column, ddl in columns:
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()


def _rebuild_users_for_patreon(conn: sqlite3.Connection, have: set) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(
            """
            CREATE TABLE users_new (
              id text PRIMARY KEY,
              email text NOT NULL,
              name text,
              role text NOT NULL CHECK (role IN ('owner', 'admin', 'editor', 'author')),
              is_owner integer NOT NULL DEFAULT 0,
              is_admin integer NOT NULL DEFAULT 0,
              contact_email text NOT NULL DEFAULT '',
              phone text NOT NULL DEFAULT '',
              auth_provider text NOT NULL CHECK (auth_provider IN ('google', 'dev', 'patreon')),
              auth_subject text NOT NULL,
              created_at text NOT NULL,
              deleted_at text,
              UNIQUE (auth_provider, auth_subject)
            )
            """
        )
        optional = {
            "is_owner": "0",
            "is_admin": "0",
            "contact_email": "''",
            "phone": "''",
        }
        select = ["id", "email", "name", "role"]
        for column, literal in optional.items():
            select.append(column if column in have else literal)
        select.extend(["auth_provider", "auth_subject", "created_at", "deleted_at"])
        conn.execute(
            f"""
            INSERT INTO users_new (
              id, email, name, role, is_owner, is_admin, contact_email, phone,
              auth_provider, auth_subject, created_at, deleted_at
            )
            SELECT {", ".join(select)} FROM users
            """
        )
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
