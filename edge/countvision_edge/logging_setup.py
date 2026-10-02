"""Logging setup for the command line."""

from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    """Log to the console with time, level and module name."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for noisy in ("PIL", "matplotlib", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
