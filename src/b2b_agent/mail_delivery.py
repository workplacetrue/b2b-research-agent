"""Delivery adapters. No retries: uncertain acceptance must be reviewed in the outbox."""

import asyncio
import base64
import os
import smtplib
import ssl
from email.message import EmailMessage

import httpx

from .config import OutreachConfig


class DeliveryConfigurationError(ValueError):
    """Safe, actionable message; contains variable names, never secret values."""


class DeliveryRejected(Exception):
    pass


class DeliveryReceipt(str):
    """String-compatible legacy provider ID with distinct transport identifiers."""

    def __new__(cls, provider_message_id, rfc_message_id, thread_id=None):
        instance = super().__new__(cls, provider_message_id)
        instance.provider_message_id = provider_message_id
        instance.rfc_message_id = rfc_message_id
        instance.thread_id = thread_id
        return instance


def preflight(config: OutreachConfig) -> None:
    if not config.from_email or not config.signature.strip():
        raise DeliveryConfigurationError("Укажите outreach.from_email и outreach.signature")
    if config.provider == "gmail":
        names = (
            config.gmail_client_id_env,
            config.gmail_client_secret_env,
            config.gmail_refresh_token_env,
        )
        if not all(os.environ.get(name) for name in names):
            raise DeliveryConfigurationError("Настройте Gmail OAuth: " + ", ".join(names))
    else:
        if not config.smtp_host:
            raise DeliveryConfigurationError("Укажите outreach.smtp_host")
        if not all(
            os.environ.get(name)
            for name in (
                config.smtp_user_env,
                config.smtp_password_env,
            )
        ):
            raise DeliveryConfigurationError(
                f"Добавьте SMTP-доступ в {config.smtp_user_env} и {config.smtp_password_env}"
            )


async def deliver(
    config: OutreachConfig,
    client: httpx.AsyncClient,
    recipient: str,
    subject: str,
    body: str,
    message_key: str,
    gmail_token: str | None = None,
    *,
    authorization=None,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    original_subject: str | None = None,
    unsubscribe_headers: dict[str, str] | None = None,
) -> DeliveryReceipt:
    if authorization is None:
        raise DeliveryConfigurationError("A current one-message approval is required")
    if in_reply_to and not original_subject:
        raise DeliveryConfigurationError("Replies require the original thread subject")
    if original_subject and _normalized_subject(subject) != _normalized_subject(original_subject):
        raise DeliveryConfigurationError("Reply subject must match the original thread subject")
    message = make_message(
        config, recipient, subject, body, message_key,
        in_reply_to=in_reply_to, references=references,
        unsubscribe_headers=unsubscribe_headers,
    )
    metadata = {
        "sender": config.from_email, "reply_to": config.reply_to, "provider": config.provider,
        "thread_id": thread_id, "in_reply_to": in_reply_to,
        "references": references or [], "original_subject": original_subject,
        "unsubscribe_headers": unsubscribe_headers or {},
    }
    if config.provider == "gmail":
        if not gmail_token:
            raise DeliveryConfigurationError("Gmail token was not initialized")
        payload = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()}
        if thread_id:
            if not in_reply_to:
                raise DeliveryConfigurationError("Gmail thread reply requires In-Reply-To")
            payload["threadId"] = thread_id
        authorization.assert_valid(recipient, subject, body, message_key, metadata=metadata)
        response = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {gmail_token}"},
            json=payload,
        )
        if 400 <= response.status_code < 500:
            raise DeliveryRejected(f"HTTP {response.status_code}")
        response.raise_for_status()
        provider_id = response.json().get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("Mail provider did not return a message ID")
        provider_thread_id = response.json().get("threadId")
        if provider_thread_id is not None and not isinstance(provider_thread_id, str):
            raise ValueError("Mail provider returned an invalid thread ID")
        return DeliveryReceipt(provider_id, str(message["Message-ID"]), provider_thread_id)
    rfc_id = await asyncio.to_thread(
        _smtp_deliver, config, message, authorization, metadata,
        (recipient, subject, body, message_key),
    )
    # SMTP has no provider thread ID; an RFC Message-ID is not one.
    return DeliveryReceipt(rfc_id, rfc_id, None)


async def gmail_access_token(config: OutreachConfig, client: httpx.AsyncClient) -> str:
    response = await client.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ[config.gmail_client_id_env],
            "client_secret": os.environ[config.gmail_client_secret_env],
            "refresh_token": os.environ[config.gmail_refresh_token_env],
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise DeliveryConfigurationError("Gmail OAuth did not return an access token")
    return token


def _normalized_subject(subject):
    import re

    return re.sub(r"^(?:\s*re\s*:\s*)+", "", subject, flags=re.I).strip()


def make_message(
    config, recipient, subject, body, message_key, *,
    in_reply_to=None, references=None, unsubscribe_headers=None,
):
    import re

    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", recipient):
        raise DeliveryConfigurationError("Send requires one literal recipient mailbox")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", message_key):
        raise DeliveryConfigurationError("Unsafe RFC message key")
    if not in_reply_to and re.match(r"\s*re\s*:", subject, re.I):
        raise DeliveryConfigurationError("An initial message cannot use a false Re: subject")
    if references and not in_reply_to:
        raise DeliveryConfigurationError("References require a genuine reply context")
    ids = ([in_reply_to] if in_reply_to else []) + list(references or [])
    if any(not re.fullmatch(r"<[^<>\s\r\n]+@[^<>\s\r\n]+>", item) for item in ids):
        raise DeliveryConfigurationError("Replies require literal RFC Message-IDs")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config.from_email, recipient, subject
    message["Message-ID"] = f"<{message_key}@{config.from_email.split('@')[1]}>"
    if config.reply_to:
        message["Reply-To"] = config.reply_to
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join(dict.fromkeys([*(references or []), in_reply_to]))
    for name, value in (unsubscribe_headers or {}).items():
        if name not in {"List-Unsubscribe", "List-Unsubscribe-Post"}:
            raise DeliveryConfigurationError("Unsupported unsubscribe header")
        message[name] = value
    message.set_content(body)
    return message


def _smtp_deliver(config, message, authorization, metadata, fields):
    # Authorize the exact source body, before the MIME encoder's normalization.
    authorization.assert_valid(*fields, metadata=metadata, consume=False)
    context = ssl.create_default_context()
    try:
        if config.smtp_security == "ssl":
            connection = smtplib.SMTP_SSL(
                config.smtp_host,
                config.smtp_port,
                timeout=30,
                context=context,
            )
        else:
            connection = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        with connection as smtp:
            if config.smtp_security == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(os.environ[config.smtp_user_env], os.environ[config.smtp_password_env])
            authorization.assert_valid(*fields, metadata=metadata)
            refused = smtp.send_message(message)
            if refused:
                raise DeliveryRejected("SMTP recipient rejected")
    except (
        smtplib.SMTPAuthenticationError,
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPSenderRefused,
        smtplib.SMTPDataError,
    ) as error:
        raise DeliveryRejected(type(error).__name__) from None
    return str(message["Message-ID"])
