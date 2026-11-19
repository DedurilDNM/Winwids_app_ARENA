"""Connectors for external tools (GitHub, ...).

The AI agents themselves work through the external service's own connectors
(e.g. its GitHub connector) - we only instruct them via prompts. Server-side
connectors in this package are used by OUR code (github_watcher polling
/status/*.md files, future Google Drive connector, ...).

Each connector implements the `Connector` protocol from `app.connectors.base`,
so new connectors can be added without touching the watcher/orchestrator.
"""

from app.connectors.base import Connector
from app.connectors.github import GitHubConnector

__all__ = ["Connector", "GitHubConnector"]
