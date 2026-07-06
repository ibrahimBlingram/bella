"""
kb.py — knowledge loader for Bello's RAG context.

Drop ANY number of .md files into the knowledge/ folder and they all get loaded
automatically. The ONLY reserved naming rule:
  - theme_<name>.md   -> a monthly THEME file (only the active one loads)
  - everything else    -> always-on product knowledge (product.md, features.md, ...)

This is used by the test scripts and (in Phase 2) by main.py, so knowledge is
loaded the same way everywhere.
"""
from pathlib import Path


def load(root, theme: str):
    """
    Returns (knowledge_blob, theme_text):
      knowledge_blob = every non-theme .md in knowledge/  + the active theme file
      theme_text     = just the active theme file (main.py uses it for topic seeds)
    """
    kdir = Path(root) / "knowledge"
    docs = []

    # always-on docs (anything not named theme_*.md)
    for f in sorted(kdir.glob("*.md")):
        if f.name.startswith("theme_"):
            continue
        body = f.read_text(encoding="utf-8").strip()
        if body:
            docs.append(f"## SOURCE: {f.name}\n{body}")

    # the one active theme file
    theme_file = kdir / f"theme_{theme}.md"
    theme_text = ""
    if theme_file.exists():
        theme_text = theme_file.read_text(encoding="utf-8").strip()
        if theme_text:
            docs.append(f"## SOURCE: theme_{theme}.md\n{theme_text}")

    return "\n\n".join(docs), theme_text
