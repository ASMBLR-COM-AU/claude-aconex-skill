"""Builds docs/index.html, the setup page GitHub Pages serves: template.html with
the logo and the skill's files inside, so the one page works offline. Rerun it
whenever anything in aconex/ changes; a test fails if the page is out of date.

    python setup/build.py
"""

import base64
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "setup" / "template.html"
PAGE = ROOT / "docs" / "index.html"
LOGO = ROOT / "setup" / "asmblr-logo.png"
SHIPPED = (
    "aconex/SKILL.md",
    "aconex/references/aconex-api.md",
    "aconex/references/setup.md",
    "aconex/scripts/aconex.py",
)


def skill_files() -> list:
    """[zip path, base64 content] for what ships: these files by name, so keys,
    caches and anything else in this checkout's aconex/ folder stay out."""
    files = []
    for rel in SHIPPED:
        # Same bytes on every checkout, whatever git does to line endings.
        data = (ROOT / rel).read_bytes().replace(b"\r\n", b"\n")
        files.append([rel, base64.b64encode(data).decode()])
    return files


def build_page() -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    logo = base64.b64encode(LOGO.read_bytes()).decode()
    return (template.replace("__LOGO__", logo)
            .replace("__SKILL_FILES__", json.dumps(skill_files(), indent=0)))


if __name__ == "__main__":
    with PAGE.open("w", encoding="utf-8", newline="\n") as f:
        f.write(build_page())
    print(f"Wrote {PAGE}")
