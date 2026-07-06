"""
brain.py — the AI. Gemini Flash, streaming, RAG context, grounding off by default.

Streams tokens and re-chunks them into whole SENTENCES so the TTS can start
speaking sentence 1 while the model is still writing sentence 2.

Hardened for a 24/7 stream:
  - ALWAYS answers in English (Kokoro only does English well).
  - A transient Gemini error (503 high-demand, 429, etc.) RETRIES instead of
    crashing; if it still fails it speaks a short filler and moves on.
"""
import asyncio
import re

from google import genai
from google.genai import types

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
    def __init__(self, cfg, persona, knowledge):
        self.client = genai.Client()                       # reads GEMINI_API_KEY
        self.model = cfg["llm"]["model"]
        self.max_tokens = cfg["llm"]["max_output_tokens"]
        self.temperature = cfg["llm"]["temperature"]
        self.allow_grounding = cfg["llm"]["grounding"]
        self.persona = persona
        # System instruction = persona rules + a hard English rule + RAG knowledge.
        self.system = (
            persona["system_prompt"]
            + "\n\nLANGUAGE: Reply in the language the current prompt tells you to "
              "use. Default to English. Never mix two languages in one reply."
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
                from sobha import SobhaData
                self.retriever = SobhaData(ds)
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

    def _gen_config(self, ground: bool):
        tools = [types.Tool(google_search=types.GoogleSearch())] if ground else None
        return types.GenerateContentConfig(
            system_instruction=self.system,
            max_output_tokens=self.max_tokens,
            temperature=self.temperature,
            tools=tools,
            # gemini-2.5-flash "thinks" by default, and thinking tokens count
            # against max_output_tokens — starving the actual reply. Off = all
            # tokens go to the spoken answer.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )

    async def _stream_sentences(self, prompt: str, ground: bool):
        """Yield whole sentences. Retries transient failures; never raises up to caller."""
        for attempt in range(_MAX_ATTEMPTS):
            buf = ""
            yielded_any = False
            try:
                sync_stream = self.client.models.generate_content_stream(
                    model=self.model, contents=prompt, config=self._gen_config(ground)
                )
                async for chunk in _aiter(sync_stream):
                    text = getattr(chunk, "text", None)
                    if not text:
                        continue
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
        """Async iterator of spoken sentences answering a chat comment."""
        ground = self.allow_grounding and bool(_NEEDS_WEB.search(question))
        langname = "Arabic" if lang == "ar" else "English"
        prompt = (
            f'A viewer in the live chat just said: "{question}".\n'
            f"Reply as Bello in {langname}, in 1-2 short, lively spoken sentences. "
            f"Be specific and use real Blingram facts from your knowledge whenever "
            f"they apply. If you genuinely don't know a fact, say the team will "
            f"confirm — don't invent."
        )
        prompt += self._retrieve(question)
        async for s in self._stream_sentences(prompt, ground):
            yield s

    async def narrate(self, topic: str, covered: list[str]):
        """Async iterator of spoken sentences for an idle-time segment."""
        prompt = self.persona["narration_prompt"].format(
            topic=topic,
            covered="; ".join(covered[-12:]) or "(nothing yet)",
        ) + "\nReply in English."
        prompt += self._retrieve(topic)
        async for s in self._stream_sentences(prompt, ground=False):
            yield s

    async def narrate_project(self, name: str, facts: str, covered: list[str]):
        """Spoken spotlight promoting one Sobha development (its images are on
        screen). Uses ONLY the supplied facts; never invents prices/numbers."""
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
            f'Reply in English.\n\n=== PROJECT FACTS ===\n{facts}'
        )
        async for s in self._stream_sentences(prompt, ground=False):
            yield s
