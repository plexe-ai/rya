"""Control-plane API (optional; requires the [api] extra)."""

try:  # pragma: no cover - optional dependency
    from .app import build_app

    __all__ = ["build_app"]
except ImportError:  # pragma: no cover
    __all__ = []
