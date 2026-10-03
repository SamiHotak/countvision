"""Local web app: live preview, draw lines and zones, live counters, today's chart, CSV export.

Start it with ``countvision-edge app --config <file>``. Everything stays on this computer.
"""

from .runner import LiveRunner, Scope
from .server import create_app

__all__ = ["LiveRunner", "Scope", "create_app"]
