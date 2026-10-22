"""Whole-word matching for short, explicitly entered furniture designations."""
import re
from functools import lru_cache


def is_short_word(term):
    return 1 <= len(term) <= 3 and term.isalpha()


@lru_cache(maxsize=256)
def _whole_word(term):
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def whole_word_matches(text, term):
    return bool(_whole_word(term).search(text or ""))
