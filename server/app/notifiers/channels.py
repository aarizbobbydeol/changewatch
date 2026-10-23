"""Multi-channel Notification Layer for AI SRE Instant Alerts.

Supports Slack, Discord, Microsoft Teams, Email (SMTP), and Generic Webhook.
Specified in AI SRE Prompt Stage B4.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Protocol
import httpx

logger = logging.getLogger("changewatch.notifiers")


class Notifier(Protocol):
    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        """
        Send notification payload to the specified channel.
        Returns a channel-specific message ID or reference token (e.g. Slack ts).
        """
        ...


class SlackNotifier:
    """Slack channel notifier supporting Bot Token (threaded) and Incoming Webhooks."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        bot_token = config.get("bot_token")
        channel_id = config.get("channel_id")
        webhook_url = config.get("webhook_url")
        thread_ts = payload.get("thread_ts")

        # Bot token method (preferred for threading)
        if bot_token and channel_id:
            url = "https://slack.com/api/chat.postMessage"
            headers = {
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            }
            body: Dict[str, Any] = {
                "channel": channel_id,
                "text": payload.get("text", "ChangeWatch Alert"),
                "blocks": payload.get("blocks", []),
            }
            if thread_ts:
                body["thread_ts"] = thread_ts

            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(url, headers=headers, json=body)
                res_data = res.json()
                if not res_data.get("ok"):
                    raise RuntimeError(f"Slack API error: {res_data.get('error')}")
                return str(res_data.get("ts", ""))

        # Incoming webhook fallback
        elif webhook_url:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(webhook_url, json=payload)
                res.raise_for_status()
                return "webhook_delivered"
        else:
            raise ValueError("Slack configuration missing bot_token+channel_id or webhook_url")


class DiscordNotifier:
    """Discord channel notifier using Discord webhooks."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Discord configuration missing webhook_url")

        content = payload.get("text", "ChangeWatch Alert")
        embeds = []
        if "details" in payload:
            d = payload["details"]
            embeds.append({
                "title": d.get("title", "Regression Detected"),
                "description": d.get("description", ""),
                "color": 0xFF4444 if d.get("status") == "confirmed" else 0xFFA500,
                "fields": [
                    {"name": "Where", "value": d.get("where", "Unknown"), "inline": True},
                    {"name": "Owners", "value": d.get("owners", "None"), "inline": True},
                ],
            })

        body = {"content": content}
        if embeds:
            body["embeds"] = embeds

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(webhook_url, json=body)
            res.raise_for_status()
            return "discord_delivered"


class TeamsNotifier:
    """Microsoft Teams channel notifier using connector webhooks."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Microsoft Teams configuration missing webhook_url")

        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "EA4335",
            "summary": payload.get("text", "ChangeWatch Alert"),
            "sections": [
                {
                    "activityTitle": payload.get("text", "ChangeWatch Alert"),
                    "facts": [
                        {"name": k, "value": str(v)}
                        for k, v in payload.get("details", {}).items()
                    ],
                    "markdown": True,
                }
            ],
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(webhook_url, json=card)
            res.raise_for_status()
            return "teams_delivered"


class WebhookNotifier:
    """Generic JSON webhook notifier."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        url = config.get("url")
        if not url:
            raise ValueError("Generic webhook configuration missing url")

        headers = {"Content-Type": "application/json"}
        secret = config.get("secret")
        if secret:
            import hmac
            import hashlib
            data = json.dumps(payload)
            sig = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
            headers["X-ChangeWatch-Signature"] = f"sha256={sig}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, headers=headers, json=payload)
            res.raise_for_status()
            return "webhook_delivered"


class EmailNotifier:
    """SMTP email notifier."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        host = config.get("smtp_host", "localhost")
        port = int(config.get("smtp_port", 25))
        sender = config.get("from_addr", "alerts@changewatch.local")
        recipients = config.get("to_addrs", [])
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]

        if not recipients:
            raise ValueError("Email configuration missing to_addrs")

        # Non-blocking SMTP send via asyncio to_thread
        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(payload.get("text", "ChangeWatch Alert"))
        msg["Subject"] = payload.get("subject", "ChangeWatch Regression Alert")
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)

        def _send():
            with smtplib.SMTP(host, port, timeout=10.0) as server:
                user = config.get("smtp_user")
                pw = config.get("smtp_password")
                if user and pw:
                    server.login(user, pw)
                server.sendmail(sender, recipients, msg.as_string())

        await asyncio.to_thread(_send)
        return "email_delivered"


NOTIFIERS: Dict[str, Notifier] = {
    "slack": SlackNotifier(),
    "discord": DiscordNotifier(),
    "teams": TeamsNotifier(),
    "webhook": WebhookNotifier(),
    "email": EmailNotifier(),
}


def get_notifier(channel_type: str) -> Optional[Notifier]:
    return NOTIFIERS.get(channel_type.lower())
