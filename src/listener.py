"""
listener.py — TikTokLive wrapper with auto-reconnect.

Pushes (kind, name, text) tuples onto a queue.
  kind = "join" | "comment"          -> viewer activity
  kind = "viewers" (name = count)    -> current live viewer count (presence)

Requirements:
  - The target account must be LIVE for connect() to succeed.
  - A free EulerStream key (EULERSTREAM_API_KEY in .env) is strongly recommended
    for reliable long-running connections; without it the public sign server is
    rate-limited and drops more often.
"""
import os
import asyncio

from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent, JoinEvent
from TikTokLive.client.web.web_settings import WebDefaults

# Viewer-count event; name/fields vary a little across TikTokLive versions, so
# import defensively and probe attributes at runtime.
try:
    from TikTokLive.events import RoomUserSeqEvent
except Exception:                       # pragma: no cover - version fallback
    RoomUserSeqEvent = None

# Optional: raise rate limits / reliability with a free EulerStream key.
_KEY = os.environ.get("EULERSTREAM_API_KEY")
if _KEY:
    WebDefaults.tiktok_sign_api_key = _KEY


class Listener:
    def __init__(self, username: str, queue):
        if not username.startswith("@"):
            username = "@" + username          # TikTokLive wants the @handle
        self.username = username
        self.q = queue
        self.client = TikTokLiveClient(unique_id=username)

        @self.client.on(CommentEvent)
        async def _on_comment(e: CommentEvent):
            await self.q.put(("comment", e.user.nickname, e.comment))

        @self.client.on(JoinEvent)
        async def _on_join(e: JoinEvent):
            name = getattr(e.user, "nickname", None)
            if name and len(name) > 1:          # drop single-char artifacts
                await self.q.put(("join", name, None))

        # Current live viewer count -> presence. TikTok pushes this every few
        # seconds. `m_total` is the LIVE count (drops to 0 when empty); avoid
        # `total_user`, which is cumulative and never falls back to 0. Field name
        # varies by TikTokLive version, so probe a few candidates.
        if RoomUserSeqEvent is not None:
            @self.client.on(RoomUserSeqEvent)
            async def _on_seq(e):
                n = next((getattr(e, a, None)
                          for a in ("m_total", "total", "viewer_count")
                          if getattr(e, a, None) is not None), None)
                if n is not None:
                    await self.q.put(("viewers", int(n), None))

    async def run(self):
        # Never die: stream ends, account goes off/on air, network blips — reconnect.
        # (If connect() returns instantly on your TikTokLive version, swap it for
        #  `await self.client.start()`.)
        while True:
            try:
                await self.client.connect()
            except Exception as e:
                print(f"[listener] disconnected ({e}); retrying in 15s...")
            await asyncio.sleep(15)
