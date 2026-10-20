"""The languages the tools know: names, the tags that name them in file names and stream metadata, and the code pages
their subtitles used before UTF-8."""
import re
from dataclasses import dataclass, field

CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
KANA = "\u3040-\u30ff\u31f0-\u31ff"
HANGUL = "\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f"
BOPOMOFO = "\u3100-\u312f\u31a0-\u31bf"
CYRILLIC = "\u0400-\u04ff"
ARABIC = "\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff"
THAI = "\u0e00-\u0e7f"
DEVANAGARI = "\u0900-\u097f\ua8e0-\ua8ff"
VIETNAMESE_TONES = "\u0300\u0301\u0303\u0309\u0323\u1ea0-\u1ef9"
LOANWORD_ACCENTS = "àáâäçèéêëíîïñóôöúûü"


def both_cases(letters):
    return letters + "".join(ch.upper() for ch in letters if len(ch.upper()) == 1)


@dataclass(frozen=True)
class Language:
    code: str
    name: str
    aliases: tuple = ()            # other tags that name the language (ISO 639-2, Bazarr's and common short forms)
    encodings: tuple = ()          # legacy code pages, tried when a file is neither UTF-8 nor UTF-16
    script: str = ""               # character class of a non-Latin script; empty for languages written in Latin
    native: str = ""               # character class of every non-ASCII character normal text in the language uses
    cased_legacy: bool = False     # its code pages differ in letter case, so legacy text must be mostly lower case
    accent_cap: bool = True        # in Latin script, accented letters are a minority of the letters
    script_re: re.Pattern = field(init=False, repr=False, compare=False)
    native_re: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "script_re", re.compile(f"[{self.script}]") if self.script else None)
        object.__setattr__(self, "native_re", re.compile(f"[{self.native or self.script}]"))

    @property
    def tags(self):
        return {self.code, *self.aliases}


def latin(code, name, aliases, accents, encodings=("cp1252",), extra="", accent_cap=True):
    """A language written in Latin script; `accents` are its non-ASCII letters, `extra` more character-class syntax."""
    native = re.escape(both_cases(accents)) + extra
    return Language(code, name, aliases, encodings, native=native, accent_cap=accent_cap)


LANGUAGES = {lang.code: lang for lang in (
    latin("en", "English", ("eng",), LOANWORD_ACCENTS),
    Language("ja", "Japanese", ("jpn", "jp", "jap"), ("cp932", "euc_jp"), script=KANA, native=KANA + CJK),
    Language("ko", "Korean", ("kor", "kr"), ("cp949",), script=HANGUL, native=HANGUL + KANA + CJK),
    Language("zh", "Chinese", ("zho", "chi", "cmn", "yue", "zht", "zt", "zhs", "cht", "chs"), ("gb18030", "cp950"),
             script=CJK, native=CJK + BOPOMOFO + KANA),
    latin("es", "Spanish", ("spa", "spl", "ea"), "áéíñóúü¡¿"),
    latin("fr", "French", ("fra", "fre"), "àâæçéèêëîïôœùûüÿ"),
    latin("de", "German", ("deu", "ger"), "äöüßàâçéèê"),
    latin("it", "Italian", ("ita",), "àèéìíîòóùú"),
    latin("pt", "Portuguese", ("por", "pob", "pb"), "áâãàçéêíóôõúü"),
    Language("ru", "Russian", ("rus",), ("cp1251",), script=CYRILLIC, native=CYRILLIC + "№", cased_legacy=True),
    latin("vi", "Vietnamese", ("vie",), "àáâãèéêìíòóôõùúýăđĩũơư", ("cp1258",), VIETNAMESE_TONES, accent_cap=False),
    Language("th", "Thai", ("tha",), ("cp874",), script=THAI),
    latin("id", "Indonesian", ("ind",), LOANWORD_ACCENTS),
    Language("ar", "Arabic", ("ara",), ("cp1256",), script=ARABIC),
    Language("hi", "Hindi", ("hin",), script=DEVANAGARI),
    latin("tr", "Turkish", ("tur",), "çğıöşüâîûİ", ("cp1254",)),
    latin("pl", "Polish", ("pol",), "ąćęłńóśźż", ("cp1250", "iso8859_2")),
    latin("nl", "Dutch", ("nld", "dut"), "áéíóúàèëïöüĳ"),
    latin("sv", "Swedish", ("swe",), "åäöéü"),
    Language("uk", "Ukrainian", ("ukr",), ("cp1251",), script=CYRILLIC, native=CYRILLIC + "№\u02bc",
             cased_legacy=True),
)}
TAG_TO_CODE = {tag: lang.code for lang in LANGUAGES.values() for tag in lang.tags}
REGION = re.compile(r"[-_]")
ANY_LANGUAGE = "*"                 # in a list of reference languages: any other language present


def lang_of(tag):
    """The language code a tag names ("kor", "pt-BR", "zh_TW"), or None."""
    return TAG_TO_CODE.get(REGION.split(str(tag).lower(), maxsplit=1)[0])


def name_of(code):
    return LANGUAGES[code].name


def language_code(value, setting):
    """A supported language code from a setting's value, or ValueError naming the setting."""
    code = str(value).lower()
    if code not in LANGUAGES:
        raise ValueError(f"{setting}: unknown language {value!r} (use one of {', '.join(LANGUAGES)})")
    return code
