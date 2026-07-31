"""The SDK package — where the D16 package split actually cuts.

§14 puts `sdk/agent.py` in the client SDK and `sdk/context.py` in the platform:
"`ctx` stays platform code; the SDK ships type stubs". So this init must not
import `.context` at module scope. It used to, which made `import rya` (via
`rya/__init__.py` → `rya.sdk.agent`) execute the whole `ctx` implementation and
with it `store`, `providers`, `guard`, `config` and `observability` — the entire
platform tree behind the one line a client repo writes. The thin `rya` wheel
ships none of those, so that single edge was the difference between the SDK
being installable and not.

The names stay re-exported, lazily (PEP 562), so `from rya.sdk import
RuntimeContext` is unchanged wherever the platform is installed; in an SDK-only
install `context.pyi` supplies the same names to type checkers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent import Agent, define_agent

if TYPE_CHECKING:  # resolves against sdk/context.pyi in an SDK-only install
    from .context import Event, LLMResponse, RuntimeContext

__all__ = ["Agent", "define_agent", "RuntimeContext", "Event", "LLMResponse"]

_FROM_CONTEXT = frozenset({"RuntimeContext", "Event", "LLMResponse"})


def __getattr__(name: str):
    if name in _FROM_CONTEXT:
        try:
            from . import context
        except ImportError as exc:  # SDK-only install: the stubs exist, the module does not
            # Deliberately an ImportError, not an AttributeError: `from rya.sdk
            # import RuntimeContext` — the only way a client hits this — discards
            # an AttributeError and reports its own generic message instead, so
            # the explanation would never reach the person reading the traceback.
            raise ImportError(
                f"`rya.sdk.{name}` is platform code and is not part of the `rya` client SDK "
                f"(PLATFORM_DESIGN §14: `ctx` stays platform code; the SDK ships type stubs). "
                f"Import it under `if TYPE_CHECKING:` to annotate a handler, or install "
                f"`rya-server` to run one."
            ) from exc
        return getattr(context, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
