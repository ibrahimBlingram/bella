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
import time
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

# EulerStream sign key — big reliability boost. WITHOUT it, TikTokLive uses the
# public sign server, which rate-limits hard and keeps dropping the comment feed.
#
# CAUTION: do NOT rely on reading the key HERE at import time. main.py runs
# `from listener import Listener` BEFORE it calls load_dotenv(), so at import the
# key is not in the environment yet and this silently falls back to the public
# server. The key is therefore (re)applied in Listener.__init__, which runs after
# load_dotenv(). This line only helps if the key was already exported in the shell.
_KEY = os.environ.get("EULERSTREAM_API_KEY")
if _KEY:
    WebDefaults.tiktok_sign_api_key = _KEY


# A healthy room delivers events (viewer counts, joins, comments) every few
# seconds — measured ~5 in 15s even with almost nobody watching. So if NOTHING
# arrives for this long, the socket is half-open/dead: this is what happens when
# the TikTok live is stopped and restarted — the old room dies, but connect()
# keeps blocking on it forever, never erroring, never reconnecting, never
# delivering another event. When that silence trips, we force a reconnect so a
# fresh client finds the NEW room.
#
# Kept generous ON PURPOSE. Every reconnect needs a fresh EulerStream SIGNATURE,
# and the free key is rate-limited — reconnect too eagerly and you exhaust the
# quota (SignatureRateLimitError), after which you can't connect at all. A long
# threshold means we only reconnect when the connection is genuinely dead, not
# during a normal quiet spell, so we spend signatures sparingly.
_STALL_SECONDS = 90.0

# Backoff before the NEXT connect attempt. EulerStream's free key throttles
# repeated SIGNATURES of the SAME room, and the throttle RENEWS on each attempt —
# so a short retry keeps it locked out forever (each retry resets the clock). The
# cool-off must be LONGER than the throttle window (~2-3 min observed) so the room
# clears BETWEEN attempts. This only bites on (re)connect; a live connection holds
# on one signature and never re-signs. Ordinary "not live yet" retries stay quick.
_BACKOFF_RATELIMIT = 30.0
_BACKOFF_NORMAL = 5.0

# How often to POLL is_live() while the account is offline. is_live() is a cheap
# room-info lookup that does NOT spend the Sign API's "connections started"
# budget — unlike connect(). We only ever call the (budget-spending) connect()
# once is_live() is true, so the offline wait can never hammer the signer. This
# is the fix for the failure where, in the ~minute after the broadcast starts and
# before TikTok registers the live, the old 5s connect-retry loop burned through
# the sign limit and then couldn't connect AT ALL once the account went live.
_OFFLINE_POLL = 15.0


class Listener:
    def __init__(self, username: str, queue):
        if not username.startswith("@"):
            username = "@" + username          # TikTokLive wants the @handle
        self.username = username
        self.q = queue
        self._last_rx = 0.0                     # monotonic time of last event
        self._last_err = None                   # type name of last connect error

        # Apply the EulerStream key NOW (after main.py's load_dotenv), so the live
        # listener actually uses it instead of the rate-limited public sign server.
        # This is the fix for comments only connecting intermittently: on the key's
        # own quota a single connect succeeds and HOLDS, delivering comments in real
        # time, instead of the public server dropping every few seconds.
        key = os.environ.get("EULERSTREAM_API_KEY")
        if key:
            WebDefaults.tiktok_sign_api_key = key
            print(f"[listener] EulerStream key active (…{key[-6:]}) — using the key's "
                  f"own quota, not the public sign server.")
        else:
            print("[listener] WARNING: no EULERSTREAM_API_KEY in env — falling back to "
                  "the public sign server, which drops the comment feed often.")

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
            self._last_rx = time.monotonic()
            await self.q.put(("comment", e.user.nickname, e.comment))

        @client.on(JoinEvent)
        async def _on_join(e: JoinEvent):
            self._last_rx = time.monotonic()
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
                self._last_rx = time.monotonic()
                n = next((getattr(e, a, None)
                          for a in ("m_total", "total", "viewer_count")
                          if getattr(e, a, None) is not None), None)
                if n is not None:
                    await self.q.put(("viewers", int(n), None))

        return client

    async def _connect(self, client):
        try:
            await client.connect()              # blocks until disconnected
            self._last_err = None               # ended cleanly (we disconnected it)
        except Exception as e:
            self._last_err = type(e).__name__
            rl = "RateLimit" in self._last_err
            wait = int(_BACKOFF_RATELIMIT if rl else _BACKOFF_NORMAL)
            if rl:
                # str(e) carries the server's own "try again in N seconds" message.
                print(f"[listener] sign rate-limited ({str(e)[:120]}) — backing off "
                      f"{wait}s. Free EulerStream key; a paid key removes this limit.")
            else:
                print(f"[listener] not connected ({self._last_err}); retrying in {wait}s")

    async def run(self):
        # Never die: stream ends, account goes off/on air, network blips — reconnect
        # with a FRESH client each time (see _make_client).
        #
        # A stall WATCHDOG guards the case connect() cannot: once connected, it
        # blocks forever, so if the room silently dies (the live was restarted) it
        # would hang there receiving nothing. We watch the event clock and, on
        # silence, disconnect to force a fresh connection to the CURRENT room —
        # but sparingly (see _STALL_SECONDS), because every reconnect spends a
        # rate-limited EulerStream signature.
        self._last_err = None
        while True:
            client = self._make_client()

            # CHEAP liveness gate. is_live() does NOT spend the Sign API's
            # "connections started" budget the way connect() does, so we can poll
            # it freely. Only call the budget-spending connect() once TikTok says
            # the account is actually live — this is what stops the offline-retry
            # loop from hammering the signer into a rate-limit while TikTok is
            # still registering a freshly-started broadcast.
            try:
                live = await client.is_live()
            except Exception:
                live = True          # probe failed — fall through and try connecting
            if not live:
                await asyncio.sleep(_OFFLINE_POLL)
                continue

            self._last_rx = time.monotonic()
            conn = asyncio.create_task(self._connect(client))

            while not conn.done():
                await asyncio.sleep(5)
                if time.monotonic() - self._last_rx > _STALL_SECONDS:
                    print(f"[listener] no events for {int(_STALL_SECONDS)}s — stale "
                          f"connection (live likely restarted); forcing reconnect")
                    try:
                        await client.disconnect()   # unblocks connect() -> conn ends
                    except Exception:
                        pass
                    break

            if not conn.done():
                conn.cancel()
            try:
                await conn
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await client.disconnect()
            except Exception:
                pass
            # Long cool-off after a signature rate-limit; quick retry otherwise.
            rl = self._last_err and "RateLimit" in self._last_err
            await asyncio.sleep(_BACKOFF_RATELIMIT if rl else _BACKOFF_NORMAL)
