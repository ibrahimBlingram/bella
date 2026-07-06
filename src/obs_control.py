"""
obs_control.py — drives OBS over WebSocket.

Switching is INSTANT: both avatar clips are loaded ONCE at startup and then we
only toggle visibility, so there's no reload-from-disk lag when she starts or
stops talking. For variety, the talk clip is swapped only while it's HIDDEN
(off-screen), so rotating clips never causes a visible delay.
"""
import random

import obsws_python as obs


class OBS:
    def __init__(self, cfg):
        o = cfg["obs"]
        self.c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
        self.scene = o["scene"]
        self.idle_src = o["avatar_idle_source"]
        self.talk_src = o["avatar_talk_source"]
        self.bg_src = o["background_source"]
        self.idle_loops = o["avatar_idle_loops"]
        self.talk_loops = o["avatar_talk_loops"]
        self.demos = o["demo_backgrounds"]
        # Load each clip ONCE so switching never reloads from disk.
        if self.idle_loops:
            self._set_media(self.idle_src, random.choice(self.idle_loops))
        if self.talk_loops:
            self._set_media(self.talk_src, random.choice(self.talk_loops))

    def _set_media(self, source, path):
        self.c.set_input_settings(source, {"local_file": path}, overlay=True)

    def _set_visible(self, source, on):
        item_id = self.c.get_scene_item_id(self.scene, source).scene_item_id
        self.c.set_scene_item_enabled(self.scene, item_id, on)

    def set_talking(self, talking: bool):
        # Visibility-only toggle = instant. No file reload here.
        if talking:
            self._set_visible(self.talk_src, True)
            self._set_visible(self.idle_src, False)
        else:
            self._set_visible(self.idle_src, True)
            self._set_visible(self.talk_src, False)
            # Rotate the NEXT talk clip while it's hidden -> variety, zero visible lag.
            if len(self.talk_loops) > 1:
                self._set_media(self.talk_src, random.choice(self.talk_loops))

    def hide_avatar(self):
        """Hide BOTH avatar loops — used when nobody is in the live, so only the
        background images show. set_talking(False) restores the idle loop."""
        self._set_visible(self.talk_src, False)
        self._set_visible(self.idle_src, False)

    # ------------------------------------------------------------------
    # "Reel look" — make the LIVE scene match the exported vertical reel:
    #   * background image covers the whole canvas
    #   * both avatar loops cropped to a waist-up bust, pinned bottom-right
    #   * a title/price text source that shows only while on a project
    # All guarded: if OBS/a source isn't shaped as expected we log and skip,
    # never crash the stream. Verify/tune with tools/obs_apply_reel_layout.py.
    # ------------------------------------------------------------------
    def _canvas(self):
        v = self.c.get_video_settings()
        return v.base_width, v.base_height

    def _item_id(self, source):
        return self.c.get_scene_item_id(self.scene, source).scene_item_id

    def cover_background(self):
        """Scale the Background source to COVER the whole canvas (crop overflow)."""
        try:
            W, H = self._canvas()
            iid = self._item_id(self.bg_src)
            self.c.set_scene_item_transform(self.scene, iid, {
                "positionX": 0, "positionY": 0, "alignment": 5,   # top-left anchor
                "boundsType": "OBS_BOUNDS_SCALE_OUTER",           # cover-crop
                "boundsWidth": W, "boundsHeight": H, "boundsAlignment": 0,
                "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0,
            })
        except Exception as e:
            print(f"[obs] cover_background skipped: {e}")

    def bust_avatar(self, height_frac=0.21, keep_top=0.79, head_pad=0.055, margin=24):
        """Crop both avatar loops to a waist-up bust and pin bottom-right, sized
        to `height_frac` of the canvas height (explicit scale = predictable)."""
        try:
            W, H = self._canvas()
            for src in (self.talk_src, self.idle_src):
                iid = self._item_id(src)
                t = self.c.get_scene_item_transform(self.scene, iid).scene_item_transform
                sw, sh = t.get("sourceWidth") or 0, t.get("sourceHeight") or 0
                if not sw or not sh:
                    continue
                crop_t = int(sh * head_pad)
                crop_b = int(sh * (1 - keep_top))
                cropped_h = max(1, sh - crop_t - crop_b)
                scale = (H * height_frac) / cropped_h
                self.c.set_scene_item_transform(self.scene, iid, {
                    "cropTop": crop_t, "cropBottom": crop_b,
                    "cropLeft": 0, "cropRight": 0,
                    "boundsType": "OBS_BOUNDS_NONE",
                    "scaleX": scale, "scaleY": scale, "rotation": 0,
                    "alignment": 10,                              # bottom-right anchor
                    "positionX": W - margin, "positionY": H - margin,
                })
        except Exception as e:
            print(f"[obs] bust_avatar skipped: {e}")

    def ensure_title(self, name=None):
        """Create (if missing) and position the title/price text source, sizing
        it to the current canvas. macOS FreeType2 text source."""
        name = name or getattr(self, "title_src", "BellaTitle")
        self.title_src = name
        try:
            W, H = self._canvas()
            font = {"face": "Arial", "size": max(34, int(H * 0.024)), "style": "Bold"}
            existing = {i["inputName"] for i in self.c.get_input_list().inputs}
            if name not in existing:
                self.c.create_input(
                    self.scene, name, "text_ft2_source_v2",
                    {"text": "", "font": font, "color1": 0xFFFFFFFF,
                     "outline": True}, True)
            else:                                   # keep font in step with canvas
                self.c.set_input_settings(name, {"font": font, "outline": True},
                                          overlay=True)
            iid = self._item_id(name)
            self.c.set_scene_item_transform(self.scene, iid, {
                "positionX": int(W * 0.05), "positionY": int(H - H * 0.19),
                "alignment": 5})
        except Exception as e:
            print(f"[obs] ensure_title skipped: {e}")

    def set_title(self, name: str, price: str = ""):
        """Show the project title + price (only call while on a project)."""
        src = getattr(self, "title_src", None)
        if not src:
            return
        try:
            txt = name + (f"\nfrom {price}" if price else "")
            self.c.set_input_settings(src, {"text": txt}, overlay=True)
            self._set_visible(src, True)
        except Exception as e:
            print(f"[obs] set_title skipped: {e}")

    def hide_title(self):
        src = getattr(self, "title_src", None)
        if not src:
            return
        try:
            self._set_visible(src, False)
        except Exception as e:
            print(f"[obs] hide_title skipped: {e}")

    def _order_layers(self):
        """Stack: Background (bottom) -> avatars -> title (top). Without this a
        full-screen background would cover the avatars."""
        try:
            order = [self.bg_src, self.idle_src, self.talk_src,
                     getattr(self, "title_src", "BellaTitle")]
            for idx, name in enumerate(order):        # index 0 == bottom
                try:
                    self.c.set_scene_item_index(self.scene, self._item_id(name), idx)
                except Exception:
                    pass
        except Exception as e:
            print(f"[obs] _order_layers skipped: {e}")

    def apply_reel_layout(self):
        """One-shot: make the live scene look like the exported reel."""
        self.cover_background()
        self.bust_avatar()
        self.ensure_title()
        self._order_layers()

    def show_background(self, path: str):
        # Swap the Background source to a specific file (one slide, or a video).
        self._set_media(self.bg_src, path)

    def demo_background(self):
        if self.demos:
            self._set_media(self.bg_src, random.choice(self.demos))
