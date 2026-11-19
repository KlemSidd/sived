# SIVED

SIVED is the Super Intelligence Virtual Editting Desk, from KlemSidd LLC.

An editor stands up a practice and works with their own authors. Assistants sign in to that same desk. The editor chooses what each assistant can do. Remove an assistant and the authors and manuscripts stay where they are. There is no upgrade and no second product.

The platform owner invites each editor. An author finds that editor's page and becomes their customer. Authors are customers of an editor, not of the platform. Author access is that editor's Patreon membership. A manuscript run is included. This desk does not charge the author. Model cost is an operating expense the editor and the owner can see. A human edit is arranged in the message thread, off this desk. This desk does not invoice it.

This build has two ways to run. Leave `DEMO_MODE` unset when you want the editing workflow. Set `DEMO_MODE=1` for a proof of concept that stores manuscripts and never calls a model.

## Guides

- [Authors](docs/authors.md) upload a manuscript and ask the editor for a human edit. Access is the editor's Patreon membership.
- [Editors](docs/editors.md) publish the agent, read manuscripts, and add assistants to the same desk.
- [Administrators](docs/administrators.md) are the owner and any admin. **System Admin** invites editors and admins. An admin cannot change the owner.
- [workflow.json](docs/workflow.md) is the persona file. One step is one model call. The desk serves the same page at `/docs/workflow`.

## Deploy

This is the checklist. An agent asked what to install, what to set, or what is still missing should follow this section. When the desk is already running, the practice editor's local agent calls `deployment`. That tool reports this process. It does not return a key or a secret.

Earlier designs sit in `local-docs/` on the machine that keeps them. They are not in this repository. Do not deploy from them. This build does not use Postgres, Stripe, Caddy, or object storage. Mail is optional and is not configured from those files.

### What has to be on the machine

- Python 3.15. The desk uses the standard library only. Do not install packages.
- `openssl` on `PATH` when the desk requests a Let's Encrypt certificate.
- `pdftotext` if authors will upload PDF. Without it, a PDF the built-in reader cannot parse is refused.
- LibreOffice `soffice` if authors will upload legacy `.doc`. Without it, the desk asks for `.docx`.

The desk is two commands from this directory:

```bash
python -m app web
python -m app work
```

`web` serves the site. `work` takes the oldest queued manuscript, runs it, and exits. Run it again for the next manuscript. It prints the job id, or `idle`. A manuscript another `work` already took is left as it is. The web process does not run the queue.

SQLite is the database. The file is `data/editting.sqlite` unless `DATA_DIR` or `DATABASE_PATH` is set. Manuscript files sit under that data directory. There is no `DATABASE_URL`. Sessions are rows in that file. The row stores a hash of the cookie. The `SECRET` environment variable is not read. There is no `SESSION_SECRET` to set.

The desk creates that directory, the database, and the manuscript files with the umask of the process. It leaves those modes alone. A umask of `022` leaves the database and the manuscripts readable by other accounts on the machine when those accounts can reach the directory. The account that runs the desk chooses the mode. Certificate keys the desk writes under `DATA_DIR/acme` are owner-only.

### The data directory

The application does not encrypt this directory or the database. `SECRET` is not a key. Do not set one. There is no application cipher. A mode is not encryption.

The loss that storage is meant to stop is a stolen disk. Encrypt the disk this directory is on. Encrypt backups as well. A backup is the manuscripts, the letters, and every secret, in the clear. When `DATABASE_PATH` is a file outside the directory, include that file. The secrets are in the database.

The directory holds:

- Manuscripts, letters, compendiums, and readbacks under `DATA_DIR/objects`.
- Persona packages under `DATA_DIR/agents`, and the copy a run makes under `DATA_DIR/jobs`. The run copy includes the manuscript.
- In the database: names, sign-in emails, contact emails, phone numbers, and message threads.
- Each editor's Patreon client secret and webhook secret, and each author's Patreon refresh token.
- A mail password or API key, once a mailbox is connected.
- A provider API key, once that provider is saved on **Settings**. `XAI_API_KEY` and `LLM_API_KEY` in the environment are not written into the database when no provider row is saved.
- Session ids. The cookie holds the raw id. The database stores its SHA-256 hash. A session lasts 7 days. A copy of the hashed row is not a login. A backup that still holds the raw id works as the cookie until that row expires or the person signs out. Signing out deletes the row, then clears the cookie. A Patreon login state lasts 10 minutes and is stored the same way. The redirect carries the raw value.

