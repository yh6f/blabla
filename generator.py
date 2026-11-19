"""Username list generator for gunslol.py.

Builds candidate username lists from patterns (2C, 3L, 4N, ...), word lists and
named recipes (faces, repeaters, leet, cities, e-girl / e-boy names) so
customlist.txt can be filled without typing names by hand.

Everything here is deterministic and offline: the bundled word lists live in
./data and every pattern returns the same output for the same inputs.
"""

from __future__ import annotations

import itertools
import os
import random
import re
import string
from collections import OrderedDict

LETTERS = string.ascii_lowercase
DIGITS = string.digits
BASE_CHARS = LETTERS + DIGITS          # "C" = character: letter or digit
SPECIALS = "._-"

USERNAME_RE = re.compile(r"^[a-z0-9._-]+$")
MAX_USERNAME_LENGTH = 32

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WORDS_FILE = os.path.join(DATA_DIR, "words_top.txt")
CITIES_FILE = os.path.join(DATA_DIR, "cities.txt")
EGIRLS_FILE = os.path.join(DATA_DIR, "egirls.txt")
EBOYS_FILE = os.path.join(DATA_DIR, "eboys.txt")

HARD_LIMIT = 2_000_000        # absolute line cap for one file
DEFAULT_LIMIT = 200_000       # comfortable default
PREVIEW_COUNT = 25

_LEET_MAP = OrderedDict([("a", "4"), ("e", "3"), ("i", "1"), ("o", "0"),
                         ("s", "5"), ("t", "7"), ("l", "1"), ("g", "9"), ("b", "8")])
_ONSET = "bcdfghjklmnprstvwz"
_VOWEL = "aeiou"
_CODA = "bdgklmnprstz"
_CLUSTERS = ("bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "pl", "pr",
             "sc", "sk", "sl", "sm", "sn", "sp", "st", "sw", "tr", "tw")
_EYES = "xXOo0TtUu^-+;QqDd=*123456789"


# --------------------------------------------------------------------- helpers

def normalize(name, max_length=MAX_USERNAME_LENGTH):
    """Lower-cases and strips anything a bio-link would reject. '' when unusable."""
    clean = re.sub(r"[^a-z0-9._-]", "", str(name).strip().lower().replace(" ", ""))
    if len(clean) < 1 or len(clean) > max_length:
        return ""
    if not USERNAME_RE.match(clean):
        return ""
    return clean


def is_premium_alias(name):
    return bool(name) and (name[0] in SPECIALS or name[-1] in SPECIALS)


def dedupe(names):
    seen, result = set(), []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def apply_filters(names, filter_premium=False, limit=DEFAULT_LIMIT):
    """Normalizes, de-duplicates and optionally drops premium aliases."""
    out, truncated = [], False
    for name in names:
        clean = normalize(name)
        if not clean or (filter_premium and is_premium_alias(clean)):
            continue
        if len(out) >= limit:
            truncated = True
            break
        out.append(clean)
    return dedupe(out), truncated


def read_wordlist(path):
    """Reads a bundled word list, ignoring comments and blank lines."""
    if not os.path.exists(path):
        return []
    names = []
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith(("#", "//")):
                    names.append(line)
    except OSError:
        return []
    return names


def _words(min_length=2, max_length=12):
    return [word for word in read_wordlist(WORDS_FILE)
            if min_length <= len(word) <= max_length]


def _bounded_product(alphabets, limit):
    """itertools.product with an early stop so huge patterns stay cheap."""
    produced = 0
    for combo in itertools.product(*alphabets):
        yield "".join(combo)
        produced += 1
        if produced >= limit:
            return


# ------------------------------------------------------------------- patterns

def _pattern_chars(count):
    """2C / 3C: every combination of letters and digits."""
    return ["".join(combo) for combo in itertools.product(BASE_CHARS, repeat=count)]


def _pattern_letters(count):
    return ["".join(combo) for combo in itertools.product(LETTERS, repeat=count)]


def _pattern_digits(count):
    return ["".join(combo) for combo in itertools.product(DIGITS, repeat=count)]


