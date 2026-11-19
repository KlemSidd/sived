# Security

Report a vulnerability privately through GitHub private vulnerability reporting on this repository. Do not open a public issue for a security report.

There is no bounty.

The data directory is plaintext. That is the design of this build. Manuscripts, letters, names, contact details, and the stored secrets are in the clear. Encrypt the disk and encrypt backups. A report that restates that design is not a vulnerability.

`DEV_LOGIN` and `DEMO_MODE` list accounts and skip the Patreon check. Leave both unset on a listener other people can open.