A letter run sends the manuscript to the model provider. That is the feature. Encrypting the disk does not undo it.

Rotate Patreon credentials, mail keys, and any saved provider key when someone who could read the directory leaves, or when a backup leaves the operator's control. A Patreon refresh token can act as that author at Patreon. A mail key can send as that mailbox. A provider key can send a manuscript to that provider.

### Stored secrets

These are the secrets in the list above. The desk stores one when a later request needs the original value.

The editor's Patreon client secret finishes **Continue with Patreon** and rechecks a membership. The webhook secret checks the signature when one is saved. A later save can leave either field blank. The saved value stays.

The author's Patreon refresh token stays in the database. About every 15 minutes the desk uses it to ask Patreon whether the membership is still active, so the author does not sign in again. Sign-in and a webhook check the membership too.

A mail password or API key is stored when invitation mail or author notices is connected. Mail is optional. Without a mailbox, the invite link stays on the page and a finished manuscript is not emailed. A later save can leave the secret blank. The saved secret stays.

A provider API key is stored when that provider is saved on **Settings**. `XAI_API_KEY` and `LLM_API_KEY` stay in the environment. Saving a provider does not copy them. A later save of the same label can leave the key blank. The saved key stays.

A page and an MCP reply say whether a secret is saved, and leave the value out. A log line and an error leave it out too. `deployment` says whether a model key is set. It does not call that key protected.

Invite links and local-agent bearer tokens are stored as a SHA-256 hash. The raw value is shown once. A session cookie and a Patreon login state are stored the same way. The browser keeps the raw value.

### First account

```bash
python -m app bootstrap
```

That creates `owner@example.com` as the owner and as an editor. The practice name is Owner. Sign-in has no password. It lists accounts only when `DEV_LOGIN=1` or `DEMO_MODE=1`.

Those two flags are for a private local desk or a proof of concept. Either one skips the Patreon membership check and lists every account on the sign-in page. Do not set either one on a listener other people can open.

To open the owner desk on a real process, start it with `DEV_LOGIN=1` bound to `127.0.0.1`, sign in as that owner, then restart with the flag unset. The session cookie lasts 7 days. Editors added later open the invite link shown on **System Admin**. The owner can also email that link from Invitation mail on the same page. Authors use **Continue with Patreon** on `/e/{id}`.

### Recover a staff login

The owner, an admin, an editor, and an assistant sign in with the invited email and a password. An authenticator code is the second factor when that account has one. An author has no desk password and no authenticator. The author uses Patreon.

This build does not have staff passwords or authenticator codes yet. There is nothing to reset. Do not turn `DEV_LOGIN` or `DEMO_MODE` on to get past a locked account.

When those controls are present, only the owner can reset a password or remove MFA. An admin cannot. The page and the CLI do the same thing.

The owner is signed in. **System Admin** has **Reset password** and **Remove MFA** on each staff account.

The owner cannot reach the page, or an agent is recovering the desk from the machine that runs it. Use the same `DATA_DIR` or `DATABASE_PATH` as the running process, as the user that runs the desk:

```bash
python -m app reset-password user@example.com
python -m app clear-mfa user@example.com
```

`reset-password` prints a new password once and stores only the scrypt hash. `clear-mfa` removes the authenticator requirement and the recovery codes. It does not change the password. A lost device is `clear-mfa`, then the person signs in with the password and enrolls again. A lost password and a lost device is both commands.

An agent does this from a shell on that host. MCP has no tool for it. A practice token cannot reset another account. Give the printed password to the person. Do not write it into the repo, a skill, or a log. These two commands are not in this build yet. If the process says to use `web`, `work`, or `bootstrap`, the recovery commands are not there. Say that, and do not invent another path.

### HTTPS

Pick one. The redirect URI and the webhook URL use the scheme and host of the request, so decide this before an editor registers a Patreon client.

Certificate files. The process serves TLS 1.2 and TLS 1.3. Both files are required.

```bash
TLS_CERT=/path/cert.pem TLS_KEY=/path/key.pem HOST=0.0.0.0 python -m app web
```

Let's Encrypt. Leave the certificate files empty. One domain. HTTP-01. No wildcard. If `TLS_CERT` and `TLS_KEY` are set, those files are used and Let's Encrypt is not called.

```bash
ACME_DOMAIN=desk.example.com ACME_EMAIL=you@example.com HOST=0.0.0.0 python -m app web
```

