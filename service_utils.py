"""Shared helpers for reaching external services (qBittorrent, Jellyfin).

Kept in its own module so that both the qB protocol client (qb.py) and the
Jellyfin client (jellyfin_client.py) can use them without depending on each
other or on the gateway composition root (ISSUE 6, phase 0).
"""
import config


def _safe_service_error(exc: Exception, service_url: str, limit: int = 200) -> str:
    """Render a connection error without reflecting URL userinfo."""
    text = str(exc)
    raw = str(service_url)
    safe = config.public_service_url(raw)
    
    if raw:
        text = text.replace(raw, safe)
    
    return text[:limit]


