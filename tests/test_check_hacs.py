"""scripts/check_hacs.py: HACS shows the README inside Home Assistant, so no relative paths."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_hacs  # noqa: E402


def test_relative_images_and_links_are_found() -> None:
    text = (
        '<img src="assets/icons/sediment.svg" width="96">\n'
        "[![License](https://img.shields.io/badge/x-y-blue)](LICENSE)\n"
        "Details are in [docs/](docs/). See [the site](https://example.org/a).\n"
        "[top](#top) [mail](mailto:a@b.c)\n"
    )
    assert check_hacs.relative_readme_targets(text) == [
        "assets/icons/sediment.svg",
        "LICENSE",
        "docs/",
    ]


def test_the_readme_of_this_repository_has_only_absolute_targets() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert check_hacs.relative_readme_targets(text) == []
