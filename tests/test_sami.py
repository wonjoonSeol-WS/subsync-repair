"""Tests for subsync_repair/formats/sami.py: SAMI files with one language per paragraph class."""
import unittest

from subsync_repair.formats import sami

STYLE = (".KRCC {Name:Korean; lang:ko-KR; SAMIType:CC;}\n.ENCC {Name:English; lang:en-US; SAMIType:CC;}\n")


def document(body, style=STYLE):
    return f"<SAMI><HEAD><STYLE TYPE=\"text/css\"><!--\n{style}--></STYLE></HEAD><BODY>\n{body}</BODY></SAMI>\n"


BOTH_IN_ONE_SYNC = document("<SYNC Start=1000><P Class=KRCC>안녕\n<P Class=ENCC>Hello\n"
                            "<SYNC Start=3000><P Class=KRCC>&nbsp;\n<P Class=ENCC>&nbsp;\n")
ONE_SYNC_EACH = document("<SYNC Start=1000><P Class=KRCC>안녕\n<SYNC Start=1000><P Class=ENCC>Hello\n"
                         "<SYNC Start=3000><P Class=ENCC>&nbsp;\n<SYNC Start=3000><P Class=KRCC>&nbsp;\n")


class Classes(unittest.TestCase):
    def test_the_classes_in_use_and_their_languages(self):
        self.assertEqual(sami.classes(BOTH_IN_ONE_SYNC), ["KRCC", "ENCC"])
        self.assertEqual([sami.class_language(BOTH_IN_ONE_SYNC, c) for c in ("KRCC", "ENCC")], ["ko", "en"])

    def test_without_a_lang_property_the_class_name_tells_the_language(self):
        text = document("<SYNC Start=1000><P Class=JPCC>x\n", style="")
        self.assertEqual(sami.class_language(text, "JPCC"), "ja")
        self.assertEqual(sami.class_language(text, "ENUSCC"), "en")
        self.assertIsNone(sami.class_language(text, "SUBTTL"))


class KeepLanguage(unittest.TestCase):
    def test_a_file_with_one_class_comes_back_unchanged_whatever_its_language(self):
        for body in ("<SYNC Start=1000><P Class=KRCC>안녕\n", "<SYNC Start=1000><P Class=ENCC>Hello\n"):
            text = document(body)
            self.assertEqual(sami.keep_language(text, "ko"), (text, ()))

    def test_only_the_paragraphs_of_the_language_stay_whatever_the_layout(self):
        for text in (BOTH_IN_ONE_SYNC, ONE_SYNC_EACH):
            kept, dropped = sami.keep_language(text, "ko")
            self.assertEqual(dropped, ("ENCC",))
            self.assertNotIn("Hello", kept)
            self.assertIn("<SYNC Start=1000><P Class=KRCC>안녕", kept)
            self.assertIn("<SYNC Start=3000><P Class=KRCC>&nbsp;", kept)
            self.assertEqual(kept.count("<SYNC"), 2, "a sync that only held the other language is gone")
            self.assertTrue(kept.endswith("</BODY></SAMI>\n"))

    def test_no_class_or_several_classes_in_the_language_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "none of them is Japanese"):
            sami.keep_language(BOTH_IN_ONE_SYNC, "ja")
        twice = BOTH_IN_ONE_SYNC.replace("lang:en-US", "lang:ko")
        with self.assertRaisesRegex(ValueError, "more than one of them is Korean"):
            sami.keep_language(twice, "ko")


if __name__ == "__main__":
    unittest.main()
