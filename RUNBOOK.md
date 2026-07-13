# Bello on Vast.ai — operating runbook

Everything runs on the **Vast.ai GPU server**. Your laptop is only used to SSH in
and to open a browser tab. Nothing about the live stream depends on your Mac.

---

## 1. Connect

```bash
ssh -p 27252 root@76.121.3.151
```

(Or use the **Jupyter terminal** from the Vast dashboard — same thing, no SSH needed.)

Everything below assumes:

```bash
cd /workspace/bella
```

---

## 2. The three commands you actually need

| What | Command |
| --- | --- |
| **Start everything** (display, audio, OBS, Bello) | `bash scripts/start_all.sh` |
| **Go live** | `bash scripts/stream.sh start` |
| **Stop the broadcast** | `bash scripts/stream.sh stop` |

Supporting commands:

```bash
bash scripts/stream.sh status     # am I live? dropped frames? destination?
tail -f /tmp/bello.log            # watch what Bello is doing
pkill -f src/main.py              # stop Bello (but leave OBS up)
```

**`start_all.sh` does NOT go live.** It brings the machine up and starts Bello
talking into OBS. Broadcasting is a separate, deliberate step — `stream.sh start`.
That separation is on purpose: you never go live by accident.

---

## 3. Watching / editing OBS from a browser

OBS has no monitor on the server, so it runs on a *virtual* display. To see and
click it, tunnel in and open a browser tab:

```bash
# on YOUR laptop, in a terminal:
ssh -f -N -L 8081:localhost:16006 -p 27252 root@76.121.3.151
```

Then open: **http://localhost:8081/vnc.html** → Connect → password `bello388`

You now have the real OBS window. Change sources, move the avatar, edit
Settings → Stream, anything. **Changes persist** — OBS saves them, and they
survive a restart.

> Why `localhost:8081` and not the public IP? Corporate networks block odd
> ports on raw IPs. Tunnelling means your browser only ever talks to your own
> machine, and the traffic rides inside SSH. Nothing to block.

If the tunnel dies (laptop sleeps, wifi drops), just re-run the `ssh -f -N -L`
line. There's also a public URL, but many networks block it.

---

## 4. How the whole thing is wired

```
                    VAST.AI SERVER (RTX 3060, 12 cores)
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │  src/main.py                                                 │
  │    ├─ listener.py ──── TikTok live chat (comments, joins)    │
  │    ├─ brain.py ─────── Gemini: what Bello SAYS               │
  │    ├─ voice.py ─────── Chatterbox on the GPU: his VOICE      │
  │    │                     ↓ audio                             │
  │    │                  PulseAudio "bella_audio" sink          │
  │    │                     ↓                                   │
  │    └─ obs_control.py ─→ OBS ←── captures that audio sink     │
  │                          │                                   │
  │                          │  composites the scene:            │
  │                          │   • Background (project photos)   │
  │                          │   • AvatarIdle / AvatarTalk       │
  │                          │   • BellaTitle (name + price)     │
  │                          ↓                                   │
  │                       NVENC (GPU encoder)                    │
  └──────────────────────────┬───────────────────────────────────┘
                             ↓ RTMP
                          Restream  →  TikTok
```

**Why each piece is the way it is:**

- **Xvfb (virtual display `:99`)** — OBS is a GUI app and refuses to start
  without a display, even though nobody is looking at it.
- **Software OpenGL (llvmpipe)** — Vast's GPU is a *compute* card with no display
  engine, so OBS can't render on it. It composites on the **CPU** instead. This is
  why we rented a box with 12 cores: the CPU is the bottleneck, not the GPU.
- **NVENC** — video *encoding* still happens on the GPU (it's a separate chip
  block from the CUDA cores, and it doesn't need a display). That's what keeps the
  CPU free enough to composite. Measured: 0% dropped frames at ~45% CPU.
- **PulseAudio null sink** — there's no sound card. TTS writes to a fake sink and
  OBS records that sink's monitor. Without it, OBS records **silence**.
- **Chatterbox on CUDA** — two models: **Turbo** for English (the only engine that
  performs `[laugh]`/`[chuckle]` as real *sounds*) and **Multilingual** for Arabic
  (same cloned voice). Uses ~3 GB of the 12 GB VRAM. Synthesizes at 0.29× realtime,
  i.e. 3× faster than it speaks, so it never falls behind.

---

## 5. Things that will go wrong, and the fix

### Bello says "Hang tight, I'll be right back" over and over
**Gemini quota is exhausted.** The free tier allows 20 requests/day; a live stream
burns that in minutes. The brain catches the error and speaks a filler line rather
than crashing.

**Fix:** enable billing on the Google Cloud project behind `GEMINI_API_KEY`. It's
pay-as-you-go, not a subscription. Then `pkill -f src/main.py` and
`bash scripts/start_all.sh`.

Check it:
```bash
grep -c "giving up after error" /tmp/bello.log     # >0 means quota problems
```

### Bello has a woman's voice
The Chatterbox clone reference is missing, so it falls back to its stock voice —
which is female. Confirm:
```bash
grep "ref not found" /tmp/bello.log
```
**Fix:** `voice_samples/bella_ref_en.wav` must exist (it's committed to the repo).
To use a *different* voice, drop a clean 5–10 s mono clip there and restart.
For Arabic, add `voice_samples/bella_ref_ar.wav` — without it, Arabic borrows the
English reference and the accent bleeds.

### OBS won't start / crashes
Almost always **missing Mesa drivers** — OBS has no OpenGL and dies instantly.
```bash
DISPLAY=:99 glxinfo -B | grep "OpenGL renderer"     # must say llvmpipe
```
**Fix:** `apt-get install -y mesa-utils libgl1-mesa-dri libglx-mesa0 libegl1`

### The stream is stuttering / dropping frames
```bash
bash scripts/stream.sh status      # look at the dropped-frame %
```
The compositor runs on the CPU, so a big canvas is expensive. If frames are
dropping, lower **Settings → Video → Base Resolution** to `720x1280` and FPS to
`20` (via noVNC). That's a 3.4× reduction in CPU work and looks fine on a phone.

### The stream is silent
The audio sink died.
```bash
pactl list short sinks | grep bella_audio       # must exist
```
**Fix:** `bash scripts/start_all.sh` (it recreates the sink). Then check OBS's
`BellaAudio` source isn't muted.

### "Can't reach OBS on :4455"
OBS isn't running. **Fix:** `bash scripts/start_all.sh`

### I stopped the instance and now nothing works
Normal. A Vast **stop/start** keeps all your *files* but kills every *process*.
**Fix:** `bash scripts/start_all.sh` — one command, back in business.

> ⚠️ **Never hit "Destroy" or "Recycle"** on the Vast dashboard. Those wipe the
> container: you'd lose the installed packages and the OBS scene and have to
> redo the whole setup. Only `/workspace` survives. **"Stop" is the safe one.**

---

## 6. Costs — don't forget these

- The instance bills **~$0.089/hr (~$64/month)** whenever it is *running*, even
  when Bello is idle and not streaming. **Stop the instance** on the Vast
  dashboard when you're not using it.
- **Bandwidth is billed separately.** A 24/7 stream at 6 Mbps is roughly **2 TB/month**.
  Check the per-GB rate on your instance — this can cost more than the GPU.
- **Gemini** bills per request once billing is on.
