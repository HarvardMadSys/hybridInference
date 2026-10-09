"""Detect which natural language a user message is written in.

Messages sent through coding agents mix prose with code, paths, markup and
pasted English boilerplate, and a whole-message detector routinely labels such
a mix as a third language. So the text is cleaned first, then split into
sentences; each sentence the model is confident about casts a vote weighted by
its length, and the message takes the majority language.

Detection uses fastText's ``lid.176`` compressed model bundled with
``fast-langdetect`` (loaded from the package, never downloaded).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# A sentence must reach this weight (letters; a CJK character counts as a word)
# to vote, and a message this much in total to be classified.
MIN_SENTENCE_WEIGHT = 20
MIN_MESSAGE_WEIGHT = 25
# Model confidence a sentence needs to vote at all.
MIN_SENTENCE_CONFIDENCE = 0.5
# Share of a message's voting weight its top language needs to count.
MIN_MESSAGE_SHARE = 0.7

_TAG_BLOCK = re.compile(r"<([A-Za-z_][\w-]*)[^>]*>.*?</\1>", re.S)
_TAG = re.compile(r"<[^<>]{0,300}>")
_ATTR = re.compile(r'[\w-]+="[^"]*"')
_FENCE = re.compile(r"```.*?(```|$)", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+|www\.\S+")
_PATH = re.compile(
    r"(?:[~./\\]|[A-Za-z]:\\)[\w./\\-]+"
    r"|\b[\w-]+\.(?:py|ts|tsx|js|json|md|yaml|yml|go|rs|java|cpp|c|h|sh|txt|html|css)\b"
)
_JSONISH = re.compile(r"[{\[][^{}\[\]]{0,400}[}\]]")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_IDENTIFIER = re.compile(r"\b\w*[_A-Z]\w*[_A-Z0-9]\w*\b")  # snake_case / camelCase
# Sentence ends, including the full-width CJK forms of ! ? and ;.
_SENTENCE_END = re.compile(r"(?<=[.!?。\uff01\uff1f;\uff1b])\s+|\n+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


def _clean(text: str) -> str:
    """Strip markup, code, URLs, paths and identifiers, leaving prose."""
    for pattern in (_TAG, _ATTR, _INLINE_CODE, _URL, _PATH, _JSONISH, _EMAIL, _IDENTIFIER):
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def _weight(text: str) -> int:
    cjk = len(_CJK.findall(text))
    return cjk * 3 + sum(ch.isalpha() for ch in text) - cjk


@lru_cache(maxsize=1)
def _model() -> Any:
    from fast_langdetect import LangDetectConfig, LangDetector

    # Sentences are classified whole: the library would otherwise cut them to
    # 80 characters (and log each cut).
    return LangDetector(LangDetectConfig(model="lite", max_input_length=None))


def _predict(sentence: str) -> tuple[str, float]:
    result = _model().detect(sentence, model="lite", k=1)
    top = result[0] if isinstance(result, list) else result
    return str(top["lang"]), float(top["score"])


@dataclass(frozen=True)
class Detection:
    """The majority language of one message and the share of weight behind it."""

    language: str
    share: float
    weight: int


def detect_message(text: str | None) -> Detection | None:
    """Return the message's majority language, or None when there is too little prose."""
    if not text:
        return None
    text = _FENCE.sub(" ", _TAG_BLOCK.sub(" ", text))
    votes: Counter[str] = Counter()
    total = 0
    for chunk in _SENTENCE_END.split(text):
        sentence = _clean(chunk)
        weight = _weight(sentence)
        if weight < MIN_SENTENCE_WEIGHT:
            continue
        language, confidence = _predict(sentence)
        if confidence < MIN_SENTENCE_CONFIDENCE:
            continue
        votes[language] += weight
        total += weight
    if total < MIN_MESSAGE_WEIGHT:
        return None
    language, weight = votes.most_common(1)[0]
    return Detection(language=language, share=weight / total, weight=total)


def user_languages(detections: Iterable[Detection | None]) -> set[str]:
    """Return the languages one user's sampled messages show they write in.

    A language counts when at least two messages are mainly in it, or when the
    user has a single classifiable message that is long and unambiguous. One
    stray message is not enough, which keeps short misdetections out.
    """
    confident = [d for d in detections if d is not None and d.share >= MIN_MESSAGE_SHARE]
    counts = Counter(d.language for d in confident)
    languages = {language for language, n in counts.items() if n >= 2}
    if len(confident) == 1 and confident[0].share >= 0.9 and confident[0].weight >= 40:
        languages.add(confident[0].language)
    return languages
