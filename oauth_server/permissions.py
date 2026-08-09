import hashlib

from django.conf import settings
from django.core.cache import cache


def _source_identifier(request) -> str:
    meta = getattr(request, "META", None) or {}
    remote_addr = str(meta.get("REMOTE_ADDR") or "")
    trusted_proxy = remote_addr in {"127.0.0.1", "::1"}
    real_ip = str(meta.get("HTTP_X_REAL_IP") or "")
    if trusted_proxy and real_ip:
        return real_ip
    if remote_addr:
        return remote_addr
    return "unknown"


def _within_hourly_limit(prefix: str, source: str, limit: int) -> bool:
    if limit <= 0:
        return False
    digest = hashlib.sha256(source.encode()).hexdigest()
    key = f"operational-inbox:oauth:{prefix}:{digest}"
    if cache.add(key, 1, timeout=3_600):
        return True
    try:
        return cache.incr(key) <= limit
    except ValueError:
        cache.set(key, 1, timeout=3_600)
        return True


class RateLimitedDCRPermission:
    """Allow anonymous MCP client registration within a bounded rate."""

    def has_permission(self, request) -> bool:
        return (
            settings.OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED
            and settings.OAUTH_DCR_ENABLED
            and _within_hourly_limit(
                "dcr",
                _source_identifier(request),
                settings.OAUTH_DCR_PER_IP_HOURLY_LIMIT,
            )
        )
