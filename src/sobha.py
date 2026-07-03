"""
sobha.py — on-demand retrieval of FULL Sobha project records.

The compact index (knowledge/sobha_projects.md) is always in Bella's system
prompt so she's aware of every project. This module holds the *complete* data —
every floor plan, FAQ, amenity, landmark and currency — and hands back only the
matching project's full record when she narrates or answers about it. That keeps
100% of the dataset reachable without paying for all of it on every call.
"""
import json
import re
from pathlib import Path


def _titleize(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


class SobhaData:
    def __init__(self, path):
        self.projects = []          # list of dicts: {name, community, aliases, text}
        p = Path(path)
        if not p.exists():
            return
        data = json.loads(p.read_text())
        for it in data:
            self.projects.append(self._build(it))
        # match the most specific (longest) alias first
        self._alias_index = sorted(
            ((alias, proj) for proj in self.projects for alias in proj["aliases"]),
            key=lambda ap: -len(ap[0]),
        )

    # ---- record formatting -------------------------------------------------
    def _names(self, it):
        parts = [s for s in it.get("url", "").split("/") if s]
        try:
            seg = parts[parts.index("properties-in-dubai") + 1:]
        except ValueError:
            seg = parts[-2:]
        if not seg:
            return (it.get("title") or "Sobha project").title(), "Sobha Realty", set()
        community = _titleize(seg[0])
        aliases = {community.lower()}
        if len(seg) >= 2:
            sub = _titleize(seg[1])
            name = f"{sub} at {community}"
            aliases |= {sub.lower(), seg[1].replace("-", " ").lower(), name.lower()}
        else:
            name = community
            aliases |= {seg[0].replace("-", " ").lower()}
        # drop weak/ambiguous aliases
        aliases = {a for a in aliases if len(a) >= 4 and a not in {"sobha", "the s"}}
        return name, community, aliases

    def _build(self, it):
        name, community, aliases = self._names(it)
        L = [f"PROJECT: {name}", f"Developer: Sobha Realty | Community: {community}"]

        title = (it.get("title") or "").strip()
        if title:
            L.append(f"Tagline: {title.title()}")
        desc = (it.get("description") or "").strip()
        if desc:
            L.append(f"Description: {desc}")

        if it.get("starting_price_range"):
            L.append(f"Starting price: {it['starting_price_range']}")
        if it.get("high_price_range"):
            L.append(f"Top price: {it['high_price_range']}")
        elif it.get("prices"):
            L.append("Prices: " + " | ".join(
                f"{p.get('currency')} {p.get('value')}" for p in it["prices"]))

        fps = it.get("floor_plans") or []
        if fps:
            L.append(f"Floor plans ({len(fps)}):")
            for fp in fps:
                bits = [fp.get("name", "").strip()]
                if fp.get("unit_config"):
                    bits.append(fp["unit_config"].strip())
                if fp.get("saleable_area"):
                    bits.append(fp["saleable_area"].strip())
                L.append("  - " + " | ".join(b for b in bits if b))

        ams = it.get("amenities") or []
        if ams:
            L.append("Amenities: " + ", ".join(ams))

        near = it.get("nearby_landmarks") or []
        if near:
            L.append("Nearby: " + ", ".join(
                f"{n.get('name','').title()} ({n.get('distance','').lower()})" for n in near))

        faqs = it.get("faqs") or []
        if faqs:
            L.append(f"FAQs ({len(faqs)}):")
            for f in faqs:
                q = (f.get("question") or "").strip()
                a = (f.get("answer") or "").strip()
                if q and a:
                    L.append(f"  Q: {q}\n  A: {a}")

        return {"name": name, "community": community,
                "aliases": aliases, "text": "\n".join(L)}

    # ---- retrieval ---------------------------------------------------------
    def match(self, query: str, limit: int = 1) -> str:
        """Return the full record(s) for any project named in `query`, else ''."""
        if not query or not self.projects:
            return ""
        q = " " + re.sub(r"[^\w\s]", " ", query.lower()) + " "
        hits, seen = [], set()
        for alias, proj in self._alias_index:
            if proj["name"] in seen:
                continue
            if re.search(r"\b" + re.escape(alias) + r"\b", q):
                hits.append(proj)
                seen.add(proj["name"])
                if len(hits) >= limit:
                    break
        return "\n\n".join(p["text"] for p in hits)