Port 80 on that name must reach this process. `ACME_HTTP_PORT=0` serves the challenge on the desk port instead, which is what you want when a proxy only forwards `/.well-known/acme-challenge/`. `ACME_STAGING=1` uses the Let's Encrypt staging directory. A certificate with more than 30 days left is reused.

A proxy terminates TLS and forwards to the desk. Nginx, Envoy, and the same kind of proxy replace these headers with the public request. They do not append a value the client sent.

- `X-Forwarded-Proto: https`
- `Host` or `X-Forwarded-Host`: the public name
- `Forwarded: proto=https;host=desk.example.com`

A proxy on this machine is trusted, and the desk can stay on `127.0.0.1`. A remote load balancer needs `TRUST_PROXY=1` and a `HOST` that balancer can reach. Leave `TLS_CERT` and `TLS_KEY` empty. The proxy holds the certificate.

`HOST` defaults to `127.0.0.1` and `PORT` defaults to `8080`.

Set `PATREON_REDIRECT_URI` only when the request still does not carry the public address. Leave it empty when the proxy headers or the desk's own TLS already do.

### Letters

The worker calls one model service for the whole desk. `LLM_PROVIDER` chooses it. The default is `xai`.

`LLM_MODE` overrides `XAI_MODE` when it is set. Both default to `local`, which runs the persona and does not call the network. `sync` calls the provider once per step.

```bash
LLM_PROVIDER=xai LLM_MODE=sync XAI_API_KEY=... python -m app work
```

xAI uses `https://api.x.ai/v1` and `grok-4.3`. `XAI_MODEL` and `XAI_BASE_URL` still set those. Do not point xAI at `https://us.api.x.ai/v1`. `XAI_MODE=sync` and `XAI_API_KEY` still work when `LLM_PROVIDER` is `xai` and `LLM_MODE` is empty. Before a customer manuscript goes to a hosted model, confirm that service does not train on prompts. If it does, do not send one. A local model stays on this machine. `DEMO_MODE=1` never calls the model, even when a key is set.

| Provider | What you set |
| --- | --- |
| `xai` | `XAI_API_KEY`. Optional `XAI_MODEL` and `XAI_BASE_URL`. |
| `openai` | `LLM_API_KEY` and `LLM_MODEL`. The host is `https://api.openai.com/v1`. |
| `anthropic` | `LLM_API_KEY` and `LLM_MODEL`. |
| `gemini` | `LLM_API_KEY` and `LLM_MODEL`. The call uses Google's OpenAI-compatible endpoint. |
| `groq` | `LLM_API_KEY` and `LLM_MODEL`. |
| `openrouter` | `LLM_API_KEY` and `LLM_MODEL`. |
| `ollama` | `LLM_MODEL` is the local tag. No key. The host is `http://127.0.0.1:11434/v1` unless `LLM_BASE_URL` is set. |
| `compatible` | `LLM_BASE_URL` and `LLM_MODEL`. Use this for llama.cpp, vLLM, LM Studio, LocalAI, or any other server that speaks `/chat/completions`. A key is optional. |

`LLM_BASE_URL` is an `http` or `https` host with no password in the URL. Named cloud providers do not read it. Their hosts are fixed. The environment key stays in the environment. Saving a provider on **Settings** stores the key typed there and does not copy `XAI_API_KEY` or `LLM_API_KEY`. `deployment` reports whether the environment key is set and does not return it.

**Settings** can connect more than one provider for a practice. The desk asks each one for its model list before saving, and it does not save a failed check. A published per-token price is kept when the list includes one. The key is not shown again. Leave it blank on a later save of the same label to keep it. `workflow.json` names the model in `model`. A run uses that model on every saved provider that lists it. The cheapest published price is tried first. An exact id wins when the price is the same, including an id that ends with `/{model}`. A provider that did not list a price is used after the ones that did. If a call fails, the next provider that lists that same model is tried. The run does not switch to a different model when the named one was listed. If none list it, the desk uses the practice's default models, cheapest published price first. A default with no published price stays in the order it was saved. If none of those are listed either, the manuscript is not sent. The run fails with `no configured provider offers {model}` when no default is saved, or `no configured provider offers {model} or a default model` when defaults are saved and none are listed. If every call fails, the run fails with `no configured provider could run {model}`. A file that does not name a model uses the defaults. With no default saved, that file fails with `this workflow does not name a model` once a provider is saved. With defaults saved and none listed, it fails with `no configured provider offers a default model`. With no provider saved, the process variables above are the one provider, and local mode still writes the letter on this machine. A manual run from the local agent does not call a provider.

