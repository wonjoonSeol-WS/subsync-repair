"""Decoding subtitle files: UTF-32/16 by their byte order mark, then UTF-8, then the legacy code pages of the language,
accepting a result only when it reads like text in that language."""
import re
from typing import NamedTuple

from subsync_repair.formats.languages import LANGUAGES

MAX_REPLACEMENT_RATIO = 0.005   # share of U+FFFD characters above which text is mojibake
MIN_SCRIPT_RATIO = 0.15         # share of the letters that must be in the language's own script
MIN_NATIVE_SHARE = 0.9          # share of the other non-ASCII characters a legacy decoding must spend on the language
MAX_ACCENTED_RATIO = 0.35       # in Latin script, share of the letters that may carry an accent
MAX_UPPERCASE_RATIO = 0.6       # in a cased script, share of the letters that may be upper case
EVERYDAY_SUBSET = {"cp949": "euc_kr", "gb18030": "gb2312"}   # text read with the wrong code page falls outside these
UTF8_BOM = b"\xef\xbb\xbf"
UTF32_BOMS = (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
BOMS = (UTF8_BOM, *UTF32_BOMS, *UTF16_BOMS)
UNICODE_ENCODINGS = frozenset({"utf-8-sig", "utf-16", "utf-32"})
MARKUP = re.compile(r"<[^>]*>|\{[^}]*\}|&nbsp;|&[a-z]+;", re.I)
NEUTRAL = re.compile(
    "[\u00a0\u00a9\u00ab\u00ae\u00b0\u00b7\u00bb\u2000-\u206f\u20a0-\u20cf\u2100-\u214f\u2190-\u21ff\u2460-\u24ff"
    "\u25a0-\u27bf\u3000-\u303f\uff00-\uff65\uffe0-\uffee\ufeff\U0001f000-\U0001faff]")


class UndecodableText(ValueError):
    pass


class Decoded(NamedTuple):
    text: str
    encoding: str


def decode_text(data, lang, judge=None):
    """The text of a subtitle file in language `lang`, or UndecodableText. `judge(text)` picks the part that must read
    like the language (the spoken lines of an ASS file, not its Latin header)."""
    language = LANGUAGES[lang]
    judge = judge or (lambda text: text)
    encoding = unicode_encoding(data)
    text = try_decode(data, encoding)
    if text is not None and score(judge(text), language) is not None:
        return Decoded(text, encoding)
    best, best_score = None, -1.0
    for encoding in language.encodings:
        text = try_decode(data, encoding)
        value = None if text is None else score(judge(text), language, legacy=encoding)
        if value is not None and value > best_score:
            best, best_score = Decoded(text, encoding), value
    if best is None:
        raise UndecodableText(f"text does not read as {language.name}")
    return best


def utf8_like(text, raw, encoding):
    """`text` as UTF-8 for a file that was `raw`, read as `encoding`. It starts with a byte order mark when the original
    had one or used a legacy code page: without one, players take a file for the system's ANSI code page."""
    marked = raw.startswith(BOMS) or encoding not in UNICODE_ENCODINGS
    return (UTF8_BOM if marked else b"") + text.encode("utf-8")


def unicode_encoding(data):
    if data[:4] in UTF32_BOMS:      # before UTF-16, whose little-endian mark begins the UTF-32 one
        return "utf-32"
    if data[:2] in UTF16_BOMS:
        return "utf-16"
    return "utf-8-sig"


def try_decode(data, encoding):
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        return None


def score(text, language, legacy=None):
    """None when `text` cannot be `language`, else the share of its non-ASCII characters that belong to the language;
    text from a `legacy` code page is checked harder."""
    if text.count("\ufffd") > len(text) * MAX_REPLACEMENT_RATIO:
        return None
    plain = MARKUP.sub("", text)
    letters = [ch for ch in plain if ch.isalpha()]
    own = [ch for ch in letters if language.script_re.match(ch)] if language.script_re else []
    if language.script_re and letters and len(own) < len(letters) * MIN_SCRIPT_RATIO:
        return None
    if not legacy:
        return 1.0
    if language.cased_legacy and sum(ch.isupper() for ch in own) > len(own) * MAX_UPPERCASE_RATIO:
        return None
    subset = EVERYDAY_SUBSET.get(legacy)
    if subset and sum(one_code(ch, subset) for ch in own) < len(own) * MIN_NATIVE_SHARE:
        return None
    accented = sum(ord(ch) > 127 for ch in letters)
    if not language.script_re and language.accent_cap and accented > len(letters) * MAX_ACCENTED_RATIO:
        return None
    special = [ch for ch in plain if ord(ch) > 127 and not ch.isspace() and not NEUTRAL.match(ch)]
    if not special:
        return 1.0
    share = sum(1 for ch in special if language.native_re.match(ch)) / len(special)
    return share if share >= MIN_NATIVE_SHARE else None


def one_code(ch, codec):
    """`ch` is a single code of `codec` (Python's euc_kr spells the rarer syllables as eight-byte jamo sequences)."""
    try:
        return len(ch.encode(codec)) <= 2
    except UnicodeEncodeError:
        return False
