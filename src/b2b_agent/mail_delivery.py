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
) -> str:
    if config.provider == "gmail":
        if not gmail_token:
            raise DeliveryConfigurationError("Gmail token was not initialized")
        message = make_message(config, recipient, subject, body, message_key)
        response = await client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {gmail_token}"},
            json={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()},
        )
        if 400 <= response.status_code < 500:
            raise DeliveryRejected(f"HTTP {response.status_code}")
        response.raise_for_status()
        provider_id = response.json().get("id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError("Mail provider did not return a message ID")
        return provider_id
    return await asyncio.to_thread(_smtp_deliver, config, recipient, subject, body, message_key)


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


def make_message(config, recipient, subject, body, message_key):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config.from_email, recipient, subject
    message["Message-ID"] = f"<{message_key}@{config.from_email.split('@')[1]}>"
    if config.reply_to:
        message["Reply-To"] = config.reply_to
    message.set_content(body)
    return message


def _smtp_deliver(config, recipient, subject, body, message_key):
    message = make_message(config, recipient, subject, body, message_key)
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
