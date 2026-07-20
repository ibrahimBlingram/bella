"""
obs_control.py — drives OBS over WebSocket.

Switching is INSTANT: both avatar clips are loaded ONCE at startup and then we
only toggle visibility, so there's no reload-from-disk lag when he starts or
stops talking. For variety, the talk clip is swapped only while it's HIDDEN
(off-screen), so rotating clips never causes a visible delay.

EVERY VISUAL CHANGE IS SENT FROM A WORKER THREAD, NEVER FROM THE EVENT LOOP.

obsws-python is synchronous: each call is a blocking WebSocket round-trip. Called
straight from async code, it freezes the whole event loop for the duration — and
while the loop is frozen, the coroutine feeding PCM to the sound device cannot
run, so the audio buffer runs dry and the voice AUDIBLY STUTTERS. Viewers heard
Bello hiccup every time the slideshow changed a background image.

So the mutating calls are queued to a background thread and return instantly. The
queue preserves order, so "show talk / hide idle" can never land out of sequence.
Reads (which only happen at startup or in the watchdog) stay direct.
"""
import os
import queue
import random
import threading

import obsws_python as obs

from paths import abspath, abspaths


def setup_music(client, scene: str, music_cfg: dict):
    """Create (or update) a LOOPING background-music source and its sidechain
    DUCKING compressor, keyed to the voice source. Idempotent — safe to run on
    every startup and safe to re-run against a live OBS.

    This is intentionally standalone (takes a bare obsws ReqClient) so it can be
    applied to a running OBS WITHOUT restarting Bello, and it only ever touches
    the music source + its own filter — never the voice/avatar sources.

    The ducking: a Compressor on the music, with `sidechain_source` set to the
    VOICE source. When the voice rises above the threshold the music is pulled
    down hard; when he goes quiet it swells back up over the release time.
    """
    if not music_cfg or not music_cfg.get("file"):
        return
    src = music_cfg.get("source_name", "Music")
    fname = music_cfg.get("filter_name", "VoiceDuck")
    path = abspath(music_cfg["file"])
    if not path or not os.path.exists(path):
        print(f"[obs] music file not found ({path}); background music off.")
        return

    # 1) The source. ffmpeg_source plays the file; looping keeps it going forever.
    #    Creating it in the scene puts its audio into the stream mix.
    settings = {"local_file": path, "looping": True, "is_local_file": True}
    existing = {i["inputName"] for i in client.get_input_list().inputs}
    if src not in existing:
        client.create_input(scene, src, "ffmpeg_source", settings, True)
    else:
        client.set_input_settings(src, settings, overlay=True)

    # 2) Its volume WHEN NOT DUCKED (i.e. while he's silent).
    try:
        client.set_input_volume(src, vol_db=float(music_cfg.get("volume_db", -6.0)))
    except Exception as e:
        print(f"[obs] music volume skipped: {e}")

    # 3) The ducking compressor.
    d = music_cfg.get("duck") or {}
    fsettings = {
        "ratio": float(d.get("ratio", 12.0)),
        "threshold": float(d.get("threshold_db", -38.0)),
        "attack_time": int(d.get("attack_ms", 6)),
        "release_time": int(d.get("release_ms", 350)),
        "output_gain": float(d.get("output_gain_db", 0.0)),
        "sidechain_source": d.get("sidechain_source", "BellaAudio"),
    }
    try:
        have = {f["filterName"] for f in client.get_source_filter_list(src).filters}
    except Exception:
        have = set()
    try:
        if fname in have:
            client.set_source_filter_settings(src, fname, fsettings)
        else:
            client.create_source_filter(src, fname, "compressor_filter", fsettings)
        print(f"[obs] background music '{src}' + ducking '{fname}' ready "
              f"(keyed to {fsettings['sidechain_source']}).")
    except Exception as e:
        print(f"[obs] music ducking filter skipped: {e}")


