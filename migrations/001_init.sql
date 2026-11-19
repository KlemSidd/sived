-- SQLite schema. The desk opens this file. It does not open another database.
-- Unused columns and tables stay so an existing file still opens.

PRAGMA foreign_keys = ON;

CREATE TABLE users (
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
);

-- id is the SHA-256 of the cookie value. The cookie holds the raw id.
CREATE TABLE sessions (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  csrf_token text NOT NULL,
  created_at text NOT NULL,
  expires_at text NOT NULL
);

CREATE TABLE organizations (
  id text PRIMARY KEY,
  name text NOT NULL,
  -- Unused by the desk. Kept so an existing local file still opens.
  plan text NOT NULL CHECK (plan IN ('indie', 'firm')),
  stripe_account_id text,
  stripe_country text,
  payouts_paused integer NOT NULL DEFAULT 0,
  payouts_enabled integer NOT NULL DEFAULT 0,
  theme_profile text NOT NULL DEFAULT 'desk',
  theme_mode text NOT NULL DEFAULT 'light',
  theme_accent text NOT NULL DEFAULT '',
  theme_paper text NOT NULL DEFAULT '',
  theme_ink text NOT NULL DEFAULT '',
  created_at text NOT NULL
);

CREATE TABLE editors (
  id text PRIMARY KEY,
  user_id text NOT NULL UNIQUE REFERENCES users(id),
  organization_id text NOT NULL REFERENCES organizations(id),
  is_org_admin integer NOT NULL DEFAULT 0,
  perm_manuscripts integer NOT NULL DEFAULT 0,
  perm_messages integer NOT NULL DEFAULT 0,
  perm_pipeline integer NOT NULL DEFAULT 0,
  perm_offers integer NOT NULL DEFAULT 0,
  perm_payouts integer NOT NULL DEFAULT 0,
  perm_assistants integer NOT NULL DEFAULT 0,
  patreon_campaign_id text NOT NULL DEFAULT '',
  patreon_client_id text NOT NULL DEFAULT '',
  patreon_client_secret text NOT NULL DEFAULT '',
  patreon_webhook_secret text NOT NULL DEFAULT '',
  status text NOT NULL CHECK (status IN ('active', 'suspended', 'removed')),
  created_at text NOT NULL
);

CREATE TABLE editor_invites (
  id text PRIMARY KEY,
  email text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  invited_by text NOT NULL REFERENCES users(id),
  organization_id text REFERENCES organizations(id),
  kind text NOT NULL DEFAULT 'editor' CHECK (kind IN ('editor', 'assistant', 'admin')),
  perm_manuscripts integer NOT NULL DEFAULT 0,
  perm_messages integer NOT NULL DEFAULT 0,
  perm_pipeline integer NOT NULL DEFAULT 0,
  perm_offers integer NOT NULL DEFAULT 0,
  perm_payouts integer NOT NULL DEFAULT 0,
  perm_assistants integer NOT NULL DEFAULT 0,
  expires_at text NOT NULL,
  accepted_at text,
  revoked_at text,
  created_at text NOT NULL
);

CREATE TABLE authors (
  id text PRIMARY KEY,
  user_id text NOT NULL UNIQUE REFERENCES users(id),
  editor_id text NOT NULL REFERENCES editors(id),
  organization_id text NOT NULL REFERENCES organizations(id),
  patreon_user_id text NOT NULL DEFAULT '',
  patron_status text NOT NULL DEFAULT '',
  membership_checked_at text NOT NULL DEFAULT '',
  patreon_refresh_token text NOT NULL DEFAULT '',
  status text NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at text NOT NULL
);

-- state is the SHA-256 of the redirect value. The redirect carries the raw state.
CREATE TABLE patreon_oauth_states (
  state text PRIMARY KEY,
  editor_id text NOT NULL,
  created_at text NOT NULL,
  expires_at text NOT NULL
);

