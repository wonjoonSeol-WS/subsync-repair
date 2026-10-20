"""Tests for subsync_repair/formats/names.py and languages.py: which video and language a subtitle file belongs to."""
import os
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.formats import languages, names
from subsync_repair.formats.names import SubtitleName
from subsync_repair.storage import library
from support import TempFolder


class Languages(unittest.TestCase):
    def test_every_tag_of_every_language_is_known_and_regional_tags_count(self):
        for code, language in languages.LANGUAGES.items():
            for tag in language.tags:
                self.assertEqual(languages.lang_of(tag.upper()), code, tag)
        self.assertEqual([languages.lang_of(t) for t in ("pt-BR", "zh_TW", "en-us", "klingon", "")],
                         ["pt", "zh", "en", None, None])

    def test_bazarrs_own_codes_for_language_variants(self):
        self.assertEqual([languages.lang_of(t) for t in ("pob", "pb", "zht", "zt", "zhs", "spl")],
                         ["pt", "pt", "zh", "zh", "zh", "es"])

    def test_the_twenty_languages_of_the_extension(self):
        self.assertEqual(len(languages.LANGUAGES), 20)
        self.assertEqual(languages.name_of("de"), "German")

    def test_a_setting_with_an_unknown_language_names_the_setting(self):
        self.assertEqual(languages.language_code("DE", "target_language"), "de")
        with self.assertRaisesRegex(ValueError, "target_language: unknown language 'xx'"):
            languages.language_code("xx", "target_language")


class Parse(unittest.TestCase):
    def test_with_the_video_known_the_first_tag_after_its_name_is_the_language(self):
        self.assertEqual(names.parse("Movie.en.ko.srt", "Movie"), SubtitleName("Movie", "en", ("ko",), ".srt"))
        self.assertEqual(names.parse("Show.en.hi.srt", "Show"), SubtitleName("Show", "en", ("hi",), ".srt"))
        self.assertEqual(names.parse("Show.hi.srt", "Show").lang, "hi")
        self.assertEqual(names.parse("SHOW.KOR.Forced.SRT", "Show"), SubtitleName("Show", "ko", ("forced",), ".srt"))
        self.assertIsNone(names.parse("Movie.Extended.ko.srt", "Movie"))
        self.assertIsNone(names.parse("Movie.ko.txt", "Movie"))
        self.assertIsNone(names.parse("Other.ko.srt", "Movie"))

    def test_a_name_matches_its_video_letter_for_letter_whatever_the_case(self):
        self.assertIsNone(names.parse("straße.xko.srt", "STRASSE"))
        self.assertIsNone(names.parse("STRASSE.ko.srt", "Straße"))
        self.assertEqual(names.parse("STRAßE.ko.srt", "straße").lang, "ko")

    def test_flags_joined_with_a_dash_count(self):
        self.assertEqual(names.parse("Show.en-forced.srt", "Show"), SubtitleName("Show", "en", ("forced",), ".srt"))
        self.assertEqual(names.parse("Show.en.x-forced.srt", "Show").flags, ("x", "forced"))
        self.assertFalse(names.parse("Show.en.x-forced.srt").dialogue)
        self.assertEqual(names.parse("Show.pt-BR.srt", "Show"), SubtitleName("Show", "pt", (), ".srt"))

    def test_without_the_video_the_language_is_the_leftmost_tag_that_only_flags_follow(self):
        cases = {"Show S01E01.ko.srt": ("Show S01E01", "ko", ()),
                 "Show.en.hi.srt": ("Show", "en", ("hi",)),
                 "Show.hi.srt": ("Show", "hi", ()),
                 "Mr. Robot S01E01.ko.hi.srt": ("Mr. Robot S01E01", "ko", ("hi",)),
                 "Show S01E01.ko.clean.srt": ("Show S01E01", "ko", ("clean",)),
                 "Movie.KR.WEBRip.AAC.ko.srt": ("Movie.KR.WEBRip.AAC", "ko", ()),
                 "Show.pt-BR.srt": ("Show", "pt", ()),
                 "Show S01E01.Directors.Cut.ja.srt": ("Show S01E01.Directors.Cut", "ja", ())}
        for name, (stem, lang, flags) in cases.items():
            self.assertEqual(names.parse(name), SubtitleName(stem, lang, flags, ".srt"), name)
        for name in ("Show S01E01.korean.srt", "Show S01E01.ko.smi.bak", "Show S01E01.mkv", "ko.srt",
                     "Movie.ko.1080p.srt"):
            self.assertIsNone(names.parse(name), name)

    def test_dialogue_and_synced_flags(self):
        self.assertFalse(names.parse("Show.ko.forced.srt").dialogue)
        self.assertFalse(names.parse("Show.ja.signs.srt").dialogue)
        self.assertTrue(names.parse("Show.ko.hi.srt").dialogue)
        self.assertTrue(names.parse("Show.ko.synced.srt").synced)
        self.assertFalse(names.parse("Show.ko.synced.smi").synced)
        self.assertFalse(names.parse("Show.ko.synced.hi.srt", "Show").synced)