`LLM_TIMEOUT` is seconds. The default is 120 for a hosted provider and 600 for `ollama` and `compatible`.

A job stops at $8 or 4,000,000 tokens. The desk stops for the day at `DAILY_STOP_MICROS` (default `75000000`, which is $75). Those dollars use the grok-4.3 schedule for every provider. They are a size brake, not the other service's invoice. Authors do not see the cost.

### Mail

Mail is optional. Without it, invite links stay on the page and a finished manuscript is not emailed.

The owner connects one mailbox on **System Admin**. It emails editor and admin invites. The link is still shown if the mailbox is not connected or the send fails. Assistant invites are not emailed.

Each editor connects their own mailbox on **Settings**. It emails that editor's authors when a manuscript is ready or did not finish. The message goes to the author's sign-in email. The contact email on Profile is a different field.

The desk checks the account before it saves. A failed check is not stored. SMTP needs a host, a port, STARTTLS or TLS, a username, and a password. There is no cleartext SMTP. Amazon SES can be that SMTP server. When the host blocks outbound SMTP, choose an HTTPS API: Resend, Postmark, SendGrid, or Mailgun. Those use port 443. Mailgun also asks for its domain and for US or EU. The password or API key is not shown after it is saved. Leave it blank on a later save to keep it.

### After the public URL answers

Each editor connects their own Patreon client. The steps are in the [editor guide](docs/editors.md). The redirect URI is that public origin plus `/patreon/callback`. The webhook is the same origin plus `/patreon/webhook`. Every editor registers those same two addresses on their own client. The desk does not share one platform client.

### Kubernetes

The image is the Dockerfile in this directory. The chart is [charts/sived](charts/sived).

```bash
docker build -t sived:0.1.0 .
helm install sived charts/sived
```

A version tag on the public repository publishes `ghcr.io/klemsidd/sived:<version>`. GitHub does not run the desk. Set `image.repository` to `ghcr.io/klemsidd/sived` when the cluster pulls that image.

