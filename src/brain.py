"""
brain.py — the AI. Streaming, RAG context, and a provider switch.

Streams tokens and re-chunks them into whole SENTENCES so the TTS can start
speaking sentence 1 while the model is still writing sentence 2.

Two providers, chosen by `llm.provider` in config.yaml:

  groq    (default) — Groq's hosted Llama. Fast and cheap: ~0.2s to first token
                      vs ~1s for Gemini, which matters when a viewer is waiting
                      for a reply. Speaks Arabic and English.
  gemini            — Google Gemini Flash. The original. Its free tier is 20
                      requests/DAY, which a live stream exhausts in minutes, so
                      it needs billing enabled to be usable at all.

Only Gemini can do grounded web search (`llm.grounding`); on Groq that setting is
ignored, since Groq has no search tool. Everything else — the sentence streaming,
the retry policy, the RAG retrieval — is shared.

Hardened for a 24/7 stream: a transient error (429 rate limit, 5xx) RETRIES
instead of crashing; if it still fails, Bello speaks a short filler and moves on.
"""
import asyncio
import os
import re

_SENT_END = re.compile(r"(?<=[.!?])\s+")

# Only burn a grounded web search when the question clearly needs the live web.
_NEEDS_WEB = re.compile(
    r"\b(today|right now|latest|news|price|weather|score|who won|currently|2026)\b",
    re.IGNORECASE,
)

# Errors worth retrying (server overloaded / rate limited / gateway).
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3

# Arabic detection: any Arabic-script character in the comment.
_ARABIC = re.compile(r"[\u0600-\u06FF]")


def is_arabic(text: str) -> bool:
    return bool(_ARABIC.search(text or ""))


# One place that says HOW to write each language. Arabic is the channel's primary
# language and must sound like a polished Dubai presenter — clear, fluent Modern
# Standard Arabic, so the TTS pronounces every word cleanly. No English words or
# Latin letters (the Arabic voice would mangle them), and no tashkeel/diacritics
# (the model reads undiacritised text more naturally).
def reply_language_clause(lang: str) -> str:
    if lang == "ar":
        return ("Reply ONLY in clear, fluent Modern Standard Arabic (الفصحى), the "
                "way a professional Dubai real-estate presenter speaks — warm, "
                "natural and easy to understand. Use complete, well-punctuated "
                "sentences so every word is pronounced clearly. Do NOT use any "
                "English words, Latin letters, transliteration, or tashkeel.")
    return "Reply in English."


async def _aiter(sync_gen):
    """Drive a blocking generator from async land without freezing the loop."""
    while True:
        item = await asyncio.to_thread(next, sync_gen, None)
        if item is None:
            return
        yield item


def _is_retryable(e: Exception) -> bool:
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in _RETRYABLE:
        return True
    s = str(e)
    return any(tok in s for tok in ("503", "429", "UNAVAILABLE", "overloaded", "high demand"))


