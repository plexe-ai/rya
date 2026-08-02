"""The credential boundary (D18): tenant code holds no secrets, only a socket.

Three modules, split by which side of the boundary they run on:

- :mod:`~rya.broker.protocol` — the wire, the method allowlist, and capabilities.
  Imports nothing from the other two, because the allowlist is the part worth
  reading on its own.
- :mod:`~rya.broker.server` — runs in the claimer. Holds the DSN, the seal keys,
  the pooled provider key and the egress route.
- :mod:`~rya.broker.client` — runs in the sandbox. A ``Store``-shaped façade over
  the socket, plus the mediated services.
- :mod:`~rya.broker.inventory` — the audit: what a process holds, and whether that
  is allowed for the posture it claims.

Nothing here is imported at package scope by ``rya`` — the trusted posture never
loads it, and a laptop should not pay for a socket protocol it will not use.
"""

from __future__ import annotations

from .client import BrokerClient, BrokerStore, client_from_env
from .protocol import (
    BROKER_ENV,
    CAPABILITY_ENV,
    SOCKET_ENV,
    Capability,
    broker_enabled,
)
from .server import BrokerServer

__all__ = [
    "BROKER_ENV", "CAPABILITY_ENV", "SOCKET_ENV",
    "BrokerClient", "BrokerServer", "BrokerStore", "Capability",
    "broker_enabled", "client_from_env",
]
