"""Outbound mail for two separate accounts.

The owner connects one mailbox for invitations. Each editor connects one
mailbox for notices to that editor's authors. A transport is SMTP, or one
HTTPS provider this desk actually calls. The account is checked before it
is stored. A failed check is not saved.

Amazon SES can be the SMTP server. When a host blocks outbound SMTP, the
HTTPS providers use port 443: Resend, Postmark, SendGrid, and Mailgun.
"""

from __future__ import annotations

import base64
import json
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

SMTP_UNREACHABLE = (
    "that SMTP server could not be reached. "
    "Many hosts block outbound SMTP. An HTTPS mail API uses port 443."
)
SMTP_AUTH = "that SMTP server refused the username or password"
SMTP_FROM = "that SMTP server refused the from address"
API_AUTH = "that mail API refused the credential"
API_UNREACHABLE = "that mail API could not be reached"
SEND_REFUSED = "the mail service refused the message"

_PROVIDERS = ("resend", "postmark", "sendgrid", "mailgun")
_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_TIMEOUT = 10
_USER_AGENT = "SIVED"


class MailError(ValueError):
    """A mailbox could not be checked or a message could not be sent."""


def normalize(fields: dict) -> dict:
    """Check the shape of one account. Unused transport fields are blank.

    A blank secret is allowed here. The caller fills it from the saved
    account, then asks for a secret when nothing is saved yet.
    """
    if not isinstance(fields, dict):
        raise MailError("a mail transport is required")
    transport = _text(fields.get("transport")).lower()
    if transport not in ("smtp", "https"):
        raise MailError("a mail transport is required")
    account = {
        "transport": transport,
        "from_address": _address(fields.get("from_address"), "a from address is required"),
        "smtp_host": "",
        "smtp_port": 0,
        "smtp_security": "",
        "smtp_username": "",
        "secret": "",
        "https_provider": "",
        "mailgun_domain": "",
        "mailgun_region": "",
    }
    secret = fields.get("secret")
    if secret is None:
        secret = ""
    if not isinstance(secret, str):
        if transport == "smtp":
            raise MailError("an SMTP password is required")
        raise MailError("a mail API key is required")
    if transport == "smtp":
        account["smtp_host"] = _hostname(fields.get("smtp_host"), "an SMTP host is required")
        account["smtp_port"] = _port(fields.get("smtp_port"))
        security = _text(fields.get("smtp_security")).lower()
        if security not in ("starttls", "tls"):
            raise MailError("SMTP security must be STARTTLS or TLS")
        account["smtp_security"] = security
        account["smtp_username"] = _token(fields.get("smtp_username"), "an SMTP username is required")
        account["secret"] = _smtp_secret(secret)
        return account
    provider = _text(fields.get("https_provider")).lower()
    if provider not in _PROVIDERS:
        raise MailError("a mail provider is required")
    account["https_provider"] = provider
    account["secret"] = _api_secret(secret)
    if provider == "mailgun":
        account["mailgun_domain"] = _hostname(
            fields.get("mailgun_domain"), "a Mailgun domain is required"
        )
        region = _text(fields.get("mailgun_region")).lower()
        if region not in ("us", "eu"):
            raise MailError("a Mailgun region is required")
        account["mailgun_region"] = region
    return account


def require_secret(account: dict, saved: str) -> dict:
    """Use the typed secret, or the saved one when the field was left blank."""
    secret = account.get("secret") or ""
    if not secret:
        secret = saved or ""
    checked = dict(account)
    checked["secret"] = secret
    if account["transport"] == "smtp":
        if not secret or any(ord(ch) < 32 for ch in secret) or len(secret) > 200:
            raise MailError("an SMTP password is required")
        return checked
    if (
        not secret
        or len(secret) > 200
        or any(ch.isspace() or ord(ch) < 32 for ch in secret)
    ):
        raise MailError("a mail API key is required")
    return checked


def clean_origin(origin: str) -> str:
    """scheme://host from a request. A strange value is dropped."""
    if not isinstance(origin, str):
        return ""
    origin = origin.strip()
    if not origin:
        return ""
    parts = urllib.parse.urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    host = parts.hostname
    if any(ord(ch) < 32 or ch.isspace() for ch in host):
        return ""
    netloc = f"[{host}]" if ":" in host else host
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme}://{netloc}"


