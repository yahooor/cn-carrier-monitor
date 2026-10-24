"""Carrier API clients for China Carrier Monitor."""

from __future__ import annotations

from .base import CarrierAuthError, CarrierClient, CarrierError, empty_data  # noqa: F401
from .broadnet import BroadnetClient
from .mobile import MobileClient, async_send_sms_code
from .telecom import TelecomClient
from .unicom import UnicomClient, UnicomOpenIdClient

CLIENTS = {
    "telecom": TelecomClient,
    "unicom": UnicomClient,
    "mobile": MobileClient,
    "broadnet": BroadnetClient,
}


def create_client(carrier: str, session, data: dict):
    """Instantiate the right client for an auth mode."""
    if carrier == "unicom" and data.get("auth_mode") == "openid":
        return UnicomOpenIdClient(session, data)
    factory = CLIENTS.get(carrier)
    if factory is None:
        raise CarrierError(f"未知运营商: {carrier}")
    return factory(session, data)


__all__ = [
    "CarrierAuthError",
    "CarrierClient",
    "CarrierError",
    "BroadnetClient",
    "MobileClient",
    "TelecomClient",
    "UnicomClient",
    "UnicomOpenIdClient",
    "async_send_sms_code",
    "create_client",
    "empty_data",
]
