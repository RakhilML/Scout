"""Repository hygiene."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_python_sources_are_plain_ascii():
    # Invisible look-alike characters (no-break spaces, zero-width joiners) must be written as
    # \N{...} escapes, where a reviewer can see them.
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for folder in ("src", "tests")
        for path in sorted((ROOT / folder).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if not line.isascii()
    ]
    assert offenders == []