def probe(account: dict) -> None:
    """Confirm the account without sending mail. Raise MailError when it fails."""
    if account.get("transport") == "smtp":
        _smtp_probe(account)
        return
    _https_probe(account)


def deliver(account: dict, to: str, subject: str, text: str) -> None:
    """Send one plain-text message. Raise MailError when the service refuses it."""
    recipient = _address(to, SEND_REFUSED)
    subject = _subject(subject)
    body = _body(text)
    if account.get("transport") == "smtp":
        _smtp_deliver(account, recipient, subject, body)
        return
    _https_deliver(account, recipient, subject, body)


def _smtp_probe(account: dict) -> None:
    try:
        client = _smtp_session(account["smtp_host"], int(account["smtp_port"]), account["smtp_security"])
    except Exception:
        raise MailError(SMTP_UNREACHABLE) from None
    try:
        _smtp_greet(client, account["smtp_security"])
        client.login(account["smtp_username"], account["secret"])
        client.mail(account["from_address"])
        client.rset()
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailError(SMTP_AUTH) from None
    except smtplib.SMTPSenderRefused:
        raise MailError(SMTP_FROM) from None
    except Exception:
        raise MailError(SMTP_UNREACHABLE) from None
    finally:
        _smtp_quit(client)


def _smtp_deliver(account: dict, to: str, subject: str, text: str) -> None:
    try:
        client = _smtp_session(account["smtp_host"], int(account["smtp_port"]), account["smtp_security"])
    except Exception:
        raise MailError(SMTP_UNREACHABLE) from None
    try:
        _smtp_greet(client, account["smtp_security"])
        client.login(account["smtp_username"], account["secret"])
        message = EmailMessage()
        message["From"] = account["from_address"]
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        client.send_message(message)
    except MailError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise MailError(SMTP_AUTH) from None
    except smtplib.SMTPSenderRefused:
        raise MailError(SMTP_FROM) from None
    except smtplib.SMTPServerDisconnected:
        raise MailError(SMTP_UNREACHABLE) from None
    except smtplib.SMTPException:
        raise MailError(SEND_REFUSED) from None
    except (TimeoutError, ConnectionError, OSError):
        raise MailError(SMTP_UNREACHABLE) from None
    except Exception:
        raise MailError(SEND_REFUSED) from None
    finally:
        _smtp_quit(client)


def _smtp_session(host: str, port: int, security: str):
    context = ssl.create_default_context()
    if security == "tls":
        return smtplib.SMTP_SSL(host, port, timeout=_TIMEOUT, context=context)
    return smtplib.SMTP(host, port, timeout=_TIMEOUT)


def _smtp_greet(client, security: str) -> None:
    client.ehlo("sived")
    if security == "starttls":
        client.starttls(context=ssl.create_default_context())
        client.ehlo("sived")


def _smtp_quit(client) -> None:
    try:
        client.quit()
    except Exception:
        try:
            client.close()
        except Exception:
            return


def _https_probe(account: dict) -> None:
    provider = account.get("https_provider") or ""
    secret = account.get("secret") or ""
    if provider == "resend":
        status = _request("GET", "https://api.resend.com/domains", _bearer(secret), None)
    elif provider == "postmark":
        status = _request(
            "GET",
            "https://api.postmarkapp.com/server",
            _postmark(secret),
            None,
        )
    elif provider == "sendgrid":
        status = _request("GET", "https://api.sendgrid.com/v3/scopes", _bearer(secret), None)
    elif provider == "mailgun":
        status = _request("GET", _mailgun_domain_url(account), _basic(secret), None)
    else:
        raise MailError("a mail provider is required")
    if status == 200:
        return
    if status in (401, 403):
        raise MailError(API_AUTH)
    raise MailError(API_UNREACHABLE)


