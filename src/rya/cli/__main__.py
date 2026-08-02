"""``python -m rya.cli`` — the CLI without depending on PATH.

The console script (``rya``) is the interface an operator uses. The supervisor
(D25) needs something stricter: it must launch *its own* build, and a ``PATH``
lookup can resolve to a different install. ``sys.executable -m rya.cli`` cannot —
same interpreter, same ``sys.path``, same ``rya``. See
``execution.drivers.worker_command``.

The ``if __name__`` guard is load-bearing beyond the usual reason. D27's warm pool
spawns its template with ``multiprocessing``'s spawn method, which re-imports the
parent's ``__main__`` in the child — under the name ``__mp_main__``, not
``__main__``. Without the guard, every template start would re-enter the CLI.
"""

from .main import app

if __name__ == "__main__":  # pragma: no cover - exercised by subprocess
    app()