class OBS:
    def __init__(self, cfg):
        o = cfg["obs"]
        self.c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
        self.scene = o["scene"]
        self.idle_src = o["avatar_idle_source"]
        self.talk_src = o["avatar_talk_source"]
        self.bg_src = o["background_source"]
        # OBS runs as its own process: it needs ABSOLUTE paths, always.
        self.idle_loops = abspaths(o["avatar_idle_loops"])
        self.talk_loops = abspaths(o["avatar_talk_loops"])
        self.demos = abspaths(o["demo_backgrounds"])

        # The worker that keeps blocking WebSocket calls off the event loop.
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)
        self._worker.start()

        # Load each clip ONCE so switching never reloads from disk.
        if self.idle_loops:
            self._set_media(self.idle_src, random.choice(self.idle_loops))
        if self.talk_loops:
            self._set_media(self.talk_src, random.choice(self.talk_loops))

    def _drain(self):
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception as e:
                # A visual glitch must never take down the stream.
                print(f"[obs] {getattr(fn, '__name__', fn)} failed: {e}")
            finally:
                self._q.task_done()

    def _submit(self, fn, *args):
        """Queue an OBS write. Returns IMMEDIATELY — the caller (often the audio
        path) never waits on a network round-trip."""
        self._q.put((fn, args))

    def _set_media(self, source, path):
        # Media (video) sources use "local_file".
        self.c.set_input_settings(source, {"local_file": path}, overlay=True)

    def _set_image(self, source, path):
        # Image sources use "file". Still JPG/PNG must be an image_source — a
        # media source clears to black when the single frame "ends".
        self.c.set_input_settings(source, {"file": path}, overlay=True)

    def _set_visible(self, source, on):
        item_id = self.c.get_scene_item_id(self.scene, source).scene_item_id
        self.c.set_scene_item_enabled(self.scene, item_id, on)

    def set_talking(self, talking: bool):
        # Called from the AUDIO path (voice.say's on_start/on_stop), so it must not
        # block: queued, not sent inline.
        self._submit(self._do_set_talking, talking)

    def _do_set_talking(self, talking: bool):
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

    # There is deliberately NO hide_avatar() any more.
    #
    # There used to be one, for a "nobody is watching, save the API bill" mode. It
    # hid both avatar loops. It took down a live stream: you go live, nobody has
    # joined YET so the viewer count reads 0, and Bello hides his own avatar, hides
    # the title and stops talking — so the first person who clicks in finds a blank,
    # dead stream and leaves. The show switched itself off exactly when it most
    # needed to be a show.
    #
    # The avatar IS the product. It stays on screen for as long as Bello runs.
    # If you are tempted to add this back: don't.

    def ensure_avatar_visible(self):
        """Belt and braces: whatever else happened, put an avatar back on screen."""
        self._submit(self._do_ensure_avatar_visible)

    def _do_ensure_avatar_visible(self):
        if not self._is_visible(self.talk_src) and not self._is_visible(self.idle_src):
            self._set_visible(self.idle_src, True)

    def _is_visible(self, source):
        item_id = self.c.get_scene_item_id(self.scene, source).scene_item_id
        return self.c.get_scene_item_enabled(self.scene, item_id).scene_item_enabled

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

    def bust_avatar(self, width_frac=0.5, height_frac=0.6, margin=24, drop_px=0,
                    shift_x=0):
        """Scale both avatar loops into a fixed box pinned bottom-right, using OBS
        bounds so we DON'T need the clip's pixel size (which is 0 before a frame
        decodes, and never decodes for the hidden talk clip — that was making the
        old crop-based version silently skip and leave the avatar mis-placed).

        drop_px pushes the avatar DOWN past the bottom edge, so the bottom band of
        the source video falls outside the canvas and is never rendered. That is
        how the generator's watermark (burned into the bottom of the clip, over his
        hand) is removed: it isn't covered up, it's pushed off-screen entirely.
        Tune it with obs.avatar_drop_px in config.yaml.
        """
        try:
            W, H = self._canvas()
            box_w, box_h = W * width_frac, H * height_frac
            for src in (self.talk_src, self.idle_src):
                iid = self._item_id(src)
                self.c.set_scene_item_transform(self.scene, iid, {
                    "cropTop": 0, "cropBottom": 0, "cropLeft": 0, "cropRight": 0,
                    "boundsType": "OBS_BOUNDS_SCALE_INNER",   # fit inside the box, keep aspect
                    "boundsWidth": box_w, "boundsHeight": box_h,
                    "boundsAlignment": 10,                    # anchor the box bottom-right
                    "alignment": 10,                          # bottom-right anchor
                    "positionX": W - margin + shift_x,        # + = further RIGHT (off the right edge)
                    "positionY": H - margin + drop_px,        # + = further down = more cropped off
                    "rotation": 0,
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
        if not getattr(self, "title_src", None):
            return
        self._submit(self._do_set_title, name, price)

    def _do_set_title(self, name: str, price: str = ""):
        src = self.title_src
        txt = name + (f"\nfrom {price}" if price else "")
        self.c.set_input_settings(src, {"text": txt}, overlay=True)
        self._set_visible(src, True)

    def hide_title(self):
        if not getattr(self, "title_src", None):
            return
        self._submit(self._set_visible, self.title_src, False)

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

    def apply_reel_layout(self, drop_px=0, shift_x=0):
        """One-shot: make the live scene look like the exported reel."""
        self.cover_background()
        self.bust_avatar(drop_px=drop_px, shift_x=shift_x)
        self.ensure_title()
        self._order_layers()

    def show_background(self, path: str):
        # Swap the Background image source to a specific still (one slide).
        self._set_image(self.bg_src, abspath(path))

    def demo_background(self):
        if self.demos:
            self._submit(self._set_image, self.bg_src, random.choice(self.demos))

    def ensure_music(self, music_cfg: dict):
        """Set up looping background music + voice ducking. Called once at startup
        so a fresh box gets it automatically; a no-op if music_cfg is empty."""
        try:
            setup_music(self.c, self.scene, music_cfg)
        except Exception as e:
            print(f"[obs] background music setup skipped: {e}")

    def ensure_language_ticker(self, cfg: dict):
        """Create (if missing) a scrolling ticker along the bottom that advertises
        every language Bello can speak. Idempotent + guarded, so a fresh box gets it
        automatically and a missing OBS feature never crashes the stream. cfg is the
        obs.language_ticker block; None or enabled:false -> off."""
        if not cfg or not cfg.get("enabled", True):
            return
        self._submit(self._do_ensure_language_ticker, cfg)

    def _do_ensure_language_ticker(self, cfg: dict):
        # A text source + a Scroll filter is OBS's native marquee: the filter loops
        # the text so it never runs out, giving a continuous ticker. FreeType2 only
        # renders glyphs the chosen `font` actually has — the default text is Latin
        # so it always shows; native scripts need a Unicode font (see config.yaml).
        name = cfg.get("source_name", "LanguageTicker")
        self.ticker_src = name
        text = cfg.get("text") or ("Bello speaks your language:    English    •    "
                                   "Arabic    •    Chinese    •    Russian          ")
        try:
            W, H = self._canvas()
            font = {"face": cfg.get("font", "Arial"),
                    "size": int(cfg.get("font_size", max(24, int(H * 0.02)))),
                    "style": cfg.get("font_style", "Bold")}
            color = int(cfg.get("color", 0xFFFFFFFF))
            settings = {"text": text, "font": font,
                        "color1": color, "color2": color, "outline": True}
            existing = {i["inputName"] for i in self.c.get_input_list().inputs}
            if name not in existing:
                self.c.create_input(self.scene, name, "text_ft2_source_v2",
                                    settings, True)
            else:
                self.c.set_input_settings(name, settings, overlay=True)
            # The Scroll filter is the movement. loop repeats the text so the band is
            # never empty; speed_x is px/sec (sign = direction).
            fsettings = {"speed_x": float(cfg.get("speed_x", 100.0)),
                         "speed_y": 0.0, "loop": True}
            try:
                have = {f["filterName"]
                        for f in self.c.get_source_filter_list(name).filters}
            except Exception:
                have = set()
            if "Scroll" in have:
                self.c.set_source_filter_settings(name, "Scroll", fsettings)
            else:
                self.c.create_source_filter(name, "Scroll", "scroll_filter", fsettings)
            # Bottom band, anchored top-left at (0, y_frac*H) so it spans the width.
            iid = self._item_id(name)
            self.c.set_scene_item_transform(self.scene, iid, {
                "positionX": 0, "positionY": int(H * float(cfg.get("y_frac", 0.94))),
                "alignment": 5})
            self._set_visible(name, True)
            # Keep the ticker on TOP of everything else on screen.
            try:
                n = len(self.c.get_scene_item_list(self.scene).scene_items)
                self.c.set_scene_item_index(self.scene, iid, max(0, n - 1))
            except Exception:
                pass
            print(f"[obs] language ticker '{name}' ready.")
        except Exception as e:
            print(f"[obs] language ticker skipped: {e}")
