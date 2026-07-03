"""
convert_webp_to_jpg.py — OPTIONAL. Only needed if OBS shows a blank background
on the .webp project images.

OBS's Media Source (ffmpeg) usually decodes webp fine, so try the stream first.
If a project background comes up blank, run this once to make a JPG mirror of the
whole media tree, then point config.yaml -> media.projects_root at the new root:

    pip install pillow
    python convert_webp_to_jpg.py

It writes  <root>_jpg/NN_project/....jpg  preserving folder names and order, so
featured.py maps it identically. Your original webp folder is left untouched.
"""
import sys
from pathlib import Path

SRC = Path("/Users/ibrahim/Downloads/sobha_images_by_project")
DST = SRC.with_name(SRC.name + "_jpg")

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow not installed. Run:  pip install pillow")


def main():
    n = 0
    for webp in sorted(SRC.rglob("*.webp")):
        out_dir = DST / webp.parent.relative_to(SRC)
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / (webp.stem + ".jpg")
        if out.exists():
            continue
        Image.open(webp).convert("RGB").save(out, "JPEG", quality=90)
        n += 1
        if n % 50 == 0:
            print(f"  ...{n} converted")
    print(f"Done. {n} images -> {DST}")
    print(f"Now set  media.projects_root: {DST}  in config.yaml")


if __name__ == "__main__":
    main()
