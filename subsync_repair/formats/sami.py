"""SAMI (.smi) files that hold several languages, one per paragraph class (KRCC, ENCC, ...)."""
import re

from subsync_repair.formats.languages import lang_of, name_of

STYLE_CLASS = re.compile(r"\.([\w-]+)\s*\{([^}]*)\}")
STYLE_LANG = re.compile(r"\blang\s*:\s*([A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)?)", re.I)
P_CLASS = re.compile(r"<P\b[^>]*?\bclass\s*=\s*[\"']?([\w-]+)", re.I)
SYNC_BLOCK = re.compile(r"(<SYNC\b[^>]*>)(.*?)(?=<SYNC\b|</BODY\s*>|\Z)", re.I | re.S)
PARAGRAPH = re.compile(r"<P\b.*?(?=<P\b|\Z)", re.I | re.S)
FIRST_PARAGRAPH = re.compile(r"<P\b", re.I)
CAPTION_SUFFIX = re.compile(r"CC$", re.I)


def classes(text):
    """The paragraph classes the captions use, in order of first use."""
    return list(dict.fromkeys(c.upper() for c in P_CLASS.findall(text)))


def class_language(text, name):
    """The language of a class: its lang property in the STYLE block, else its name (KRCC is Korean)."""
    for cls, body in STYLE_CLASS.findall(text):
        if cls.upper() == name:
            m = STYLE_LANG.search(body)
            if m and lang_of(m.group(1)):
                return lang_of(m.group(1))
    short = CAPTION_SUFFIX.sub("", name)
    return lang_of(short) or lang_of(short[:2])


def keep_language(text, lang):
    """(text with only the captions in `lang`, the classes left out). A file with one class comes back unchanged;
    ValueError when several classes are present and not exactly one of them is in `lang`."""
    used = classes(text)
    if len(used) <= 1:
        return text, ()
    matching = [c for c in used if class_language(text, c) == lang]
    if len(matching) != 1:
        raise ValueError(f"the .smi holds {len(used)} languages ({', '.join(used)}) and "
                         f"{'none' if not matching else 'more than one'} of them is {name_of(lang)}")
    keep = matching[0]

    def only_kept(sync):
        head, body = sync.group(1), sync.group(2)
        first = FIRST_PARAGRAPH.search(body)
        if not first:
            return sync.group(0)
        kept = [p for p in PARAGRAPH.findall(body, first.start()) if paragraph_class(p) == keep]
        return head + body[:first.start()] + "".join(kept) if kept else ""

    return SYNC_BLOCK.sub(only_kept, text), tuple(c for c in used if c != keep)


def paragraph_class(paragraph):
    m = P_CLASS.match(paragraph)
    return m.group(1).upper() if m else ""
