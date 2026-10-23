"""ChangeWatch PII Scrubber.

Configurable PII scrubber (SCRUB_PII=true by default) for:
- Email addresses
- IPv4 and IPv6 addresses
- JWT tokens (JSON Web Tokens)
- Credit card numbers / PANs
"""
from __future__ import annotations

import os
import re
from typing import Optional

# 1. Email address pattern
EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# 2. IPv4 address pattern (0-255 octets)
IPV4_PATTERN = re.compile(
    r"\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
)

# 3. IPv6 address pattern
IPV6_PATTERN = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
)

# 4. JWT token pattern (header.payload.signature)
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]{10,}\b"
)

# 5. Credit card / payment card pattern (13-19 digits, formatted with spaces or hyphens)
CARD_PATTERN = re.compile(
    r"\b(?:\d{4}[-\s]?){3}\d{4}\b"
)


def scrub_pii(text: Optional[str], enabled: Optional[bool] = None) -> Optional[str]:
    """Scrub personally identifiable information (PII) from strings."""
    if not text:
        return text

    if enabled is None:
        enabled = os.getenv("SCRUB_PII", "1").lower() in ("1", "true", "yes")

    if not enabled:
        return text

    # Redact JWTs first
    scrubbed = JWT_PATTERN.sub("[REDACTED_JWT]", text)

    # Redact Emails
    scrubbed = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", scrubbed)

    # Redact IPv4 and IPv6
    scrubbed = IPV4_PATTERN.sub("[REDACTED_IP]", scrubbed)
    scrubbed = IPV6_PATTERN.sub("[REDACTED_IP]", scrubbed)

    # Redact Credit Cards
    scrubbed = CARD_PATTERN.sub("[REDACTED_CARD]", scrubbed)

    return scrubbed
