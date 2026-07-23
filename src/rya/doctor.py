"""`rya doctor` - static checks for durable-execution discipline.

Handlers (@agent.on_event / @agent.job) are REPLAYED after a pause: any effect
not routed through ctx.* re-executes on resume. This linter walks the
entrypoint AST and flags raw-IO calls inside handler bodies. Tool functions
(@agent.tool) are exempt - tools are leaves and MAY do real IO.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Modules whose use inside a durable handler almost always breaks replay.
_IO_MODULES = {"requests", "urllib", "httpx", "boto3", "socket", "subprocess",
               "smtplib", "ftplib", "psycopg", "sqlite3", "pymongo", "redis"}
_IO_CALLS = {"open"}


def _decorator_kind(fn: ast.AST) -> str:
    for d in getattr(fn, "decorator_list", []):
        target = d.func if isinstance(d, ast.Call) else d
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        dotted = ".".join(reversed(parts))
        if dotted.endswith(".on_event"):
            return "handler"
        if dotted.endswith(".job"):
            return "handler"
        if dotted.endswith(".tool"):
            return "tool"
    return ""


def lint_replay(entrypoint: Path) -> list:
    """Return findings: [{line, handler, call, hint}]."""
    tree = ast.parse(Path(entrypoint).read_text())
    findings = []
    # map handler-name -> called plain functions, to follow one level of helpers
    handlers, helpers = [], {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = _decorator_kind(node)
            if kind == "handler":
                handlers.append(node)
            elif kind == "":
                helpers[node.name] = node

    def scan(fn, owner, seen):
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            root = f
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in _IO_MODULES:
                findings.append({"line": node.lineno, "handler": owner,
                                 "call": ast.unparse(f)[:60],
                                 "hint": "route this effect through a @agent.tool or ctx.*"})
            elif isinstance(f, ast.Name):
                if f.id in _IO_CALLS:
                    findings.append({"line": node.lineno, "handler": owner,
                                     "call": f.id,
                                     "hint": "file IO in a handler re-executes on replay; use ctx.files or a tool"})
                elif f.id in helpers and f.id not in seen:
                    scan(helpers[f.id], owner, seen | {f.id})

    for h in handlers:
        scan(h, h.name, set())
    return findings
