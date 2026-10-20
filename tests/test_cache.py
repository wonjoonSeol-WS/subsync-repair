"""Tests for subsync_repair/engine/cache.py: the verdict cache."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.engine import cache
from subsync_repair.engine.cache import VerdictCache
from subsync_repair.engine.verdicts import Status, Verdict
from subsync_repair.storage import fileio
from subsync_repair.storage.library import lexical_key
from support import TempFolder


class Cache(TempFolder):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "data" / "cache.json"

    def test_entries_round_trip(self):
        verdicts = VerdictCache(self.path, "settings-1")
        verdicts.put(self.tmp / "a.ko.srt", "sig", Verdict(Status.OK, "in sync"))
        self.assertTrue(verdicts.save())
        self.assertEqual(VerdictCache(self.path, "settings-1").load().get(self.tmp / "a.ko.srt", "sig"),
                         {"sig": "sig", "status": "ok", "note": "in sync"})

    def test_saving_replaces_the_file_in_one_step(self):
        VerdictCache(self.path, "s").save()
        with mock.patch.object(fileio, "atomic_write_bytes", wraps=fileio.atomic_write_bytes) as write:
            VerdictCache(self.path, "s").save()
        self.assertEqual(write.call_args.args[0], self.path)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["cache.json"])

    def test_a_save_that_fails_is_a_warning(self):
        with mock.patch.object(fileio, "atomic_write_bytes", side_effect=OSError(28, "disk full")), \
                self.assertLogs("subsync_repair.engine.cache", "WARNING"):
            self.assertFalse(VerdictCache(self.path, "s").save())

    def test_entries_are_kept_by_the_lexical_key_of_the_path(self):
        verdicts = VerdictCache(self.path, "s")
        verdicts.put(self.tmp / "A.ko.srt", "sig", Verdict(Status.OK, "in sync"))
        self.assertEqual(set(verdicts.files), {lexical_key(self.tmp / "A.ko.srt")})

    def test_an_entry_only_answers_for_the_same_files_and_a_known_status(self):
        verdicts = VerdictCache(self.path, "s")
        a, b = lexical_key("/x/a"), lexical_key("/x/b")
        verdicts.files = {a: {"sig": "1", "status": "ok"}, b: {"sig": "1", "status": "weird"}}
        self.assertIsNone(verdicts.get("/x/a", "2"))
        self.assertIsNone(verdicts.get("/x/b", "1"))
        self.assertEqual(verdicts.get("/x/a", "1"), {"sig": "1", "status": "ok"})

    def test_other_settings_or_another_format_version_start_empty(self):
        verdicts = VerdictCache(self.path, "settings-1")
        verdicts.put("a", "sig", Verdict(Status.SKIP, "x"))
        verdicts.save()
        self.assertEqual(VerdictCache(self.path, "settings-2").load().files, {})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        for version in (cache.FORMAT_VERSION - 1, cache.FORMAT_VERSION + 1):
            self.path.write_text(json.dumps({**saved, "version": version}), encoding="utf-8")
            self.assertEqual(VerdictCache(self.path, "settings-1").load().files, {}, version)
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(len(VerdictCache(self.path, "settings-1").load().files), 1)

    def test_entries_that_are_not_objects_are_dropped(self):
        good = {"sig": "1", "status": "ok", "note": "in sync"}
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": cache.FORMAT_VERSION, "settings": "s",
                                         "files": {"a": good, "b": "text", "c": [1]}}), encoding="utf-8")
        self.assertEqual(VerdictCache(self.path, "s").load().files, {"a": good})

    def test_an_unreadable_file_is_ignored_with_a_warning_and_a_missing_one_silently(self):
        with self.assertNoLogs("subsync_repair", "WARNING"):
            self.assertEqual(VerdictCache(self.path, "s").load().files, {})
        self.path.parent.mkdir(parents=True)
        self.path.write_text("not json", encoding="utf-8")
        with self.assertLogs("subsync_repair", "WARNING"):
            self.assertEqual(VerdictCache(self.path, "s").load().files, {})

    def test_pruning_forgets_unseen_files_under_the_scanned_folder_unless_they_are_still_there_to_repair(self):
        verdicts = VerdictCache(self.path, "s")
        lib, other = Path(os.path.realpath(self.tmp)) / "lib", Path(os.path.realpath(self.tmp)) / "other"
        for p in (lib / "a.srt", lib / "b.srt", lib / "kept.srt", other / "c.srt"):
            verdicts.put(p, "sig", Verdict(Status.OK, "in sync"))
        verdicts.prune(lib, {lexical_key(lib / "a.srt")}, keep=lambda key: key == lexical_key(lib / "kept.srt"))
        self.assertEqual(set(verdicts.files), {lexical_key(p) for p in (lib / "a.srt", lib / "kept.srt",
                                                                         other / "c.srt")})


if __name__ == "__main__":
    unittest.main()
