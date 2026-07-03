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

    def show_background(self, path: str):
        # Swap the Background source to a specific file (one slide, or a video).
        self._set_media(self.bg_src, path)

    def demo_background(self):
        if self.demos:
            self._set_media(self.bg_src, random.choice(self.demos))
