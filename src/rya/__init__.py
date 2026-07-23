"""Rya — production backend/runtime for AI agents.

Public SDK surface lives here so agent code can simply do::

    from rya import define_agent

    agent = define_agent()
"""

from .sdk.agent import Agent, define_agent
from .errors import RyaError, RyaRecoverableToolError

__all__ = ["Agent", "define_agent", "RyaError", "RyaRecoverableToolError", "__version__"]

__version__ = "0.1.0"
