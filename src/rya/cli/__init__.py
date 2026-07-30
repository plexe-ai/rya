"""The CLI package.

§14 splits `cli/` into an operator subset (`rya-server`) and a client subset
(`rya`): `cli/main.py` is the operator CLI and imports the runtime, the store
and the model providers at module scope; `cli/client.py` is the client subset
and imports nothing outside the SDK. Eagerly re-exporting `main.app` here made
`from rya.cli import scaffold` — which ~35 tests and `mcp/ops.py` do — pull the
whole platform in, and would have put `cli/main.py` inside the thin wheel's
import closure through the package init alone. `app` is therefore resolved
lazily (PEP 562); it is unchanged wherever the platform is installed, and is not
reachable in an SDK-only install, where the `rya` console script points at
`rya.cli.client:app` instead.
"""

from __future__ import annotations

__all__ = ["app"]


def __getattr__(name: str):
    if name == "app":
        from .main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