def _pattern_semi_3l():
    """Semi 3Ls (a_bc): three letters with an underscore after the first."""
    return [first + "_" + rest for first in LETTERS for rest in
            ("".join(combo) for combo in itertools.product(LETTERS, repeat=2))]


def _pattern_dotted(min_length=3, max_length=6):
    """Dotted words (s.e.a)."""
    return [".".join(word) for word in _words(min_length, max_length)]


def _pattern_underscores():
    """_ Repeaters: underscores around and inside short letter pairs."""
    pairs = ("".join(combo) for combo in itertools.product(LETTERS, repeat=2))
    shapes = ("_{p}", "{p}_", "{c1}_{c2}", "__{p}", "{p}__", "{c1}__{c2}")
    out = []
    for pair in pairs:
        for shape in shapes:
            out.append(shape.format(p=pair, c1=pair[0], c2=pair[1]))
    for letter in LETTERS:
        out.extend(("_" + letter + "_", letter + "_" + letter, "_" + letter, letter + "_"))
    return out


def _pattern_pronounceable():
    """Pronounceable 4Ls: CVCV plus CVCC syllable shapes."""
    out = []
    for onset in _ONSET:
        for vowel in _VOWEL:
            for second in _ONSET:
                out.append(onset + vowel + second + vowel)   # CVCV -> baba
                for coda in _CODA:
                    out.append(onset + vowel + second + coda)  # CVCC -> sakra
    return out


def _pattern_repeaters():
    """Letter Repeaters: aa, aaa, aaaa, aabb, abab (letters and digits)."""
    out = []
    for char in BASE_CHARS:
        out.extend((char * 2, char * 3, char * 4))
    for first in BASE_CHARS:
        for second in BASE_CHARS:
            out.extend((first * 2 + second * 2, first + second + first + second))
    return out


def _pattern_repeaters_4l(limit=10000):
    """4L Repeaters 10K: 4 characters with at least one repeat, best first."""
    out = []
    for position in range(3):
        for char in BASE_CHARS:
            for other in BASE_CHARS:
                for tail in BASE_CHARS:
                    chars = [other, tail, tail, other]
                    chars[position] = char
                    out.append("".join(chars))
    for first in BASE_CHARS:
        for second in BASE_CHARS:
            out.extend((first + second + second + first, first + second + first + second))
    ordered = dedupe(out)
    if len(ordered) >= limit:
        return ordered[:limit]
    remaining = limit - len(ordered)
    for combo in _bounded_product(BASE_CHARS * 4, remaining * 4):
        if len(set(combo)) < 4:            # at least one repeated character
            ordered.append(combo)
            remaining -= 1
            if remaining <= 0:
                break
    return dedupe(ordered)


def _pattern_faces():
    """Faces (X_X / X.X): eyes separated by an underscore or a dot."""
    return [first + separator + second
            for first in _EYES for separator in "_." for second in _EYES]


def _pattern_wordlist(path):
    return read_wordlist(path)


def _pattern_leet_4l(variants_per_word=12):
    """Leet 4Cs: four-letter words with classic leet substitutions."""
    substitution_letters = [letter for letter in _LEET_MAP if letter in LETTERS]
    out = []
    for word in _words(4, 4):
        swappable = [position for position, char in enumerate(word) if char in substitution_letters]
        if not swappable:
            out.append(word)
            continue
        produced = 0
        for mask in range(1 << len(swappable)):
            if mask == 0:
                continue
            chars = list(word)
            for bit, position in enumerate(swappable):
                if mask & (1 << bit):
                    chars[position] = _LEET_MAP[word[position]]
            out.append("".join(chars))
            produced += 1
            if produced >= variants_per_word:
                break
        out.append(word)
    return out


def _pattern_cities():
    return _pattern_wordlist(CITIES_FILE)


def _pattern_egirls():
    return _pattern_wordlist(EGIRLS_FILE)


def _pattern_eboys():
    return _pattern_wordlist(EBOYS_FILE)


