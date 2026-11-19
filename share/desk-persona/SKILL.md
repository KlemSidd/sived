---
name: desk-persona
description: >
  Work on SIVED from this agent. Use when the editor wants to
  write the workflow.json they developed here onto a persona, see or run a
  workload, do the desk work this login is assigned, or determine what this
  desk still needs before other people use it.
---

# Desk persona

The file you write is `workflow.json`. The desk runs one model call for each step. The editor has usually already developed the process with you. Put that process in `workflow.json` on the persona they name.

The field reference is `docs/workflow.md` in the desk repo. A running desk serves the same page at `/docs/workflow`.

This skill uses the `sived` MCP.

## Connect

The desk shows the MCP address and a token under **Local agent**. The token is shown once. The server is the HTTP address from the desk, with header `Authorization: Bearer <token>`.

If `whoami` is not available, stop and say the `sived` MCP is not connected.

## Who can write a persona

Call `whoami`. Use a tool only when its permission is true.

- An author cannot do this.
- The owner is also an editor. `whoami` lists `roles`. **System Admin** on the desk is for the owner and for an admin. This skill does not invite an editor, add an admin, or suspend a practice.
- Personas (`offers` in whoami) is required to save or publish a persona.
- Manuscripts is required to see or run a workload.
- Messages, Pipeline, Costs (`payouts` in whoami), and Assistants each gate the tools of that name.

Writes stay on this login's practice.

## workflow.json

`manifest.json` names the file:

```json
{"harness": "workflow", "entry": "workflow.json"}
```

```json
{
  "title": "Developmental edit",
  "steps": [
    {"name": "compendium", "skill": "author-compendium", "ask": "Compile what this manuscript establishes.", "produces": "compendium"},
    {"name": "promise", "skill": "author-dev", "ask": "Diagnose promise and genre fit.", "into": "letter", "heading": "Promise and genre fit", "attach": "compendium"},
    {"name": "focus", "skill": "author-dev", "ask": "Diagnose only the author's focus.", "into": "letter", "heading": "Author focus", "attach": "compendium", "when": "focus"}
  ]
}
```

Each step is one model call. There is no loop. `produces` stores that step as its own document. `into` adds it under `heading`. A step has one of those, not both. `attach` is an earlier step name. `when` is `focus` or omitted. A readback step needs a compendium step.

Name a built-in skill on the step: `author-dev`, `author-compendium`, or `author-readback`. Do not add files for those names. The ask on the step is the question. A custom skill is `skills/{name}/SKILL.md`, plus any text files beside it. The worker reads `SKILL.md` and does not run scripts from the package.

Call `list_personas` and `get_persona` before writing. Say which persona will change. Show `workflow.json` and the paths. Write only after they agree.

`install_agent_files` writes the set in one call: `manifest.json`, `workflow.json`, and any custom skill files. `write_agent_file` changes one file on a package that is already valid. Do not write `agent.py`. The desk does not run it.

`publish_persona` makes the persona available when they asked to publish. It does not take a price. A draft is not on the public page until it is published. Author access is an active membership of that editor's Patreon campaign. The editor connects their own Patreon client. A typed name and email is not access. This desk does not charge an author.

Do not send a manuscript in the package.

## Workloads

A workload is one manuscript run. `list_workloads` is the list. `get_workload` is one run: the form, the manuscript, and documents already stored. Messages are on that result only when Messages is true.

`run_workload` runs a queued workload on the desk. It does not call the model API. A submitted manuscript is queued. Run one only when they name it. Then tell them the status and which documents came back.

Do not paste the manuscript into the chat unless they asked to read it.

## The rest of the desk

These match SIVED. Do the one they asked for.

- `send_message` writes on a workload thread.
- `search_manuscripts` searches this practice's manuscripts for authors to edit by hand. It does not pause or change the automated run. Set `human_requested` to true for authors who asked.
- `model_cost` is the practice model spend. That spend is an operating expense. Authors do not see it. Do not quote it to an author.
- `list_assistants`, `invite_assistant`, `revoke_assistant_invite`, `set_assistant_permissions`, and `remove_assistant` are the staff. The invite path is shown once. Give it to them. Removing an assistant leaves authors and manuscripts. You cannot remove the practice editor.
- `patreon_integration` shows this practice's Patreon status, campaign id, client id, whether each secret is saved, the redirect URI, and the webhook URL.
- `set_patreon_integration` saves `campaign_id`, `client_id`, `client_secret`, and an optional `webhook_secret` for this practice. Leave a secret empty to keep the saved one.
- `practice_settings` shows the practice name, the color profile and mode, and the same Patreon status.
- `set_practice_settings` sets `name`, `profile` (`desk`, `ink`, `forest`, `sea`, `custom`), `mode` (`light`, `dark`), and for a custom profile `accent`, `paper`, and `ink`. Leave a field out to keep the saved value.
- `author_notices` shows this practice's mailbox for telling an author a manuscript is ready or did not finish. `secret` is `saved` or `not saved`. It does not return the secret.
- `set_author_notices` saves that mailbox. `transport` is `smtp` or `https`. An HTTPS provider is `resend`, `postmark`, `sendgrid`, or `mailgun`. Mailgun also needs `mailgun_domain` and `mailgun_region` `us` or `eu`. SMTP needs `smtp_host`, `smtp_port`, `smtp_security` (`starttls` or `tls`), and `smtp_username`. `secret` is the SMTP password or the API key. Leave `secret` empty to keep the saved one. `remove` true deletes it. The desk checks the account before saving.
- `inference` lists the providers saved for this practice, every model each one lists, and `defaults`. `secret` is `saved`, `not saved`, or `not required`. `prices` is present when a provider published a per-token price, in micros per million tokens, for the prompt and the completion. A missing price is not free. It does not return the key.
- `set_inference` saves one provider. `provider` is `xai`, `openai`, `anthropic`, `gemini`, `groq`, `openrouter`, `ollama`, or `compatible`. `label` names the row. The same label replaces it. `secret` is the API key. Leave it empty to keep the saved one. `base_url` is for `ollama` and `compatible`. `remove` true deletes that label. The desk lists models before it saves. Put the model id in `workflow.json` as `model`. Do not put a provider or a key in that file.
- `set_inference_defaults` saves `models`, a list of model ids in order. An empty list clears them. A workflow's named model is used when any provider lists it. These are used when the file names none, or when none list the named one. The cheapest published price is tried first. A default with no published price stays in this order.