class SyncedCopies(unittest.TestCase):
    def test_every_subtitle_gets_its_own_copy_that_keeps_its_tags(self):
        cases = {"Show.ko.srt": "Show.ko.synced.srt", "Show.ko.hi.srt": "Show.ko.hi.synced.srt",
                 "Show.ko.smi": "Show.ko.smi.synced.srt", "Show.ko.ass": "Show.ko.ass.synced.srt",
                 "Show.ja.srt": "Show.ja.synced.srt"}
        self.assertEqual({k: names.synced_name(Path(k)) for k in cases}, cases)

    def test_a_copy_is_never_taken_for_a_subtitle_to_repair(self):
        for name in ("Show.ko.synced.srt", "Show.ko.hi.synced.srt", "Show.ko.smi.synced.srt"):
            self.assertTrue(names.parse(name, "Show").synced, name)
            self.assertTrue(names.parse(name).synced, name)


class Folder(TempFolder):
    def files(self, *names_):
        for name in names_:
            (self.tmp / name).write_bytes(b"")
        return library.listing(self.tmp)

    def test_videos_are_the_files_jellyfin_plays(self):
        for ext in (".mkv", ".mp4", ".ts", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg", ".ogv", ".flv", ".3gp", ".webm"):
            self.assertEqual(names.videos([Path("Movie" + ext.upper())]), [Path("Movie" + ext.upper())], ext)
        self.assertEqual(names.videos([Path("Movie.iso"), Path("Movie.strm"), Path("Movie.srt")]), [])

    def test_a_subtitle_belongs_to_the_video_whose_whole_name_it_extends(self):
        entries = self.files("Show S01E01.mkv", "Show S01E02.mkv", "Show S01E01.Directors.Cut.mkv")
        for sub, video in (("Show S01E01.ko.srt", "Show S01E01.mkv"), ("Show S01E02.ko.srt", "Show S01E02.mkv"),
                           ("Show S01E01.Directors.Cut.ko.srt", "Show S01E01.Directors.Cut.mkv"),
                           ("show s01e02.KO.srt", "Show S01E02.mkv"), ("Other.ko.srt", None)):
            found = names.video_of(self.tmp / sub, entries)
            self.assertEqual(found.name if found else None, video, sub)

    def test_the_longest_matching_video_name_wins(self):
        entries = self.files("Movie.avi", "Movie.en.mkv")
        self.assertEqual([p.name for p in names.videos(entries)], ["Movie.avi", "Movie.en.mkv"], "shorter first")
        name, video = names.identify(self.tmp / "Movie.en.ko.srt", entries)
        self.assertEqual((video.name, name.lang), ("Movie.en.mkv", "ko"))

    def test_identify_uses_the_video_when_there_is_one(self):
        entries = self.files("Movie.mkv")
        self.assertEqual(names.identify(self.tmp / "Movie.en.ko.srt", entries)[0].lang, "en")
        self.assertEqual(names.identify(self.tmp / "Film.en.ko.srt", entries)[0].lang, "ko")

    def test_sidecars_belong_to_one_video(self):
        entries = self.files("Show.mkv", "Show.ko.srt", "Show.ja.hi.srt", "Show.Extended.en.srt", "Show2.ko.srt")
        self.assertEqual([p.name for _, p in names.sidecars("Show", entries)], ["Show.ja.hi.srt", "Show.ko.srt"])

    def test_a_twin_prefers_the_file_without_flags_then_the_fuller_one_and_skips_forced_and_synced_files(self):
        entries = self.files("Show.ko.ass", "Show.ko.clean.srt", "Show.ko.hi.srt", "Show.kor.srt",
                             "Show.ko.forced.srt", "Show.ko.synced.srt")
        (self.tmp / "Show.ko.hi.srt").write_bytes(b"a fuller file")
        ass_file = self.tmp / "Show.ko.ass"
        name = names.parse(ass_file.name, "Show")
        self.assertEqual(names.twin(ass_file, name, entries, {".srt"}).name, "Show.kor.srt")
        entries = [p for p in entries if p.name != "Show.kor.srt"]
        self.assertEqual(names.twin(ass_file, name, entries, {".srt"}).name, "Show.ko.hi.srt")
        (self.tmp / "Show.ko.hi.srt").write_bytes(b"")
        self.assertEqual(names.twin(ass_file, name, entries, {".srt"}).name, "Show.ko.clean.srt")
        self.assertIsNone(names.twin(ass_file, name, entries, {".ssa"}))

    def test_another_language_keeps_the_video_and_the_flags(self):
        self.assertEqual(names.renamed(SubtitleName("Show S01E01", "ko", ("hi",), ".smi"), "en"),
                         "Show S01E01.en.hi.srt")

    def test_the_listing_holds_files_only_and_an_unreadable_folder_has_none(self):
        self.files("b.srt", "a.mkv")
        (self.tmp / "folder.srt").mkdir()
        self.assertEqual([p.name for p in library.listing(self.tmp)], ["a.mkv", "b.srt"])
        self.assertEqual(library.listing(self.tmp / "missing"), [])

    def test_the_listing_does_not_stat_every_file(self):
        self.files("a.mkv", "a.ko.srt", "a.ja.srt")
        with mock.patch.object(Path, "stat", side_effect=AssertionError("a stat per file")), \
                mock.patch.object(os, "stat", side_effect=AssertionError("a stat per file")):
            self.assertEqual(len(library.listing(self.tmp)), 3)


if __name__ == "__main__":
    unittest.main()
