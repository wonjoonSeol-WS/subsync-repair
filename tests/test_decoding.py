"""Tests for subsync_repair/formats/decoding.py: every language's legacy code pages, and mojibake that must be
refused."""
import unicodedata
import unittest

from subsync_repair.formats import ass, decoding
from subsync_repair.formats.decoding import UndecodableText, decode_text
from subsync_repair.formats.languages import LANGUAGES

TEXT = {
    "en": "Hello, how are you? I haven't seen you in a long time. Where have you been all this time?",
    "ja": "こんにちは、元気ですか。久しぶりですね。今までどこにいたんですか。もう行かなきゃ。",
    "ko": "안녕하세요, 잘 지내세요? 오랜만이에요. 그동안 어디 있었어요? 이제 가야 해요.",
    "zh-hans": "你好，你最近怎么样？我很久没见到你了。这段时间你去哪里了？人们都说他是个好人。",
    "zh-hant": "你好，你最近怎麼樣？我很久沒見到你了。這段時間你去哪裡了？人們都說他是個好人。",
    "es": "Hola, ¿cómo estás? Hace mucho que no te veo. ¿Dónde has estado todo este tiempo? Mañana.",
    "fr": "Bonjour, comment ça va ? Je ne t'ai pas vu depuis longtemps. Où étais-tu pendant tout ce temps ?",
    "de": "Hallo, wie geht es dir? Ich habe dich lange nicht gesehen. Schöne Grüße, wir müssen gehen.",
    "it": "Ciao, come stai? Non ti vedo da molto tempo. Dobbiamo andare, è tardi. Perché città.",
    "pt": "Olá, como você está? Não te vejo há muito tempo. Temos que ir, já é tarde. Coração.",
    "ru": "Привет, как дела? Я давно тебя не видел. Где ты был всё это время? Нам пора идти.",
    "vi": "Xin chào, bạn có khỏe không? Lâu rồi tôi không gặp bạn. Chúng ta phải đi thôi, muộn rồi.",
    "th": "สวัสดี สบายดีไหม ไม่ได้เจอกันนานเลย คุณไปอยู่ที่ไหนมาตลอดเวลานี้ เราต้องไปแล้ว",
    "id": "Halo, apa kabar? Sudah lama aku tidak melihatmu. Ke mana saja kamu selama ini?",
    "ar": "مرحبا، كيف حالك؟ لم أرك منذ وقت طويل. أين كنت طوال هذا الوقت؟ يجب أن نذهب الآن.",
    "hi": "नमस्ते, आप कैसे हैं? मैंने आपको बहुत समय से नहीं देखा। हमें अब जाना चाहिए।",
    "tr": "Merhaba, nasılsın? Seni uzun zamandır görmedim. Şimdi gitmeliyiz, çok geç oldu. Ağaç İstanbul.",
    "pl": "Cześć, jak się masz? Dawno cię nie widziałem. Gdzie byłeś przez cały ten czas? Żółć gęślą jaźń.",
    "nl": "Hallo, hoe gaat het? Ik heb je lang niet gezien. We moeten gaan, het is laat. Café één.",
    "sv": "Hej, hur mår du? Jag har inte sett dig på länge. Vi måste gå nu, det är sent. Smörgåsbord.",
    "uk": "Привіт, як справи? Я давно тебе не бачив. Де ти був увесь цей час? Їжак, ґанок, є.",
}
LEGACY = [("en", "en", "cp1252"), ("ja", "ja", "cp932"), ("ja", "ja", "euc_jp"), ("ko", "ko", "cp949"),
          ("zh-hans", "zh", "gb18030"), ("zh-hans", "zh", "gbk"), ("zh-hant", "zh", "cp950"),
          ("zh-hant", "zh", "big5"), ("es", "es", "cp1252"), ("fr", "fr", "cp1252"), ("de", "de", "cp1252"),
          ("it", "it", "cp1252"), ("pt", "pt", "cp1252"), ("ru", "ru", "cp1251"), ("vi", "vi", "cp1258"),
          ("th", "th", "cp874"), ("th", "th", "tis_620"), ("id", "id", "cp1252"), ("ar", "ar", "cp1256"),
          ("tr", "tr", "cp1254"), ("pl", "pl", "cp1250"), ("pl", "pl", "iso8859_2"), ("nl", "nl", "cp1252"),
          ("sv", "sv", "cp1252"), ("uk", "uk", "cp1251")]


