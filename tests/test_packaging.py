"""Checks on what actually ships, as opposed to what the source says."""

from pathlib import Path

import quantity_guard


def test_the_package_is_marked_as_typed():
    """The classifier claims inline types; without this marker no checker reads them."""
    marker = Path(quantity_guard.__file__).parent / "py.typed"
    assert marker.exists()


def test_the_version_is_stated_once():
    """pyproject reads the version from here, so the two cannot drift apart. It did:
    the attribute said 0.6.2 while the release said 0.7.0."""
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'path = "src/quantity_guard/__init__.py"' in pyproject
    assert quantity_guard.__version__.count(".") == 2