Patreon, settings, author-notice, and inference tools are for the practice editor. An assistant does not have them. Do not repeat a client secret, a webhook secret, a mail secret, or a provider key, and do not write any of them into a file.

`invitation_mail` and `set_invitation_mail` are the owner's mailbox for editor and admin invites. The fields match `set_author_notices`. Only the owner has these tools. Configuring that mailbox is not sending an invite. An editor who is not the owner does not have them.

Suspending a practice, inviting an editor, and adding an admin stay on **System Admin**. This skill does not do those. An admin cannot change the owner.

## Recover a staff login

Only the owner can reset a password or remove MFA, and only on a staff account. An author uses Patreon and has no desk password.

Do not look for an MCP tool. A practice token cannot reset another account. From a shell on the host that runs the desk, with that process's `DATA_DIR` or `DATABASE_PATH`, run the commands in the Deploy section of `README.md`: `python -m app reset-password` and `python -m app clear-mfa`. Those commands are not in this build yet. If the process rejects them, say so. Do not turn on `DEV_LOGIN` or `DEMO_MODE`. Do not store the new password in the repo, a skill, or a log. Tell the person.

## Deploy

When they ask how to deploy, what to install, or what is still missing, read the Deploy section of `README.md`. That is the checklist. Do not deploy from `local-docs/`. Those are earlier designs and they are not in this repository. This build has no Postgres, Stripe, Caddy, or object storage. The container is the Dockerfile. The chart is `charts/sived`, in that Deploy section. Its Ingress, cert-manager, network policy, and external secret stay off until set. It does not read `SECRET` or `SESSION_SECRET`. The application does not encrypt the data directory. Manuscripts, letters, names, contact details, and the stored secrets are in the clear. Encrypt the disk and encrypt backups. Rotate Patreon credentials, mail keys, and saved provider keys when someone who could read the directory leaves, or when a backup leaves the operator's control. Do not add a cipher. Mail is optional. The owner connects invitation mail. Each editor connects author notices. SMTP works, including Amazon SES as SMTP. When the host blocks outbound SMTP, use Resend, Postmark, SendGrid, or Mailgun.

If this login is the practice editor, call `deployment`. It reports this process: flags, the HTTPS choice, whether a model key is present, and `still_needed`. It does not return the key or a secret. An assistant does not have the tool. Say the open items in `still_needed`, in that order. Name the variable. Do not invent a unit file or a second app. The chart in Deploy is this desk.

`python -m app web` serves the site. `python -m app work` runs one queued manuscript and exits. `deployment` cannot see the worker. Say that when letters are part of the question.

HTTPS is one of three choices: `TLS_CERT` and `TLS_KEY`, `ACME_DOMAIN` and `ACME_EMAIL`, or a proxy that terminates TLS and replaces the forwarded headers. Certificate files win over Let's Encrypt. Leave `DEV_LOGIN` and `DEMO_MODE` unset on a listener other people can open.

After the public name answers over HTTPS, call `patreon_integration` and give them the redirect URI and the webhook URL. Save with `set_patreon_integration` when they supply the values. Do not write the secrets into a file.

A workflow names its model in `workflow.json`. Call `inference` for the models this practice can run, `set_inference` when they want a provider connected, and `set_inference_defaults` for the models to use when a file names none or no provider lists the named one. The run uses a named model on any saved provider that lists it, cheapest published price first. If none list it, the run uses a default model. If nothing listed can run, the run fails and the manuscript is not sent. `deployment` still reports `llm_provider`, `llm_model`, `llm_base_url`, and `llm_key_set` for the process. It does not return a key. Those process variables are the one provider only when Settings has no provider saved. The default there is `xai`, model `grok-4.3`, at `https://api.x.ai/v1`. Do not point that provider at a regional host. Other providers are `openai`, `anthropic`, `gemini`, `groq`, `openrouter`, `ollama`, and `compatible`. `compatible` is llama.cpp, vLLM, LM Studio, LocalAI, or any other `/chat/completions` server. The names and variables are in the Letters section of `README.md`. A hosted model needs a confirmation that it does not train on manuscripts before a customer manuscript is submitted. If it does, do not send one. `ollama` and `compatible` stay on the operator's machine. `run_workload` does not call a provider.
