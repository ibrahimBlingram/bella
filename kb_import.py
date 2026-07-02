#!/usr/bin/env python3
"""
kb_import.py — turn ONE Bella_KB theme folder into the project's knowledge/ files.

    python kb_import.py Creator_Economy

Writes into knowledge/:
    theme_<slug>.md   <- that theme's topic seeds      (always refreshed)
    product.md        <- Blingram messaging + facts     (created ONLY if missing,
                         so it never overwrites facts you've already filled in)

After running:
  1) fill the [FILL IN] lines in knowledge/product.md  (do this ONCE)
  2) set  stream.theme: <slug>  in config.yaml          (slug is printed below)
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent
KB = ROOT / "Bella_KB"
OUT = ROOT / "knowledge"


def main():
    themes = sorted(p.name for p in KB.iterdir() if p.is_dir()) if KB.exists() else []
    if len(sys.argv) < 2:
        print("usage: python kb_import.py <Theme>")
        print("themes:", ", ".join(themes) or "(put the Bella_KB/ folder next to this script)")
        return

    theme = sys.argv[1]
    folder = KB / theme
    if not folder.is_dir():
        print(f"no theme folder '{theme}'.  available: {', '.join(themes)}")
        return

    OUT.mkdir(exist_ok=True)
    slug = theme.lower()

    # 1. theme seeds -> always refreshed
    topics = folder / "topics.txt"
    theme_md = OUT / f"theme_{slug}.md"
    theme_md.write_text(
        f"# THEME: {theme} — topic seeds (riff on these, never read verbatim)\n\n"
        + (topics.read_text(encoding="utf-8").strip() if topics.exists() else ""),
        encoding="utf-8",
    )
    print(f"wrote knowledge/{theme_md.name}")

    # 2. product.md -> created only if missing (never clobber your filled facts)
    product_md = OUT / "product.md"
    prod_txt = folder / "product.txt"
    if product_md.exists():
        print("kept existing knowledge/product.md (not overwritten)")
    elif prod_txt.exists():
        product_md.write_text(prod_txt.read_text(encoding="utf-8"), encoding="utf-8")
        print("wrote knowledge/product.md  (TEMPLATE — fill the [FILL IN] lines)")

    # 3. nudge about anything still unfilled
    if product_md.exists():
        n = product_md.read_text(encoding="utf-8").count("[FILL IN")
        if n:
            print(f"  !! {n} [FILL IN] placeholders remain in product.md — fill them for good answers.")

    print(f"\nnext: set  stream.theme: {slug}  in config.yaml, then run  python test_brain.py")


if __name__ == "__main__":
    main()