# Pattern catalogue. Aliases (All 2Cs / All 3Cs) keep the classic naming people use.
PATTERNS = [
    {"code": "2c", "label": "2C List", "group": "Combination",
     "help": "All 2-character names over a-z 0-9 (1,296 names).",
     "builder": lambda lim: _pattern_chars(2)},
    {"code": "3c", "label": "3C List", "group": "Combination",
     "help": "All 3-character names over a-z 0-9 (46,656 names).",
     "builder": lambda lim: _pattern_chars(3)},
    {"code": "all2c", "label": "All 2Cs", "group": "Combination",
     "help": "Alias of 2C List - every possible 2-character name.",
     "builder": lambda lim: _pattern_chars(2)},
    {"code": "all3c", "label": "All 3Cs", "group": "Combination",
     "help": "Alias of 3C List - every possible 3-character name.",
     "builder": lambda lim: _pattern_chars(3)},
    {"code": "2l", "label": "2L List", "group": "Letters only",
     "help": "All 2-letter names (676).", "builder": lambda lim: _pattern_letters(2)},
    {"code": "3l", "label": "3L List", "group": "Letters only",
     "help": "All 3-letter names (17,576).", "builder": lambda lim: _pattern_letters(3)},
    {"code": "4l", "label": "4L List", "group": "Letters only",
     "help": "All 4-letter names (456,976) - a very large file.",
     "builder": lambda lim: _pattern_letters(4)},
    {"code": "2n", "label": "2N List", "group": "Numbers only",
     "help": "All 2-digit names (00-99).", "builder": lambda lim: _pattern_digits(2)},
    {"code": "3n", "label": "3N List", "group": "Numbers only",
     "help": "All 3-digit names (000-999).", "builder": lambda lim: _pattern_digits(3)},
    {"code": "4n", "label": "4N List", "group": "Numbers only",
     "help": "All 4-digit names (0000-9999).", "builder": lambda lim: _pattern_digits(4)},
    {"code": "semi3l", "label": "Semi 3Ls (a_bc)", "group": "Styled",
     "help": "Three letters with an underscore after the first (a_bc).",
     "builder": lambda lim: _pattern_semi_3l()},
    {"code": "dotted", "label": "Dotted words (s.e.a)", "group": "Styled",
     "help": "Dictionary words written with a dot between every letter.",
     "builder": lambda lim: _pattern_dotted()},
    {"code": "uscore", "label": "_ Repeaters", "group": "Styled",
     "help": "Underscores around and inside two-letter pairs (_ab, ab_, a__b).",
     "builder": lambda lim: _pattern_underscores()},
    {"code": "faces", "label": "Faces (X_X / X.X)", "group": "Styled",
     "help": "Emoji-style faces built from eyes and _ or . separators.",
     "builder": lambda lim: _pattern_faces()},
    {"code": "top10k", "label": "TOP 10K English Words", "group": "Word lists",
     "help": "The bundled list of the 10,000 most common English words.",
     "builder": lambda lim: _pattern_wordlist(WORDS_FILE)},
    {"code": "cities", "label": "Cities in the world", "group": "Word lists",
     "help": "721 world cities, normalized (newyork, shibuya, saopaulo).",
     "builder": lambda lim: _pattern_cities()},
    {"code": "egirl", "label": "E-Girl Names", "group": "Word lists",
     "help": "Aesthetic / soft / sparkle-core starter names (editable file).",
     "builder": lambda lim: _pattern_egirls()},
    {"code": "eboy", "label": "E-Boy Names", "group": "Word lists",
     "help": "Grunge, anime, skater and cyber starter names (editable file).",
     "builder": lambda lim: _pattern_eboys()},
    {"code": "pron4l", "label": "Pronounceable 4Ls", "group": "Advanced",
     "help": "Four-letter names that read like words (baba, mikto, sakra).",
     "builder": lambda lim: _pattern_pronounceable()},
    {"code": "rep", "label": "Letter Repeaters", "group": "Advanced",
     "help": "aa, aaa, aaaa, aabb, abab over letters and digits.",
     "builder": lambda lim: _pattern_repeaters()},
    {"code": "rep4l", "label": "4L Repeaters 10K", "group": "Advanced",
     "help": "First 10,000 four-character names containing a repeated character.",
     "builder": lambda lim: _pattern_repeaters_4l(min(lim, 10000))},
    {"code": "leet4c", "label": "Leet 4Cs", "group": "Advanced",
     "help": "Four-letter words with leet swaps (h3ll, s0da, m4tt).",
     "builder": lambda lim: _pattern_leet_4l()},
]

