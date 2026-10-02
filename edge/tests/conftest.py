"""Fixtures shared by all tests."""

from __future__ import annotations

import pytest

from countvision_edge.testing.synthetic import demo_scene


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory) -> str:
    """The synthetic demo scene as an .avi file (created once per test run)."""
    folder = tmp_path_factory.mktemp("video")
    return str(demo_scene().write_video(folder / "demo.avi"))