def encode(text, codec):
    """`text` in `codec`; Vietnamese code page 1258 spells most tones as combining marks."""
    out = b""
    for ch in text:
        try:
            out += ch.encode(codec)
        except UnicodeEncodeError:
            base, *marks = unicodedata.normalize("NFD", ch)
            for i in range(-1, len(marks)):
                composed = unicodedata.normalize("NFC", base + (marks[i] if i >= 0 else ""))
                rest = "".join(m for j, m in enumerate(marks) if j != i)
                try:
                    out += composed.encode(codec) + rest.encode(codec)
                    break
                except UnicodeEncodeError:
                    continue
            else:
                raise
    return out


def sample(key):
    return "\n".join([TEXT[key]] * 5)


class LegacyCodePages(unittest.TestCase):
    def test_a_file_in_each_languages_old_code_page_is_decoded_exactly(self):
        for key, lang, codec in LEGACY:
            with self.subTest(language=lang, codec=codec):
                got = decode_text(encode(sample(key), codec), lang)
                self.assertEqual(unicodedata.normalize("NFC", got.text), sample(key))

    def test_every_language_has_a_decoding_rule(self):
        for code, language in LANGUAGES.items():
            with self.subTest(code):
                self.assertTrue(language.encodings or code == "hi", "only Hindi has no legacy code page")
                self.assertTrue(language.script or language.native, "a script or an alphabet to judge text by")

    def test_the_western_code_page_is_never_tried_for_a_language_it_cannot_write(self):
        for code in ("ja", "ko", "zh", "ru", "uk", "ar", "th", "hi", "tr", "pl", "vi"):
            self.assertNotIn("cp1252", LANGUAGES[code].encodings, code)

    def test_when_two_code_pages_both_read_well_the_one_that_reads_best_wins(self):
        text = ("Dobry wieczór, jak minął dzień? Mówiłem już, że będę w domu o ósmej. Ten tramwaj był pełny ludzi, "
                "ale wszyscy byli mili. Jutro też będę pracować do nocy, więc nie czekaj na mnie z obiadem.\n") * 5
        data = text.encode("iso8859_2")
        pl = LANGUAGES["pl"]
        self.assertIsNotNone(decoding.score(data.decode("cp1250"), pl, legacy="cp1250"), "cp1250 reads it well too")
        self.assertEqual(decode_text(data, "pl"), (text, "iso8859_2"))

    def test_unicode_files_are_read_by_their_byte_order_mark(self):
        for codec, expected in (("utf-8", "utf-8-sig"), ("utf-8-sig", "utf-8-sig"), ("utf-16", "utf-16"),
                                ("utf-32", "utf-32")):
            self.assertEqual(decode_text(sample("ko").encode(codec), "ko"), (sample("ko"), expected), codec)


class Choice(unittest.TestCase):
    def test_when_two_code_pages_read_the_same_the_first_listed_wins(self):
        text = "Mówiłem, że będę późno. Ten koń jest mój, ale łódź nie. Ćma, łąka, ołówek, kółko.\n" * 5
        same = "".join(ch for ch in text if ch.encode("cp1250") == ch.encode("iso8859_2"))
        self.assertEqual(LANGUAGES["pl"].encodings[0], "cp1250")
        self.assertEqual(decode_text(same.encode("cp1250"), "pl"), (same, "cp1250"))

    def test_a_legacy_text_exactly_at_the_native_share_is_accepted(self):
        native, foreign = "äöüäöüäöü", "ñ"
        text = ("Guten Abend und willkommen zu Hause bei uns " + native + foreign + "\n") * 3
        self.assertEqual(decode_text(text.encode("cp1252"), "de").encoding, "cp1252")
        too_foreign = text.replace(native[0], "ñ")
        with self.assertRaises(UndecodableText):
            decode_text(too_foreign.encode("cp1252"), "de")

    def test_the_refusal_names_the_language(self):
        with self.assertRaisesRegex(UndecodableText, "^text does not read as Japanese$"):
            decode_text(bytes(range(128, 256)) * 4, "ja")


class Utf8Output(unittest.TestCase):
    def test_a_byte_order_mark_is_kept_and_added_for_a_legacy_code_page(self):
        cases = (("x".encode("utf-8-sig"), "utf-8-sig", True), ("x".encode("utf-16"), "utf-16", True),
                 ("x".encode("utf-32"), "utf-32", True), (b"x", "utf-8-sig", False), (b"x", "cp1252", True))
        for raw, encoding, bom in cases:
            data = decoding.utf8_like("안녕", raw, encoding)
            self.assertEqual(data.startswith(decoding.UTF8_BOM), bom, encoding)
            self.assertEqual(data.decode("utf-8-sig"), "안녕")