PATTERN_BY_CODE = {pattern["code"]: pattern for pattern in PATTERNS}
PATTERN_ALIASES = {"2 characters": "2c", "3 characters": "3c", "all 2cs": "all2c",
                   "all 3cs": "all3c", "top 10k": "top10k", "10k": "top10k",
                   "english": "top10k", "words": "top10k", "cities": "cities",
                   "egirls": "egirl", "eboys": "eboy", "leet": "leet4c",
                   "faces": "faces", "semi": "semi3l", "dotted": "dotted"}


def resolve_pattern(code):
    """'All 3Cs' / 'all3c' / '3' -> the matching pattern entry (None if unknown)."""
    text = str(code or "").strip().lower()
    if not text:
        return None
    if text in PATTERN_BY_CODE:
        return PATTERN_BY_CODE[text]
    if text in PATTERN_ALIASES:
        return PATTERN_BY_CODE[PATTERN_ALIASES[text]]
    for pattern in PATTERNS:
        if pattern["label"].lower() == text:
            return pattern
    stripped = text.replace(" ", "")
    if stripped in PATTERN_BY_CODE:
        return PATTERN_BY_CODE[stripped]
    if text.rstrip("s") in PATTERN_BY_CODE:
        return PATTERN_BY_CODE[text.rstrip("s")]
    return None


class GenResult:
    """Generated names plus bookkeeping for the menu."""

    def __init__(self, names, truncated=False, notes=()):
        self.names = names
        self.truncated = truncated
        self.notes = list(notes)

    @property
    def count(self):
        return len(self.names)


def generate(code, limit=DEFAULT_LIMIT, filter_premium=False):
    """Builds the list for one pattern code. Raises ValueError for unknown codes."""
    pattern = resolve_pattern(code)
    if pattern is None:
        raise ValueError("unknown pattern: {}".format(code))
    notes = []
    raw = pattern["builder"](max(limit, 1))
    names, truncated = apply_filters(raw, filter_premium=filter_premium, limit=limit)
    if truncated:
        notes.append("stopped at the {} line limit".format(limit))
    if not names:
        notes.append("nothing usable was produced - check the data files in ./data")
    return GenResult(names, truncated=truncated, notes=notes)


def estimate(code):
    """Upper-bound line count for a pattern (used for the size warning)."""
    pattern = resolve_pattern(code)
    if pattern is None:
        return 0
    try:
        return len(pattern["builder"](HARD_LIMIT))
    except Exception:
        return 0


def write_list(names, path, mode="replace"):
    """Writes generated names. mode: 'replace' | 'append'. Returns (mode_used, lines)."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    if mode == "append" and os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(names) + "\n")
        return "appended", len(names)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("// Generated by gunslol.py list generator\n")
        handle.write("\n".join(names) + "\n")
    return "replaced", len(names)


def mix(codes, limit=DEFAULT_LIMIT, filter_premium=False):
    """Combines several patterns into one de-duplicated list."""
    combined, notes = [], []
    for code in codes:
        result = generate(code, limit=limit, filter_premium=filter_premium)
        combined.extend(result.names)
        notes.extend("{}: {}".format(code, note) for note in result.notes)
    unique = dedupe(combined)
    if len(unique) > limit:
        unique = unique[:limit]
        notes.append("combined list trimmed to {} lines".format(limit))
    return GenResult(unique, truncated=len(unique) >= limit, notes=notes)


def random_sample(code, count=25, filter_premium=False):
    """A random look at what a pattern produces (for previews)."""
    names = generate(code, limit=HARD_LIMIT, filter_premium=filter_premium).names
    if len(names) <= count:
        return list(names)
    return random.sample(names, count)
