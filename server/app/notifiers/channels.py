"""Multi-channel Notification Layer for AI SRE Instant Alerts.

Supports Slack, Discord, Microsoft Teams, Email (SMTP), and Generic Webhook.
Specified in AI SRE Prompt Stage B4.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional, Protocol
import httpx

logger = logging.getLogger("changewatch.notifiers")

# Alerts quote text ChangeWatch does not control: error messages (which can echo a user's input),
# commit messages, runbook titles, AI answers. Chat apps turn some of it into actions: Slack
# "<!channel>" pings everyone and "<https://x|Fix here>" hides where a link goes; Discord
# "@everyone" pings and "[Fix here](https://x)" masks a link. Messages are made safe when sent.
_SLACK_TOKEN = re.compile(r"<@[UW][A-Z0-9]{2,20}>|<(https?://[^|<>\s]+)\|([^<>]*)>")


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_safe(text: str) -> str:
    """Slack text with every control sequence escaped, except user mentions ChangeWatch adds and
    links to its own dashboard; any other labelled link is shown as "label (url)"."""
    from app.config import settings

    ours = settings.dashboard_link("")
    out, pos = [], 0
    for match in _SLACK_TOKEN.finditer(text):
        out.append(_slack_escape(text[pos:match.start()]))
        url, label = match.group(1), match.group(2)
        if url is None or (url == ours or url.startswith(ours + "/")):
            out.append(match.group(0))
        else:
            out.append(_slack_escape(f"{label} ({url})"))
        pos = match.end()
    out.append(_slack_escape(text[pos:]))
    return "".join(out)


def _slack_safe_blocks(value: Any) -> Any:
    """Every formatted ("mrkdwn") text inside Slack blocks, made safe. Plain text is shown as
    written by Slack (no mentions, no links), so it is left alone."""
    if isinstance(value, list):
        return [_slack_safe_blocks(v) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "plain_text":
            return value
        return {k: (slack_safe(v) if k == "text" and isinstance(v, str) else _slack_safe_blocks(v)) for k, v in value.items()}
    return value


def _no_masked_links(text: str) -> str:
    """'[label](url)' shown as written instead of as a link (Discord, Teams markdown)."""
    return text.replace("](", "] (")


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
                "text": slack_safe(str(payload.get("text", "ChangeWatch Alert"))),
                "blocks": _slack_safe_blocks(payload.get("blocks", [])),
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
            safe = {**payload, "text": slack_safe(str(payload.get("text", "ChangeWatch Alert")))}
            if "blocks" in payload:
                safe["blocks"] = _slack_safe_blocks(payload["blocks"])
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(webhook_url, json=safe)
                res.raise_for_status()
                return "webhook_delivered"
        else:
            raise ValueError("Slack configuration missing bot_token+channel_id or webhook_url")


def _clip(text: Any, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class DiscordNotifier:
    """Discord channel notifier using Discord webhooks."""

    async def send(self, payload: Dict[str, Any], config: Dict[str, Any]) -> str:
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Discord configuration missing webhook_url")

        # Discord rejects (HTTP 400) content over 2000 characters and embed fields over their limits
        content = _clip(_no_masked_links(str(payload.get("text", "ChangeWatch Alert"))), 2000)
        embeds = []
        if "details" in payload:
            d = payload["details"]
            embeds.append({
                "title": _clip(d.get("title", "Regression Detected"), 256),
                "description": _clip(_no_masked_links(str(d.get("description", ""))), 4096),
                "color": 0xFF4444 if d.get("status") == "confirmed" else 0xFFA500,
                "fields": [
                    {"name": "Where", "value": _clip(_no_masked_links(str(d.get("where", "Unknown") or "Unknown")), 1024), "inline": True},
                    {"name": "Owners", "value": _clip(d.get("owners", "None") or "None", 1024), "inline": True},
                ],
            })

        # User mentions of code owners only: never @everyone, @here or a role
        body: Dict[str, Any] = {"content": content, "allowed_mentions": {"parse": ["users"]}}
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
                    "activityTitle": _no_masked_links(str(payload.get("text", "ChangeWatch Alert"))),
                    "facts": [
                        {"name": k, "value": _no_masked_links(str(v))}
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

        # Sign and send the exact same bytes, so receivers can verify the body they get
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = config.get("secret")
        if secret:
            import hashlib
            import hmac
            import time

            # Timestamp is part of the signed message so receivers can reject replays
            # (verify: HMAC-SHA256(secret, f"{timestamp}.{body}") and |now - timestamp| <= 300 s)
            timestamp = str(int(time.time()))
            sig = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
            headers["X-ChangeWatch-Timestamp"] = timestamp
            headers["X-ChangeWatch-Signature"] = f"sha256={sig}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, headers=headers, content=body)
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
        # Named after what the message is: a digest or a "Resolved" note is not a regression alert
        kind = payload.get("type") or payload.get("kind") or ""
        default_subject = {
            "weekly_digest": "ChangeWatch weekly digest",
            "incident_resolved": "ChangeWatch: resolved",
            "budget_warning": "ChangeWatch budget warning",
        }.get(kind, "ChangeWatch Regression Alert")
        title = str(payload.get("title") or "").replace("*", "").strip()
        msg["Subject"] = (payload.get("subject") or title or default_subject)[:200]
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)

        # smtp_tls: "auto" (default: STARTTLS when offered), "starttls" (required), "ssl" (implicit TLS), "none"
        tls_mode = str(config.get("smtp_tls", "auto")).lower()
        user = config.get("smtp_user")
        pw = config.get("smtp_password")

        def _send():
            import ssl

            context = ssl.create_default_context()
            smtp_cls = smtplib.SMTP_SSL if tls_mode == "ssl" else smtplib.SMTP
            kwargs = {"context": context} if tls_mode == "ssl" else {}
            with smtp_cls(host, port, timeout=10.0, **kwargs) as server:
                encrypted = tls_mode == "ssl"
                if not encrypted and tls_mode in ("auto", "starttls"):
                    server.ehlo()
                    if server.has_extn("starttls"):
                        server.starttls(context=context)
                        server.ehlo()
                        encrypted = True
                    elif tls_mode == "starttls":
                        raise RuntimeError("SMTP server does not offer STARTTLS (smtp_tls=starttls)")
                if user and pw:
                    if not encrypted:
                        raise RuntimeError("Refusing to send the SMTP password over an unencrypted connection")
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
