"""
featured.py — the 33 curated Sobha projects that have on-screen visuals.

Each project maps to a media subfolder named `NN_slug` (e.g. 01_the_grove) of
webp images — and any videos you drop in later. The idle loop walks these
projects IN ORDER (1..33); while Bella narrates project N, its media plays in
the OBS background. Facts come from the ordered manifest JSON, but the folder's
`NN_` prefix is the source of truth for both ORDER and which images to show.

  featured.projects   -> ordered list of Project (1..N)
  project.media       -> videos first, then images (for "video, then slideshow")
  featured.match(txt) -> the Project a viewer comment is about, else None
"""
import json
import re
from pathlib import Path

_IMG_EXT = {".webp", ".jpg", ".jpeg", ".png"}
_VID_EXT = {".mp4", ".mov", ".m4v", ".webm"}
_COMMUNITY_HINTS = ("sanctuary", "central", "hartland", "city", "siniya",
                    "seahaven", "skyscape", "one", "orbis", "verde")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def _titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", "_").split("_"))


class Project:
    __slots__ = ("order", "name", "community", "aliases",
                 "media_dir", "images", "videos", "facts")

    def __init__(self, order, name, community, aliases,
                 media_dir, images, videos, facts):
        self.order = order
        self.name = name
        self.community = community
        self.aliases = aliases
        self.media_dir = media_dir
        self.images = images
        self.videos = videos
        self.facts = facts

    @property
    def media(self):
        """Videos first (if any), then images — 'video, then slideshow'."""
        return self.videos + self.images


class Featured:
    def __init__(self, json_path, media_root):
        self.projects = []          # ordered 1..N
        self._alias_index = []

        recs = []
        p = Path(json_path) if json_path else None
        if p and p.exists():
            recs = json.loads(p.read_text())

        by_order, null_recs = {}, []
        for it in recs:
            o = it.get("_manifest_order")
            if o is None:
                null_recs.append(it)
            else:
                by_order[o] = it

        root = Path(media_root) if media_root else None
        folders = []
        if root and root.exists():
            folders = sorted(
                (d for d in root.iterdir()
                 if d.is_dir() and re.match(r"\d\d_", d.name)),
                key=lambda d: int(d.name[:2]),
            )

        for d in folders:
            nn = int(d.name[:2])
            fslug = d.name[3:]
            rec = by_order.get(nn)
            if rec is None:                     # e.g. island_villas (null order)
                for it in null_recs:
                    cands = {_slug(s) for s in it.get("url", "").split("/") if s}
                    if fslug in cands or any(fslug in c or c in fslug for c in cands):
                        rec = it
                        break
            self.projects.append(self._build(nn, fslug, rec or {}, d))

        # longest alias first so the most specific project wins on a match
        self._alias_index = sorted(
            ((a, pr) for pr in self.projects for a in pr.aliases),
            key=lambda ap: -len(ap[0]),
        )

    # ---- build one project -------------------------------------------------
    def _build(self, order, fslug, it, media_dir):
        name = (it.get("_manifest_project") or "").strip() or _titleize(fslug)
        segs = [s for s in it.get("url", "").split("/") if s]

        community = ""
        if len(segs) >= 2:
            cand = segs[-2]
            if cand.startswith("sobha-") or any(h in cand for h in _COMMUNITY_HINTS):
                community = _titleize(cand)

        # Aliases swap the background when a viewer asks about a project. Use the
        # project's OWN name/slug only — never the parent-community segment, or
        # e.g. "sobha one" would wrongly match "The Element at Sobha One".
        aliases = {name.lower(), fslug.replace("_", " ")}
        if segs:
            aliases.add(segs[-1].replace("-", " ").lower())
        aliases = {a for a in aliases
                   if len(a) >= 4 and a not in {"sobha", "the s", "villas", "dubai"}}

        images = sorted(str(f) for f in media_dir.iterdir()
                        if f.suffix.lower() in _IMG_EXT)
        videos = sorted(str(f) for f in media_dir.iterdir()
                        if f.suffix.lower() in _VID_EXT)

        return Project(order, name, community, aliases,
                       str(media_dir), images, videos,
                       self._facts(name, community, it))

    def _facts(self, name, community, it) -> str:
        L = [f"PROJECT: {name}"]
        L.append(f"Developer: Sobha Realty | Community: {community}"
                 if community else "Developer: Sobha Realty")

        desc = (it.get("description") or "").strip()
        if desc:
            L.append("About: " + desc)
        if it.get("starting_price_range"):
            L.append("Starting price: " + it["starting_price_range"])
        if it.get("high_price_range"):
            L.append("Top price: " + it["high_price_range"])

        beds = sorted({m.group(1)
                       for fp in (it.get("floor_plans") or [])
                       for m in [re.match(r"\s*(\d+)\s*Bed", fp.get("name", ""), re.I)]
                       if m}, key=int)
        if beds:
            L.append("Bedrooms: " + "/".join(beds) + "-BR")

        ams = it.get("amenities") or []
        if ams:
            L.append("Amenities: " + ", ".join(ams[:8]))
        near = it.get("nearby_landmarks") or []
        if near:
            L.append("Nearby: " + ", ".join(
                f"{n.get('name', '').title()} ({n.get('distance', '').lower()})"
                for n in near[:5]))
        return "\n".join(L)

    # ---- retrieval ---------------------------------------------------------
    def match(self, query: str):
        """The Project a comment is about (for background sync), else None."""
        if not query or not self._alias_index:
            return None
        q = " " + re.sub(r"[^\w\s]", " ", query.lower()) + " "
        for alias, pr in self._alias_index:
            if re.search(r"\b" + re.escape(alias) + r"\b", q):
                return pr
        return None