CREATE TABLE author_invites (
  id text PRIMARY KEY,
  editor_id text NOT NULL REFERENCES editors(id),
  organization_id text NOT NULL REFERENCES organizations(id),
  email text NOT NULL,
  token_hash text NOT NULL UNIQUE,
  invited_by text NOT NULL REFERENCES users(id),
  expires_at text NOT NULL,
  accepted_at text,
  revoked_at text,
  created_at text NOT NULL
);

CREATE TABLE personas (
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
);

CREATE TABLE persona_passes (
  persona_id text NOT NULL REFERENCES personas(id),
  editor_id text NOT NULL,
  ask text NOT NULL CHECK (ask IN ('promise', 'plot', 'character', 'stakes')),
  position integer NOT NULL CHECK (position BETWEEN 1 AND 4),
  enabled integer NOT NULL DEFAULT 1,
  PRIMARY KEY (persona_id, ask),
  UNIQUE (persona_id, position)
);

CREATE TABLE jobs (
  id text PRIMARY KEY,
  editor_id text NOT NULL REFERENCES editors(id),
  organization_id text NOT NULL REFERENCES organizations(id),
  author_id text NOT NULL REFERENCES authors(id),
  persona_id text NOT NULL REFERENCES personas(id),
  price_cents integer NOT NULL,
  instructions_snapshot text NOT NULL,
  agent_code_snapshot text NOT NULL DEFAULT '',
  agent_skills_snapshot text NOT NULL DEFAULT '',
  compendium_enabled_snapshot integer NOT NULL,
  readback_enabled_snapshot integer NOT NULL,
  max_words_snapshot integer NOT NULL,
  status text NOT NULL CHECK (status IN (
    'awaiting_payment', 'queued', 'running', 'batch_pending',
    'needs_user', 'ready', 'failed', 'cancelled'
  )),
  title text NOT NULL,
  slug text NOT NULL,
  genre text NOT NULL,
  audience text NOT NULL,
  comps text,
  do_not_change text,
  focus text,
  human_requested integer NOT NULL DEFAULT 0,
  dev_answer text,
  blocking_ask text,
  blocking_question text,
  manuscript_object_key text,
  manuscript_bytes integer,
  manuscript_sha256 text,
  word_count integer,
  stripe_fee_cents integer,
  tokens_used integer NOT NULL DEFAULT 0,
  cost_usd_micros integer NOT NULL DEFAULT 0,
  cost_cap_usd_micros integer NOT NULL DEFAULT 8000000,
  token_cap integer NOT NULL DEFAULT 4000000,
  model_id text NOT NULL,
  lease_owner text,
  lease_expires_at text,
  error_code text,
  created_at text NOT NULL,
  paid_at text,
  started_at text,
  finished_at text
);

CREATE TABLE job_passes (
  job_id text NOT NULL REFERENCES jobs(id),
  editor_id text NOT NULL,
  ask text NOT NULL CHECK (ask IN ('promise', 'plot', 'character', 'stakes')),
  position integer NOT NULL CHECK (position BETWEEN 1 AND 4),
  enabled integer NOT NULL,
  PRIMARY KEY (job_id, ask)
);

CREATE TABLE triggers (
  id text PRIMARY KEY,
  editor_id text NOT NULL REFERENCES editors(id),
  organization_id text NOT NULL,
  name text NOT NULL,
  genre text,
  audience text,
  word_count_min integer,
  word_count_max integer,
  comps_contains text,
  persona_id text,
  human_requested integer,
  run_status text,
  created_at text NOT NULL
);

CREATE TABLE pipeline_items (
  id text PRIMARY KEY,
  editor_id text NOT NULL,
  organization_id text NOT NULL,
  job_id text NOT NULL REFERENCES jobs(id),
  trigger_id text NOT NULL REFERENCES triggers(id),
  status text NOT NULL CHECK (status IN ('new', 'reviewing', 'contacted', 'declined', 'engaged')),
  created_at text NOT NULL,
  UNIQUE (job_id, trigger_id)
);

CREATE TABLE messages (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id),
  editor_id text NOT NULL,
  author_id text NOT NULL,
  organization_id text NOT NULL,
  sender_role text NOT NULL CHECK (sender_role IN ('editor', 'author')),
  body text NOT NULL,
  created_at text NOT NULL
);

