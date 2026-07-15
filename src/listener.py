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

    def _make_client(self):
        """Build a FRESH client with handlers attached.

        A TikTokLiveClient whose connect() failed — which is exactly what happens
        when the app starts BEFORE the account goes live — does not recover if you
        just call connect() again on it: it stays wedged on UserOfflineError even
        after the account is live and the room is genuinely reachable (verified: a
        fresh client connects and receives comments in the same instant the old one
        keeps reporting 'offline'). So every reconnect attempt gets a brand new
        client. The account went live AFTER Bello started, and this is what let him
        keep answering nobody.
        """
        client = TikTokLiveClient(unique_id=self.username)

        @client.on(CommentEvent)
        async def _on_comment(e: CommentEvent):
            await self.q.put(("comment", e.user.nickname, e.comment))

        @client.on(JoinEvent)
        async def _on_join(e: JoinEvent):
            name = getattr(e.user, "nickname", None)
            if name and len(name) > 1:          # drop single-char artifacts
                await self.q.put(("join", name, None))

        # Current live viewer count -> presence. TikTok pushes this every few
        # seconds. `m_total` is the LIVE count (drops to 0 when empty); avoid
        # `total_user`, which is cumulative and never falls back to 0. Field name
        # varies by TikTokLive version, so probe a few candidates.
        if RoomUserSeqEvent is not None:
            @client.on(RoomUserSeqEvent)
            async def _on_seq(e):
                n = next((getattr(e, a, None)
                          for a in ("m_total", "total", "viewer_count")
                          if getattr(e, a, None) is not None), None)
                if n is not None:
                    await self.q.put(("viewers", int(n), None))

        return client

    async def run(self):
        # Never die: stream ends, account goes off/on air, network blips — reconnect
        # with a FRESH client each time (see _make_client). Retry fast (5s) when the
        # account is simply not live yet, so Bello starts answering within seconds
        # of the account going live rather than up to 15s later.
        while True:
            client = self._make_client()
            try:
                await client.connect()
            except Exception as e:
                print(f"[listener] not connected ({type(e).__name__}); retrying in 5s...")
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(5)