def _https_deliver(account: dict, to: str, subject: str, text: str) -> None:
    provider = account.get("https_provider") or ""
    secret = account.get("secret") or ""
    sender = account["from_address"]
    if provider == "resend":
        status = _request(
            "POST",
            "https://api.resend.com/emails",
            _json_headers(_bearer(secret)),
            json.dumps({"from": sender, "to": [to], "subject": subject, "text": text}).encode(),
        )
    elif provider == "postmark":
        status = _request(
            "POST",
            "https://api.postmarkapp.com/email",
            _json_headers(_postmark(secret)),
            json.dumps(
                {
                    "From": sender,
                    "To": to,
                    "Subject": subject,
                    "TextBody": text,
                    "MessageStream": "outbound",
                }
            ).encode(),
        )
    elif provider == "sendgrid":
        status = _request(
            "POST",
            "https://api.sendgrid.com/v3/mail/send",
            _json_headers(_bearer(secret)),
            json.dumps(
                {
                    "personalizations": [{"to": [{"email": to}]}],
                    "from": {"email": sender},
                    "subject": subject,
                    "content": [{"type": "text/plain", "value": text}],
                }
            ).encode(),
        )
    elif provider == "mailgun":
        headers = _basic(secret)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        status = _request(
            "POST",
            _mailgun_message_url(account),
            headers,
            urllib.parse.urlencode(
                {"from": sender, "to": to, "subject": subject, "text": text}
            ).encode(),
        )
    else:
        raise MailError("a mail provider is required")
    if status in (200, 201, 202):
        return
    raise MailError(SEND_REFUSED)


def _request(method: str, url: str, headers: dict, body: bytes | None) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read(1024)
            return resp.status
    except urllib.error.HTTPError as exc:
        try:
            exc.read(1024)
        except Exception:
            pass
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        raise MailError(API_UNREACHABLE) from None


def _bearer(secret: str) -> dict:
    return {
        "Authorization": f"Bearer {secret}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }


def _postmark(secret: str) -> dict:
    return {
        "X-Postmark-Server-Token": secret,
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }


def _basic(secret: str) -> dict:
    token = base64.b64encode(f"api:{secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }


def _json_headers(headers: dict) -> dict:
    copied = dict(headers)
    copied["Content-Type"] = "application/json"
    return copied


def _mailgun_base(region: str) -> str:
    if region == "eu":
        return "https://api.eu.mailgun.net"
    return "https://api.mailgun.net"


def _mailgun_domain_url(account: dict) -> str:
    domain = urllib.parse.quote(account.get("mailgun_domain") or "", safe="")
    return f"{_mailgun_base(account.get('mailgun_region') or '')}/v3/domains/{domain}"


def _mailgun_message_url(account: dict) -> str:
    domain = urllib.parse.quote(account.get("mailgun_domain") or "", safe="")
    return f"{_mailgun_base(account.get('mailgun_region') or '')}/v3/{domain}/messages"


def _address(value, message: str) -> str:
    if not isinstance(value, str):
        raise MailError(message)
    value = value.strip()
    if (
        not value
        or len(value) > 200
        or value.count("@") != 1
        or value.startswith("@")
        or value.endswith("@")
        or any(ch.isspace() or ord(ch) < 32 for ch in value)
    ):
        raise MailError(message)
    return value


def _hostname(value, message: str) -> str:
    if not isinstance(value, str):
        raise MailError(message)
    value = value.strip().lower()
    if (
        not value
        or len(value) > 253
        or "://" in value
        or "/" in value
        or "@" in value
        or _HOST.fullmatch(value) is None
    ):
        raise MailError(message)
    return value


def _port(value) -> int:
    if isinstance(value, bool) or value is None:
        raise MailError("an SMTP port is required")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise MailError("an SMTP port is required")
        value = int(value)
    if isinstance(value, float) or not isinstance(value, int):
        raise MailError("an SMTP port is required")
    if not 1 <= value <= 65535:
        raise MailError("an SMTP port is required")
    return value


def _token(value, message: str) -> str:
    if not isinstance(value, str):
        raise MailError(message)
    value = value.strip()
    if not value or len(value) > 200 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise MailError(message)
    return value


def _smtp_secret(value: str) -> str:
    if any(ord(ch) < 32 for ch in value) or len(value) > 200:
        raise MailError("an SMTP password is required")
    return value


def _api_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) > 200 or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise MailError("a mail API key is required")
    return value


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _subject(value: str) -> str:
    if not isinstance(value, str):
        value = ""
    cleaned = "".join(ch if ord(ch) >= 32 else " " for ch in value)
    cleaned = " ".join(cleaned.split())
    return (cleaned[:200] or "SIVED")


def _body(value: str) -> str:
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    return value.replace("\x00", "")
