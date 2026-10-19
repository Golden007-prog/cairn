"""Outbound OAuth brokering — a replaceable seam (#100).

cairn depends on the :class:`OAuthBroker` Protocol, never on a specific vendor.
AuthlibBroker (in-process) is the shipped implementation; swapping it for another
service is a new ``OAuthBroker`` impl + a config flip, with no call-site
changes. See :mod:`cairn.core.oauth.broker`.
"""
from __future__ import annotations

from cairn.core.oauth.broker import (
    AuthlibBroker,
    AuthSession,
    OAuthBroker,
    OAuthBrokerError,
    get_oauth_broker,
    register_broker,
)
from cairn.core.oauth.catalog import (
    OAuthProviderSpec,
    get_oauth_provider,
    list_oauth_providers,
    load_oauth_catalog,
)

__all__ = [
    "AuthSession",
    "AuthlibBroker",
    "OAuthBroker",
    "OAuthBrokerError",
    "OAuthProviderSpec",
    "get_oauth_broker",
    "get_oauth_provider",
    "list_oauth_providers",
    "load_oauth_catalog",
    "register_broker",
]
