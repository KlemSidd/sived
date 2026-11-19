# Editors

You run one practice. Authors are your customers. They find your page and upload a manuscript. Access is your Patreon membership. A manuscript run is included. This desk does not charge the author.

Model cost is an operating expense of the practice. Authors do not see it.

A human edit is arranged with you, off this desk. This desk does not charge, hold, or invoice that work.

Assistants are your staff. They sign in to this same desk, and you choose what each one can do. They do not get their own authors or their own page.

## Sign in

The owner invites you. Open the invite link, enter your name, and press **Accept**. Then sign in. The owner is already an editor on their own login. **System Admin** is in that editor menu for the owner and for an admin. It is not in yours unless you are one of those.

On a local desk or a demo, open the sign-in page, choose `owner@example.com`, and press **Continue**. That login is the owner and the editor. The persona is already published. A separate editor is not created. The owner adds one from **System Admin** when a second practice is needed.

Press **Sign out** in the header when you are done. **Profile** sets your name, contact email, and phone. The sign-in email stays the account. Assistant invites are shown on the screen. Author notices are on Settings.

The desk opens on the manuscript in hand. That page is the pass, the thread, and a shelf of the other manuscripts. A manuscript the author asked you to edit by hand comes first. **Pipeline** is how you search for the next one. **Personas**, **Manuscripts**, **Costs**, **Assistants**, **Settings**, **Local agent**, and **Your page** stay in the menu. An owner or an admin also has **System Admin**. An assistant sees only the sections assigned to them. With no manuscript yet, the desk names the practice and the active personas.

## Settings

**Settings** is one page with a tab for practice, appearance, Patreon, author notices, inference, and default models. The open tab is the only form. The manuscript page does not.

The practice name is the heading when no manuscript is open, and it is on your public page. Change it and press **Save practice name**.

**Profile** is Desk, Ink, Forest, Sea, or Custom. **Mode** is Light or Dark. Custom uses an accent, a background, and a text color. The public page, the author's pages, and this desk use those colors. Press **Save appearance**.

**Author notices** emails an author's sign-in address when a manuscript is ready or did not finish. SMTP and HTTPS are tabs. SMTP uses the mail server. HTTPS uses port 443 when a host blocks outbound SMTP. Amazon SES can be the SMTP server. On HTTPS, Resend, Postmark, SendGrid, and Mailgun are tabs. Mailgun is the tab that asks for the domain and US or EU. The desk checks the account before it saves. Leave the password or API key blank to keep the saved one. The form does not show that secret. An assistant cannot change this.

**Inference** is where a provider is connected. xAI, OpenAI, Anthropic, Gemini, Groq, OpenRouter, Ollama, and another OpenAI-compatible server are tabs. Give it a label. The base URL is on the Ollama tab and the compatible-server tab. The others use their own host. The desk asks the provider for its model list before it saves. A failed check is not stored. The key is not shown again. Leave it blank when you save the same label again. **Check again** asks that provider for its current list. A failed check keeps the saved list. This page shows the first 40 names. A price is shown next to a model only when that provider published one. The figure is dollars per million tokens, input then output. A workflow names the model it wants. A run uses that model on any saved provider that lists it. The cheapest published price is tried first. A provider that did not list a price is used after the ones that did. If a call fails, the next provider that lists that same model is tried. If none list it, the desk uses a default model. **Default models** shows every model each saved provider returned, one provider at a time. The page says how many, and when the list was last checked. **Check again** updates that list. Type part of a name to narrow a long list. Priced models are first, cheapest input price first. **Add** puts that model on the default list. **Move up** and **Move down** set the order. **Remove** takes one off. An id the last check did not include can be typed in. A default with no published price stays in that order. A workflow with no model uses those defaults. With no default saved, it does not run once a provider is saved. Until a provider is saved, the desk uses the process setup.

The local agent reads `practice_settings` and saves with `set_practice_settings`. Leave a field out to keep the saved value. It reads `author_notices` and saves with `set_author_notices`. It reads `inference` and saves with `set_inference` and `set_inference_defaults`. An assistant cannot open Settings.

## Personas

**Personas** lists every persona on the practice. The desk starts with one, **Developmental editor**. It is a `workflow.json` file, and it is a draft until you publish it. Authors do not see a draft.

A persona is a `workflow.json` file. The worker reads that file and runs one model call for each step. [How workflow.json works](workflow.md) names every field. On a running desk that same page is `/docs/workflow`.

**Create a workflow.json** is one page with tabs: Upload, Edit, and Wizard. The open tab is the only form. Upload and Edit each ask for a name and **Max words**. The default max is 180,000.

- **Upload** takes a `workflow.json` you already have. The desk checks the JSON before it saves.
- **Edit** is a text editor. The desk checks the JSON before it saves. Invalid JSON is not saved.
- **Wizard** creates a new file from the developmental edit. It does not change a file you already saved. Its tabs are Steps, Add a step, and File. Steps is the file you are building. Add a step is one new step. File is the JSON. **Create this file** stays on each of those tabs.