CREATE TABLE charges (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id),
  editor_id text NOT NULL,
  organization_id text NOT NULL,
  amount_cents integer NOT NULL,
  stripe_fee_cents integer,
  status text NOT NULL CHECK (status IN ('pending', 'captured', 'refunded')),
  stripe_session_id text,
  created_at text NOT NULL
);

CREATE UNIQUE INDEX charges_one_open_per_job
  ON charges(job_id) WHERE status IN ('pending', 'captured');

CREATE TABLE token_costs (
  id text PRIMARY KEY,
  job_id text NOT NULL REFERENCES jobs(id),
  editor_id text NOT NULL,
  organization_id text NOT NULL,
  call_name text NOT NULL,
  cost_usd_micros integer NOT NULL,
  input_tokens integer NOT NULL,
  output_tokens integer NOT NULL,
  created_at text NOT NULL
);

CREATE TABLE editor_payables (
  id text PRIMARY KEY,
  organization_id text NOT NULL REFERENCES organizations(id),
  editor_id text NOT NULL,
  job_id text,
  kind text NOT NULL CHECK (kind IN ('run', 'refund', 'connect_trueup')),
  amount_cents integer NOT NULL,
  payout_id text,
  created_at text NOT NULL,
  CHECK (kind <> 'run' OR amount_cents > 0)
);

CREATE TABLE payouts (
  id text PRIMARY KEY,
  organization_id text NOT NULL REFERENCES organizations(id),
  period_month text NOT NULL,
  attempt integer NOT NULL DEFAULT 1,
  amount_cents integer NOT NULL,
  withheld_connect_cents integer NOT NULL,
  status text NOT NULL CHECK (status IN ('pending', 'paid', 'failed')),
  idempotency_key text NOT NULL UNIQUE,
  created_at text NOT NULL,
  UNIQUE (organization_id, period_month, attempt)
);

CREATE TABLE artifacts (
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
);

CREATE TABLE provider_calls (
  id text PRIMARY KEY,
  job_id text NOT NULL,
  call_name text NOT NULL,
  prompt_sha256 text NOT NULL,
  input_tokens integer,
  output_tokens integer,
  status text NOT NULL,
  created_at text NOT NULL
);

CREATE TABLE spend_guard (
  day text PRIMARY KEY,
  spent_usd_micros integer NOT NULL DEFAULT 0
);

CREATE TABLE admin_audit (
  id text PRIMARY KEY,
  actor_user_id text NOT NULL,
  action text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  reason text,
  created_at text NOT NULL
);

CREATE TABLE agent_tokens (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES users(id),
  token_hash text NOT NULL UNIQUE,
  label text NOT NULL DEFAULT '',
  created_at text NOT NULL,
  revoked_at text
);

CREATE TABLE mail_accounts (
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
);

CREATE UNIQUE INDEX mail_invitations_user
  ON mail_accounts(user_id) WHERE purpose = 'invitations';

CREATE UNIQUE INDEX mail_notices_org
  ON mail_accounts(organization_id) WHERE purpose = 'author_notices';

CREATE TABLE inference_providers (
  id text PRIMARY KEY,
  organization_id text NOT NULL,
  provider text NOT NULL,
  label text NOT NULL,
  base_url text NOT NULL DEFAULT '',
  secret text NOT NULL DEFAULT '',
  models_json text NOT NULL DEFAULT '[]',
  checked_at text NOT NULL DEFAULT '',
  created_at text NOT NULL
);

CREATE UNIQUE INDEX inference_providers_org_label
  ON inference_providers(organization_id, label);

CREATE TABLE inference_defaults (
  id text PRIMARY KEY,
  organization_id text NOT NULL,
  model text NOT NULL,
  position integer NOT NULL,
  created_at text NOT NULL
);

CREATE UNIQUE INDEX inference_defaults_org_model
  ON inference_defaults(organization_id, model);

CREATE UNIQUE INDEX inference_defaults_org_position
  ON inference_defaults(organization_id, position);
