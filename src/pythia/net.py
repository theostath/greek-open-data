"""Network setup helpers.

This host terminates TLS with a corporate proxy CA that lives only in the OS
trust store, not in certifi. ``use_system_trust_store`` routes Python's ssl (and
thus httpx, used by huggingface_hub for model downloads) through the OS store.
"""

from __future__ import annotations

import truststore


def use_system_trust_store() -> None:
    """Make ssl/httpx verify against the OS trust store (call before downloads)."""
    truststore.inject_into_ssl()