The chart runs one pod. A volume at `/data` is the data directory from [The data directory](#the-data-directory). The web process and the worker share that volume. The strategy is Recreate, so the next pod starts after the current one stops. `openssl` and `pdftotext` are in the image. LibreOffice is included only when the build uses `--build-arg INSTALL_SOFFICE=1`. The image does not install Python packages.

Ingress, cert-manager, a Gateway route, a network policy, an external secret, a pod disruption budget, and a secret reloader are off until you set them. The chart does not install those controllers. The values are in the [chart README](charts/sived/README.md).

The desk opens SQLite on that volume. It does not read `DATABASE_URL`. It does not open Postgres. `extraManifests` applies what you put there. The desk does not connect to it.

The container listens on `0.0.0.0` port `8080`. Leave `DEV_LOGIN` and `DEMO_MODE` at `0` on a listener other people can open. Prefer Ingress or Gateway TLS. A certificate mounted into the process is for the public name, and the kubelet calls the pod IP.

The worker sidecar runs `python -m app work` and then sleeps. `worker.mode: cronjob` runs that command on a schedule, on the same node as the volume. `worker.mode: off` leaves the queue to you.

## Sign in

`DEV_LOGIN=1` and `DEMO_MODE=1` each start with two accounts. You do not create them. The owner is the editor. Another editor is added from **System Admin** when a separate practice is needed.

| Email | Where Continue lands |
| --- | --- |
| `owner@example.com` | Owner and editor. SIVED opens first. The persona is already published. **System Admin** in that menu invites editors and admins. |
| `author@example.com` | Author desk, **Your manuscripts**. This author is the owner's customer. |

```bash
DEV_LOGIN=1 python -m app web
```

The terminal prints the public author page. On this local desk that page offers **Become a customer** and does not ask Patreon. A real desk offers **Continue with Patreon** instead. The built-in author does not need either one.

Open http://127.0.0.1:8080, choose the email, and press Continue. Sign out and choose another email to switch desks. Leave both flags unset when you are not running a local desk or a proof of concept. Either flag skips the Patreon membership check. Google sign-in is not in this build. Invite links for extra people are shown on the page. Invitation mail and author notices are optional. See [Mail](#mail).

## Author access

Each editor connects their own Patreon client. This desk does not share one client across practices, and it is not listed in the Patreon app directory. [OAuth explained](https://www.patreon.com/portal/start/oauth-explained) describes the model: register a client for one creator, and when a member signs in, check that they are currently pledged to that creator. The steps for an editor are in the [editor guide](docs/editors.md).

The redirect URI and the webhook URL use the scheme and host of the request. Decide that address in [Deploy](#deploy) before an editor registers a client. A local desk is `http`. Set `PATREON_REDIRECT_URI` only when the request still does not carry the public address. Leave it empty on a local desk. Every editor registers that same redirect URI on their own client. The login state selects the practice. The webhook address is the same origin with the path `/patreon/webhook`.

An author opens `/e/{id}` and presses **Continue with Patreon** after that editor has saved a campaign id, a client id, and a client secret. The desk allows the login only when Patreon shows a current entitlement to that campaign and the Patreon email is verified. A typed name and email is refused. An existing desk login is not attached to Patreon. `DEV_LOGIN=1` and `DEMO_MODE=1` skip this check. Leave both unset on a real desk.

The editor saves their own webhook secret on the same form. The desk checks the HMAC-MD5 signature of the raw body. A signature that matches one editor updates only that editor's authors, and only when the payload names that campaign. `members:create`, `members:update`, and `members:delete` open or close access. Without that editor's webhook secret, a dropped membership is caught on the author's next visit once the last confirmation is more than 15 minutes old.

## Run the editing workflow

```bash
DEV_LOGIN=1 python -m app web
```

In another shell, after an author has submitted:

```bash
python -m app work
```

Each run takes the oldest queued manuscript. Leave `DEMO_MODE` unset.

`LLM_MODE` and `XAI_MODE` default to `local`. That still runs the editor's agent on this machine and makes no network call. The default agent writes a compendium, four diagnoses, an optional focus pass, and a readback. Set `LLM_MODE=sync` and the provider's key to call the model once per skill step, or connect providers on **Settings** and name the model in `workflow.json`. Default models on that same page are used when a file names none, or when no provider lists the named one. The providers and the local servers are in [Letters](#letters).

A persona is a `workflow.json` file. Checkout copies the package onto the manuscript. The worker stages that copy and runs one model call per step. The built-in skills are `author-compendium`, `author-dev`, and `author-readback`. The ask on the step is the question. A custom skill is `skills/{name}/SKILL.md` in the package. [workflow.json](docs/workflow.md) is the field reference.

## Connect a local agent

The desk serves MCP at `/mcp` for the owner, an editor, and an assistant. A bearer token from **Local agent** is the login. The caller can do what that login can do in SIVED: install a persona package, see workloads, and use messages, the manuscript search, model cost, and assistants. The practice editor can also set that practice's name and colors, and connect that practice's Patreon client. An assistant cannot. A workload is one manuscript run. Running one is manual and stays on this machine. It does not call the model API. The caller cannot see or change another practice. Model cost is an operating expense. Authors do not see it.

`share/desk-persona/SKILL.md` is the skill the editor installs in the local agent. The desk also serves it at `/skills/desk-persona`. The steps are in the [editor guide](docs/editors.md). Asked what the desk still needs, that skill reads [Deploy](#deploy) and calls `deployment`.

## Deploy a demo

`DEMO_MODE=1` is the proof-of-concept switch. Submitting a manuscript stores it, marks it ready, and writes a short note that no letter was produced. No prompt is built and no model is called, even if `XAI_API_KEY` is set and even if `XAI_MODE=sync`. You do not run `python -m app work`. If a manuscript was already queued and you then turn the flag on, one `python -m app work` closes that manuscript the same way.

Model cost on a demo manuscript is zero. Messages, the human-edit request, the manuscript search, and assistants still work. Every page says the desk is a demo.

```bash
DEMO_MODE=1 HOST=0.0.0.0 PORT=8080 python -m app web
```

The server creates `owner@example.com` and `author@example.com` if they are not already there. Sign in as `author@example.com` for the author desk, or `owner@example.com` for SIVED. The sign-in page lists every account and links the public author page. Add another editor from **System Admin** when you need one. Turn `DEMO_MODE` off before you want the agent to run.

## Tests

```bash
python -m unittest discover tests
```

## License

The application in this repository is under the MIT license. The copyright notice is in [LICENSE](LICENSE): Copyright (c) 2026 KlemSidd LLC.

KlemSidd and SIVED are names of KlemSidd LLC. The MIT license does not grant those names.

The container image also contains Debian packages. Those programs keep their own licenses. [NOTICE](NOTICE) names them.

Report a vulnerability the way [SECURITY.md](SECURITY.md) describes.