class Mojibake(unittest.TestCase):
    def refused(self, key, lang, codec):
        with self.assertRaises(UndecodableText, msg=f"{key} read as {lang} from {codec}"):
            decode_text(encode(sample(key), codec), lang)

    def test_text_read_with_another_scripts_code_page_is_refused(self):
        for key, lang, codec in (("ru", "en", "cp1251"), ("ru", "de", "cp1251"), ("ru", "pl", "cp1251"),
                                 ("ru", "tr", "cp1251"), ("ru", "vi", "cp1251"), ("ko", "en", "cp949"),
                                 ("ar", "fr", "cp1256"), ("zh-hans", "en", "gb18030"), ("th", "vi", "cp874"),
                                 ("ja", "ko", "cp932"), ("ko", "ja", "cp949"), ("zh-hant", "ja", "cp950"),
                                 ("ja", "zh", "cp932")):
            self.refused(key, lang, codec)

    def test_a_neighbouring_code_page_of_the_same_script_is_refused(self):
        for key, lang, codec in (("pl", "tr", "cp1250"), ("tr", "pl", "cp1254"), ("ru", "ru", "koi8_r"),
                                 ("uk", "uk", "koi8_u")):
            self.refused(key, lang, codec)

    def test_big5_is_not_taken_for_gb18030_garbage(self):
        data = encode(sample("zh-hant"), "cp950")
        self.assertIsNotNone(decoding.try_decode(data, "gb18030"), "the bytes do decode as gb18030")
        self.assertEqual(decode_text(data, "zh").encoding, "cp950")

    def test_korean_utf8_named_as_japanese_and_random_bytes_are_refused(self):
        for data in (sample("ko").encode("utf-8"), bytes(range(128, 256)) * 4):
            with self.assertRaises(UndecodableText):
                decode_text(data, "ja")


class RealFiles(unittest.TestCase):
    def test_markup_heavy_sami_is_not_mistaken_for_mojibake(self):
        body = "".join(f"<SYNC Start={i * 1000}><P Class=KRCC>네<br>응\n"
                       f"<SYNC Start={i * 1000 + 500}><P Class=KRCC>&nbsp;\n" for i in range(200))
        sami = ("<SAMI><HEAD><STYLE TYPE=\"text/css\"><!--\nP {margin-left:8pt; font-family:arial, sans-serif;}\n"
                ".KRCC {Name:한국어; lang:ko-KR; SAMIType:CC;}\n--></STYLE></HEAD><BODY>\n" + body + "</BODY></SAMI>")
        self.assertEqual(decode_text(sami.encode("cp949"), "ko").encoding, "cp949")

    def test_a_sparse_ass_is_judged_by_its_dialogue_not_its_latin_header(self):
        header = "[Script Info]\nTitle: a long English header\n" + "Style: Default,Arial,20,&H00FFFFFF\n" * 40
        raw = (header + "[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,안녕하세요 잘 지내세요\n").encode("cp949")
        with self.assertRaises(UndecodableText):
            decode_text(raw, "ko")
        self.assertEqual(decode_text(raw, "ko", judge=ass.dialogue_text).encoding, "cp949")

    def test_everyday_extras_do_not_spoil_a_legacy_file(self):
        korean = "♪ 안녕하세요 \u00b7 잘 지내세요? ★ 오랜만이에요 … ※ 그동안 어디 있었어요?\n" * 5
        lyrics = "♪ 君の名前を呼んでいた ♪\n" * 3
        self.assertEqual(decode_text((korean + lyrics).encode("cp949"), "ko").encoding, "cp949")
        french = "« Bonjour », dit-il. C’est l’été, où étais-tu ?\n" * 5
        self.assertEqual(decode_text(french.encode("cp1252"), "fr").encoding, "cp1252")

    def test_an_english_utf8_file_with_japanese_lines_is_still_english(self):
        text = "Hello there, how are you?\n" * 5 + "君の名前を呼んでいた\n" * 3
        self.assertEqual(decode_text(text.encode("utf-8"), "en").text, text)

    def test_a_language_written_in_latin_accepts_plain_utf8_text(self):
        self.assertEqual(decode_text("Guten Tag, wie geht es dir".encode("utf-8"), "de").text,
                         "Guten Tag, wie geht es dir")


if __name__ == "__main__":
    unittest.main()