class Brain:
    def __init__(self, cfg, persona, knowledge, performs_tags: bool = False):
        llm = cfg["llm"]
        self.provider = (llm.get("provider") or "groq").lower()
        self.model = llm["model"]
        self.max_tokens = llm["max_output_tokens"]
        self.temperature = llm["temperature"]
        # Grounded web search is a Gemini-only tool. Groq has no search, so the
        # setting is ignored there rather than silently pretending to work.
        self.allow_grounding = bool(llm.get("grounding")) and self.provider == "gemini"
        self.persona = persona

        if self.provider == "groq":
            from groq import Groq
            if not os.environ.get("GROQ_API_KEY"):
                raise RuntimeError("GROQ_API_KEY not set in .env")
            self.client = Groq()                   # reads GROQ_API_KEY
        elif self.provider == "gemini":
            from google import genai
            self.client = genai.Client()           # reads GEMINI_API_KEY
        else:
            raise ValueError(f"llm.provider must be 'groq' or 'gemini', got {self.provider!r}")
        print(f"[brain] {self.provider} / {self.model}")
        # Only Chatterbox Turbo PERFORMS [laugh]/[chuckle]/[sigh] as real sounds;
        # every other engine speaks the literal word. So the instruction to write
        # them is added ONLY when the English voice can actually perform them —
        # otherwise Bello says "laugh" out loud on the live stream.
        # (voice.say() also strips them per-engine as a second safety net.)
        laughter = persona.get("expressive_tags", "") if performs_tags else ""
        # System instruction = persona rules + a hard English rule + RAG knowledge.
        self.system = (
            persona["system_prompt"]
            + (f"\n\n{laughter.strip()}" if laughter else "")
            + "\n\nLANGUAGE: Reply in the language the current prompt tells you to "
              "use. ARABIC is this channel's PRIMARY language — when you write "
              "Arabic, use clear, fluent Modern Standard Arabic (الفصحى) as a "
              "professional Dubai presenter speaks: warm, natural, easy to follow, "
              "no English words or Latin letters, no tashkeel. Never mix two "
              "languages in one reply."
            + "\n\nDELIVERY: You are spoken aloud by an energetic TTS voice. Write "
              "like an excited, upbeat host — use natural exclamation marks and "
              "lively phrasing so you sound enthusiastic and warm. Don't put an "
              "exclamation on every sentence; keep it natural."
            + "\n\n=== PRODUCT & THEME KNOWLEDGE (treat as fact) ===\n"
            + knowledge
        )
        # On-demand full-record retrieval (all floor plans, FAQs, currencies).
        # The compact index is always-on in `knowledge`; this adds the complete
        # record for whichever project the current prompt is about.
        self.retriever = None
        ds = (cfg.get("data") or {}).get("sobha_dataset")
        if ds:
            try:
                from paths import abspath
                from sobha import SobhaData
                self.retriever = SobhaData(abspath(ds))
            except Exception as e:
                print(f"[brain] Sobha retrieval disabled ({e}).")

    def _retrieve(self, text: str) -> str:
        if not self.retriever:
            return ""
        detail = self.retriever.match(text)
        if not detail:
            return ""
        return ("\n\n=== FULL PROJECT DATA (use these EXACT facts; do not invent "
                "beyond them) ===\n" + detail)

    def _gemini_stream(self, prompt: str, ground: bool, max_tokens: int):
        """Blocking generator of raw text chunks from Gemini."""
        from google.genai import types
        tools = [types.Tool(google_search=types.GoogleSearch())] if ground else None
        cfg = types.GenerateContentConfig(
            system_instruction=self.system,
            max_output_tokens=max_tokens,
            temperature=self.temperature,
            tools=tools,
            # gemini-2.5-flash "thinks" by default, and thinking tokens count
            # against max_output_tokens — starving the actual reply. Off = all
            # tokens go to the spoken answer.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        for chunk in self.client.models.generate_content_stream(
                model=self.model, contents=prompt, config=cfg):
            text = getattr(chunk, "text", None)
            if text:
                yield text

    def _groq_stream(self, prompt: str, ground: bool, max_tokens: int):
        """Blocking generator of raw text chunks from Groq (OpenAI-shaped API).
        `ground` is ignored: Groq has no web-search tool."""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": self.system},
                      {"role": "user", "content": prompt}],
            max_completion_tokens=max_tokens,
            temperature=self.temperature,
            stream=True,
        )
        for chunk in stream:
            # delta.content is None on the opening/closing chunks — skip, don't crash.
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    def _tokens(self, prompt: str, ground: bool, max_tokens: int = None):
        mt = max_tokens or self.max_tokens
        return (self._groq_stream if self.provider == "groq"
                else self._gemini_stream)(prompt, ground, mt)

    async def _stream_sentences(self, prompt: str, ground: bool, max_tokens: int = None):
        """Yield whole sentences. Retries transient failures; never raises up to caller."""
        for attempt in range(_MAX_ATTEMPTS):
            buf = ""
            yielded_any = False
            try:
                async for text in _aiter(self._tokens(prompt, ground, max_tokens)):
                    buf += text
                    parts = _SENT_END.split(buf)
                    if len(parts) > 1:
                        for s in parts[:-1]:
                            if s.strip():
                                yielded_any = True
                                yield s.strip()
                        buf = parts[-1]
                if buf.strip():
                    yield buf.strip()
                return  # clean finish
            except Exception as e:
                # Failed mid-answer after already speaking -> stop gracefully.
                if yielded_any:
                    return
                # Failed before any output -> retry if transient, else give up softly.
                if attempt < _MAX_ATTEMPTS - 1 and _is_retryable(e):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                print(f"[brain] giving up after error: {type(e).__name__}: {e}")
                yield "Hang tight, I'll be right back."
                return

    async def answer(self, question: str, lang: str = "en"):
        """Async iterator of spoken sentences answering a chat comment.

        Most comments on this stream are about the properties, and a property
        question is a LEAD — so the answer has to be specific and, above all,
        accurate. A wrong price is worse than no price."""
        ground = self.allow_grounding and bool(_NEEDS_WEB.search(question))
        if lang == "ar":
            # Arabic is the PRIMARY language now, so it gets a full, clear answer —
            # not the old one-line cap. 1-2 complete, fluent MSA sentences: enough to
            # actually answer the question and sound like a real Dubai presenter. The
            # Arabic voice is slower to synthesize, so we keep it to 1-2 sentences (not
            # a paragraph) to stay responsive — but clarity comes first.
            length_rule = ("Reply as Bello in 1-2 complete, fluent spoken sentences. "
                           + reply_language_clause("ar"))
            max_tokens = 220
        else:
            length_rule = ("Reply as Bello in English, in 1-2 short, lively spoken "
                           "sentences.")
            max_tokens = self.max_tokens
        prompt = (
            f'A viewer in the live chat just said: "{question}".\n'
            f"{length_rule}\n"
            f"If they asked about a Sobha project, ANSWER IT — the price, the size, "
            f"the amenities, who it suits — using ONLY the project facts you have. "
            f"Quote prices as starting prices ('from AED ...'), never as a final "
            f"figure.\n"
            f"If you don't have the number they asked for, say so warmly and tell "
            f"them the Sobha team will confirm. NEVER invent a price, a size, a "
            f"handover date or a payment plan — a viewer might act on it.\n"
            f"If it's not about property at all, be charming, keep it short, and "
            f"bring it back to what's on screen."
        )
        prompt += self._retrieve(question)
        async for s in self._stream_sentences(prompt, ground, max_tokens=max_tokens):
            yield s

    # Openers to FORCE variety on the Dubai segments. Left to itself the model
    # begins almost every one the same way ("Dubai's real estate market is...",
    # "Let me tell you about..."), which is what viewers heard as "the same sentence
    # every time". A different required opening style each call breaks that.
    _OPENERS = [
        "Open with a genuinely surprising number or fact — no throat-clearing.",
        "Open with a joke or a playful hot take, THEN make the point.",
        "Open mid-thought, like you're continuing a conversation — 'okay so...'.",
        "Open by teasing the viewer ('bet you didn't know...').",
        "Open with a bold one-liner claim, then back it up.",
        "Open with a 'here's the thing nobody tells you' reveal.",
        "Open by reacting to Dubai like you still can't believe it exists.",
        "Open with a quick 'imagine this...' scenario.",
    ]

    async def narrate(self, topic: str, covered: list[str], lang: str = "en"):
        """Async iterator of spoken sentences for an idle-time (Dubai) segment.
        `lang` picks the delivery language (en | ar). The English _OPENERS are
        instructions to the model, not output — it still replies in `lang`."""
        # Vary the index by how much has been covered so it walks the list rather
        # than risk repeating (Math.random is unavailable in some sandboxes anyway).
        opener = self._OPENERS[len(covered) % len(self._OPENERS)]
        prompt = self.persona["narration_prompt"].format(
            topic=topic,
            covered="; ".join(covered[-12:]) or "(nothing yet)",
        ) + f"\n{opener}\nBe genuinely funny — this is entertainment between listings, "
        prompt += ("not a lecture. Never start the same way you did last time. "
                   + reply_language_clause(lang))
        prompt += self._retrieve(topic)
        async for s in self._stream_sentences(prompt, ground=False):
            yield s

    async def narrate_project(self, name: str, facts: str, covered: list[str],
                              lang: str = "en"):
        """Spoken spotlight promoting one Sobha development (its images are on
        screen). Uses ONLY the supplied facts; never invents prices/numbers.
        `lang` picks the reply language (en | ar)."""
        prompt = (
            f'Do a lively 2-3 sentence spoken spotlight on the Sobha Realty '
            f'development "{name}". Its photos are on screen right now, so paint '
            f'the lifestyle and make it sound desirable. Drop ONE concrete hook '
            f'(a starting price, a standout amenity, or who it is perfect for), '
            f'and end with a light nudge to drop a comment or ask a question. '
            f'Warm, excited, and fun — a great host, not a hard sell.\n'
            f'Use ONLY the facts below; never invent prices, numbers, or features.\n'
            f'Do NOT repeat points already covered today: '
            f'{"; ".join(covered[-12:]) or "(nothing yet)"}.\n'
            f'{reply_language_clause(lang)}\n\n=== PROJECT FACTS ===\n{facts}'
        )
        async for s in self._stream_sentences(prompt, ground=False):
            yield s
