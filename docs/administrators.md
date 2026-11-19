# Administrators

The desk starts with one owner. That login is also an editor. The owner can add editors, assistants, and admins. An admin uses the same **System Admin** page. An admin cannot change the owner, and nobody can take ownership away.

Each editor you invite opens a separate practice. That practice has its own page, its own authors, and its own staff. Authors are customers of that editor. An editor adds assistants and chooses what each person can do. Those steps are in the [editor guide](editors.md).

## Sign in

On a local desk or a demo, open the sign-in page, choose `owner@example.com`, and press **Continue**. You land on your desk. The practice starts with the name **Owner**.

**System Admin** is in the editor menu. It is the same page for an owner and for an admin, with a tab for Editors, Assistants, Admins, and, for the owner, Invitation mail. The open tab is the only form. The page says whether you are the owner or an admin, and it lists every role on this login. Editors is the open tab.

Press **Sign out** in the header when you are done. **Profile** is where you set your name, contact email, and phone. The sign-in email stays the account. How to start the desk is in the [README](../README.md).

## Recover a staff login

Only the owner resets a password or removes MFA. An admin does not see those actions. An author has no desk password. Patreon is that sign-in.

**System Admin** is the path while you are signed in. **Reset password** sets a new password for that staff account and shows it once. **Remove MFA** turns the authenticator off for that account and leaves the password as it is. The person enrolls an authenticator again after they sign in.

When you cannot sign in, the CLI on the machine that runs the desk is the path. An agent that can open a shell there uses the same commands. The steps, and the rule that MCP cannot do this, are in the Deploy section of the [README](../README.md). The command has to use the same data directory as the running desk.

This build does not have staff passwords or authenticator codes yet. The sign-in page has no password form. Do not turn the local account list on to stand in for a reset.

## Stand the desk up

The Deploy section of the [README](../README.md) is the checklist. The machine needs Python 3.12 and no extra packages. `python -m app web` serves the site. `python -m app work` takes one queued manuscript and exits. Pick one HTTPS path before an editor registers Patreon: certificate files, Let's Encrypt, or a proxy that terminates TLS.

`python -m app bootstrap` creates `owner@example.com` when `DEV_LOGIN` and `DEMO_MODE` are unset. The sign-in page then has no account list. Sign in once with `DEV_LOGIN=1` on `127.0.0.1`, then restart without that flag. The session lasts 7 days. Do not leave either flag on a listener other people can open. Either one skips the Patreon check.

On **Local agent**, `deployment` reports this process and what is still missing. It does not return a key or a secret. Earlier designs in `local-docs/` are not the checklist. They are not in this repository.

## This login is already the editor

You do not open a second account to edit. Personas, manuscripts, the pipeline, costs, and assistants are in the editor menu. That work is the [editor guide](editors.md).

On **Settings**, **Save** renames your practice. An invite to another email opens a separate practice. A local desk or a demo does not create that second editor.

**Local agent** connects this login. It works on your practice. It does not change another editor's desk. Suspending a practice and inviting a new editor stay on **System Admin**.

## Invite an editor

On the **Editors** tab, enter the editor's email and press **Add an editor**. The page shows an invite link. If invitation mail is connected, the desk also emails that link. If it is not connected, or the send fails, the link is still on the page. The practice table is on this tab.

The editor opens the link, enters their name, and presses **Accept**. They then have a practice and a public page at `/e/{id}`. Authors use that page to become a customer.

The practice table lists the practice name, the editor's email, and the status. The owner's row has no suspend button. On a local desk or a demo the only practice is the owner's, and the persona is already published.

Send another invite for the next editor. Do not reuse one invite for two practices.

## Invitation mail

The owner connects one mailbox on the **Invitation mail** tab. It is used for editor and admin invites. An admin does not see that tab. An invite an admin creates still uses the owner's mailbox when one is connected. Assistant invites stay on the screen.

SMTP and HTTPS are tabs. On HTTPS, Resend, Postmark, SendGrid, and Mailgun are tabs. Mailgun asks for the domain and US or EU. The desk checks the account before it saves. A host that blocks outbound SMTP can use an HTTPS provider on port 443. Amazon SES can be used as SMTP. The secret is not shown after it is saved. The local agent uses `invitation_mail` and `set_invitation_mail`. Saving the mailbox does not send an invite.

## Invite an admin

On the **Admins** tab, enter an email and press **Add an admin**. Only the owner sees that button. The new admin signs in and opens the same **System Admin** page. It says they are an admin. They can add an editor and suspend a practice. They cannot add an admin, remove an admin, or suspend the owner.

**Remove admin** takes the admin flag off that login. It does not remove an editor practice or an author membership if that same email has one. It cannot be used on the owner.

## Assistants

On the **Assistants** tab, **Add an assistant** invites someone onto your practice. You choose what they can do. The same form is on **Assistants**. An admin who is not an editor does not add assistants here. The editor of that practice does.

## Suspend a practice

On another practice's row, on the **Editors** tab, enter a reason and press **Suspend**. The editor's status becomes `suspended`. The owner's practice cannot be suspended.

A suspended practice cannot take new customers. The public page says "This editor is not taking customers." A suspended editor cannot publish a persona or accept a manuscript. The reason is stored with the suspension.

Suspending a practice does not delete its authors or manuscripts. Closing an assistant's login is the editor's action, on **Assistants**, and it leaves the authors where they are.

## What each practice handles

On a practice desk, the editor publishes the persona, reads manuscripts, answers messages, and can see model cost. When that practice is yours, you do those steps from the editor menu. When it is another editor's, they do them from their own sign-in. Author access is that editor's Patreon membership. Each practice editor connects their own Patreon client on that practice's Settings page. That is not a System Admin setting. The owner does it on their own practice. An admin does not connect Patreon for another editor. The desk asks Patreon with that client and refuses the author when the membership is not active. A typed email is not access. Local and demo sign-in skip the check and must stay off on a real desk. A manuscript run is included. This desk does not charge the author. Model cost is an operating expense of the practice. A human edit is arranged with the editor, off this desk. You do not invoice that work.
