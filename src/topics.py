"""
topics.py — no-repeat topic queue for idle narration.

Shuffles the theme's topics, hands them out one at a time, and refills+reshuffles
when exhausted. `covered` tracks what's been narrated today and is fed back into
the LLM prompt so even reworded segments don't repeat their points.
"""
import random


class TopicQueue:
    def __init__(self, topics: list[str]):
        self.all = list(topics)
        self.remaining: list[str] = []
        self.covered: list[str] = []

    def next(self) -> str:
        if not self.remaining:
            self.remaining = self.all[:]
            random.shuffle(self.remaining)
        return self.remaining.pop()

    def mark(self, topic: str):
        self.covered.append(topic)

    def reset_day(self):
        self.covered.clear()