Open a persona. Its tabs are Edit, Upload, Details, and Package. Edit is the `workflow.json` text. Upload replaces that file. Details is the name and the word limit. Package is the file list and **Install package**. The same JSON check runs before a save. **Publish** stays on the page.

The starting file writes a compendium, then four diagnoses (promise and genre fit, plot and structure, character, stakes and pacing), an optional focus pass, and a readback. A step marked `focus` runs only when the author filled in a focus. A readback step requires a compendium step.

The built-in skills are `author-dev`, `author-compendium`, and `author-readback`. Name one on a step. The ask on that step is the question. A custom skill is `skills/{name}/SKILL.md` inside the package. The worker reads that file for the step and does not run scripts from the package.

Open a persona to see its steps, `workflow.json`, and the model cost estimate at the word limit. That estimate is an operating expense. It is not shown to the author.

Press **Publish**. The status becomes active, and the name appears on your public page. Publishing does not set a price.

**Install package** takes a zip that includes `manifest.json` and `workflow.json`. Checkout copies that package onto the manuscript. A later install does not change a manuscript that was already submitted. A zip that names `agent.py` is refused.

## Bring a local workflow

**Local agent** is how a `workflow.json` you built with your own agent becomes the persona. Develop the process with that agent first. Then have it write the file.

The page shows the MCP address. Press **Create token**, copy the token, and put it in the local agent's `Authorization: Bearer` header. The desk shows the token once. **Revoke** turns that token off.

**Download the desk persona skill** and install it as `desk-persona/SKILL.md` where that agent loads skills. Ask it to write the process as `workflow.json`. It writes `manifest.json`, `workflow.json`, and any custom skill files. It publishes the persona when you ask. The field reference is [workflow.json](workflow.md).

An assistant can do this when **Personas** is assigned. The token belongs to the person who created it, and it only changes this practice.

The same connection does the rest of this desk, and only the parts assigned to that login. A workload is one manuscript run. The agent can list workloads and open one, which includes the manuscript and any documents already stored. **Run** takes a queued workload and runs it on this desk. That run does not call the model API. Messages, the manuscript search, model cost, and assistants are the same actions as the sections below.

Ask that agent what the desk still needs before other people use it. It reads the Deploy section of the [README](../README.md) and, for the practice editor, calls `deployment`. An assistant does not get that tool. Earlier designs sit in `local-docs/` on the machine that keeps them. They are not in this repository, and they are not the checklist.

## Patreon

