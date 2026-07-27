"""Declared-dependency hygiene (implementation_plan Phase 8).

Guards against a clean install missing a directly-imported package.
"""

from __future__ import annotations

from pathlib import Path

_REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _declared_packages() -> list[str]:
    lines = _REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    return [
        line.strip().lower()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def test_numpy_is_declared_directly():
    """bot/charts.py imports numpy, so it must be a declared runtime dependency."""
    packages = _declared_packages()
    assert any(pkg.startswith("numpy") for pkg in packages), (
        "numpy must be declared in requirements.txt — bot/charts.py imports it "
        "directly and must not rely on it arriving transitively via matplotlib."
    )


def test_charts_module_imports_numpy():
    """The premise of the check above: charts really does import numpy."""
    source = (
        Path(__file__).resolve().parent.parent / "bot" / "charts.py"
    ).read_text(encoding="utf-8")
    assert "import numpy" in source
