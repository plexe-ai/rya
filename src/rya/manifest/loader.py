"""Locate, parse, and validate ``rya.agent.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from ..errors import RyaError
from .schema import Manifest

MANIFEST_NAME = "rya.agent.yaml"


# Kept as an alias so callers can `except ManifestError`; it's just RyaError.
ManifestError = RyaError


def find_manifest(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from ``start`` (cwd by default) looking for rya.agent.yaml."""
    cur = (start or Path.cwd()).resolve()
    for path in [cur, *cur.parents]:
        candidate = path / MANIFEST_NAME
        if candidate.is_file():
            return candidate
    return None


def load_manifest(path: Optional[Path] = None) -> Manifest:
    """Load + validate a manifest. Raises RyaError with a stable code on failure."""
    if path is None:
        path = find_manifest()
        if path is None:
            raise RyaError(
                "E_MANIFEST_NOT_FOUND",
                f"No {MANIFEST_NAME} found in the current directory or any parent.",
                hint=f"Run `rya create <name>` to scaffold one, or cd into a project that has {MANIFEST_NAME}.",
            )

    path = Path(path)
    if not path.is_file():
        raise RyaError(
            "E_MANIFEST_NOT_FOUND",
            f"Manifest not found at {path}.",
            hint="Check the path or run `rya create <name>`.",
        )

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RyaError(
            "E_MANIFEST_INVALID",
            f"Could not parse YAML in {path}: {exc}",
            hint="Fix the YAML syntax. `rya init --json` reports the first error location.",
        )

    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", []))
        raise RyaError(
            "E_MANIFEST_INVALID",
            f"Manifest validation failed at '{loc}': {first.get('msg')}",
            hint="Compare against the example manifest in docs/primitives.md or `rya create`.",
        )

    return manifest