Author access is a live check against your campaign. You register your own Patreon client and save it on this desk. Another editor registers a different client. A member of your campaign does not get into their practice. Patreon describes this in [OAuth explained](https://www.patreon.com/portal/start/oauth-explained): a tool for one creator. When a member signs in, the desk checks that they are currently pledged to you. This desk is not a Patreon app-directory product, and it does not use one client for every creator.

A name and email typed on your page are not a membership.

### Create the client

Use the Patreon account that owns the campaign. [Become a creator](https://www.patreon.com/create) if you have not. You do not have to launch the page. The client's permissions follow that campaign.

Open [Clients & API Keys](https://www.patreon.com/portal/registration/register-clients) and register an API v2 client.

Settings shows the redirect URI in a box. The local agent shows the same address from `patreon_integration`. Paste that exact address into the client. It is this desk's `/patreon/callback`, and it is `https` when the desk is reached that way, including through a proxy that terminates TLS. Every editor on this desk registers that same address. The desk uses a one-time `state` value to tell the logins apart. Patreon refuses a redirect that does not match.

When an author presses **Continue with Patreon**, this desk asks for three scopes: `identity`, `identity[email]`, and `identity.memberships`. Those are how the desk reads the email and the current membership. The older v1 scopes are not used.

Copy the client id and the client secret from that client. Copy the numeric id of your campaign. The secret is yours. Do not put it in a persona or a message. Settings and the local agent save it and do not show it again.

### Tell the desk when a membership changes

Open [My Webhooks](https://www.patreon.com/portal/registration/register-webhooks). The same creator account is required. Settings shows the webhook URL. The local agent shows the same address from `patreon_integration`. It is this desk's `/patreon/webhook`. Every editor registers that same address and a different secret.

Include member data. Turn on `members:create`, `members:update`, and `members:delete`. Copy the webhook secret.

A webhook that does not include the member cannot change access. The desk checks the signature on the raw body. A signature that matches your secret updates only your authors, and only when the payload names your campaign. Another editor's webhook does not close your authors. A payload that does not say whether the member is entitled is ignored. `members:delete` closes access.

The webhook secret can wait. Without it, a dropped membership is caught the next time that author opens the desk, once the last confirmation is more than 15 minutes old. **Continue with Patreon** still requires the client.

### Save it on Settings

**Patreon** is on Settings. It shows Status and Campaign, the redirect URI, and the webhook URL.

Enter the campaign id, the client id, and the client secret. Enter the webhook secret when you have one. Press **Save Patreon**.

Only you can save this. An assistant cannot. Saving makes each author's next visit ask Patreon again. The form does not clear the campaign id. The campaign id is digits.

A later save can leave a secret blank. The desk keeps the secret it already stored. The page says whether each secret is saved. It does not print the secret.

The local agent can save the same fields. Ask it to connect Patreon. It reads `patreon_integration`, then saves with `set_patreon_integration`. An assistant's agent cannot. The agent must not repeat the secrets or write them into a file.

Status is **Connected** when the campaign id, the client id, and the client secret are all saved. Until then, your public page does not offer a way in. It says this editor has not connected Patreon.

## Your page

**Your page** shows `/e/{id}`. Put that link on your website.

On a real desk the author presses **Continue with Patreon**. Patreon sends them back here. The desk allows that login when all of these are true:

- Patreon has verified that account's email. If it has not, the page says to verify the email at Patreon. The desk does not create an account from it.
- The membership is for the campaign id you saved.
- Patreon shows a current entitled amount, or at least one currently entitled tier. A free entitled tier counts. A former member, with no entitled amount and no entitled tier, is refused.
- That email is not already an editor, an admin, the owner, or another local login. This desk does not attach Patreon to a login it did not verify. [OAuth explained](https://www.patreon.com/portal/start/oauth-explained) is why: an unverified local email must not be taken over by a Patreon login.
- That Patreon account is not already a customer of another editor.

If Patreon does not answer, or the membership is missing from the reply, access stays closed. The desk stores a refresh token and asks again when the last confirmation is more than 15 minutes old. A dropped membership closes the session.

On a local desk or a demo, the public page still offers **Become a customer** and does not ask Patreon. Leave `DEV_LOGIN` and `DEMO_MODE` unset before a real author uses it.

An assistant has no public page. Their desk still links to your page. Opening `/e/{their id}` says that editor is not taking customers.

## Manuscripts

**Manuscripts** lists each submission: title, author, status, and model cost. Open a title to read the manuscript page. The author sees the status and the word count. The author does not see the model cost.

| Status | Meaning |
| --- | --- |
| `queued` | Submitted. The worker has not taken it yet. |
| `running` | The worker has taken it. |
| `ready` | The run is on the page. |
| `failed` | The run stopped. |

The worker takes the oldest queued manuscript. It leaves a manuscript that is already `running`.

A finished page shows the documents the agent stored. The default agent stores a letter, a compendium, and a readback. The letter diagnoses the manuscript. It does not rewrite it.

On a demo, a submitted manuscript is marked `ready` with a note that the agent did not run.

The page also shows the word count and **Messages**. You also see the model cost. Reply with **Send** when Messages is assigned to your login. The note on the page is the rule for human work: a human edit is arranged with you, off this desk.

## Pipeline

**Pipeline** searches the manuscripts already on this desk. Use it to find authors you may want to edit by hand. It does not pause, skip, or change the automated run.

Open **Pipeline**. The first list is authors who pressed **Ask this editor to work on it**. Press **Search** to look through the rest. You can search the title, the author email, the genre, the audience, or a phrase in comps, and you can limit the word count. **Author asked for a human editor** can be Yes, No, or Either.

The search reads those fields. It does not read the manuscript, and it does not call the model. Agree on the human edit in **Messages**. That work stays off this desk.

## Costs

**Costs** shows the model spend for this practice. That spend is an operating expense. Authors do not see it. A demo manuscript records no model spend.

## Assistants

Open **Assistants**. Enter an email, check what that person can do, and press **Invite**. Give them the invite link on that page. They enter a name, press **Accept**, and sign in. The desk tells them they are signed in on your desk and can do what you assigned.

A new invite starts with Manuscripts, Messages, and Pipeline checked. Personas, Costs, and Assistants stay off until you turn them on.

| Permission | What that login can do |
| --- | --- |
| Manuscripts | See the practice manuscripts |
| Messages | Write on those manuscript threads |
| Pipeline | Search manuscripts for authors to edit by hand |
| Personas | Edit a persona and publish it |
| Costs | See the practice model spend |
| Assistants | Invite, change, and remove other assistants |

Change the checkboxes later and press **Save permissions**. An assistant who has the Assistants permission can grant only permissions they already have, and cannot change their own permissions or remove you.

Press **Revoke** to cancel an invite that has not been accepted. Press **Remove** to close a login. Authors and manuscripts stay. A removed person who signs in sees "you are no longer an editor on this practice".

You cannot remove yourself. Suspending the practice is the platform owner's action.
