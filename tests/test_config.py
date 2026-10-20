"""Tests for subsync_repair/config.py: every setting is validated, nothing is read at import time, and the defaults
depend only on the platform and the environment given."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from subsync_repair import config
from subsync_repair.config import ConfigError, parse_settings
from support import CHROME_ID, FIREFOX_ID, TempFolder

ROOT = config.ROOT


class WithLibrary(TempFolder):
    """Settings whose library folder exists."""

    def setUp(self):
        super().setUp()
        self.library = self.tmp / "library"
        self.library.mkdir()
        self.minimal = {"target_language": "en", "libraries": [str(self.library)]}

    def parse(self, platform="linux", env=None, base=None, **cfg):
        return parse_settings({**self.minimal, **cfg}, platform=platform, env={"HOME": "/home/me"} if env is None
                              else env, base=base or self.tmp)


class File(WithLibrary):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "config.json"

    def test_a_missing_file_says_how_to_make_one(self):
        with self.assertRaisesRegex(ConfigError, "copy config.example.json"):
            config.load_settings(self.path)

    def test_broken_json_and_a_non_object_are_clear_errors(self):
        for text, why in (("{not json", "not valid JSON"), ("[]", "JSON object")):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, why):
                config.load_settings(self.path)

    def test_a_windows_path_with_single_backslashes_gets_a_hint(self):
        self.path.write_text('{"target_language": "en", "libraries": ["C:\\Media"]}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, r"write Windows paths with / or \\\\"):
            config.load_settings(self.path)

    def test_a_file_that_is_not_utf8_is_named_as_such(self):
        text = json.dumps({**self.minimal, "skip_dirs": ["특별편"]}, ensure_ascii=False)
        for encoding in ("utf-16", "cp949"):
            self.path.write_bytes(text.encode(encoding))
            with self.assertRaisesRegex(ConfigError, "must be saved as UTF-8"):
                config.load_settings(self.path, env={"HOME": "/home/me", "PROGRAMDATA": "C:\\ProgramData"})

    def test_a_file_with_a_byte_order_mark_is_read(self):
        self.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(self.minimal).encode("utf-8"))
        self.assertEqual(config.load_settings(self.path, platform="linux", env={"HOME": "/h"}).target, "en")

    def test_relative_paths_start_at_the_config_files_folder(self):
        self.path.write_text(json.dumps({**self.minimal, "tools": {"data_dir": "data", "alass": "tools/alass-cli",
                                                                   "ffsubsync_libs": ["libs"]}}), encoding="utf-8")
        tools = config.load_settings(self.path, platform="linux", env={"HOME": "/h"}).tools
        self.assertEqual((tools.data_dir, tools.alass, tools.ffsubsync_libs),
                         (self.tmp / "data", str(self.tmp / "tools" / "alass-cli"), (str(self.tmp / "libs"),)))

    def test_the_example_file_is_valid_once_its_libraries_are_real_folders(self):
        example = config.read_config(config.ROOT / "config.example.json")
        settings = parse_settings({**example, "libraries": [str(self.library)]}, platform="linux",
                                  env={"HOME": "/home/me"})
        self.assertEqual((settings.port, settings.extension_ids, settings.warnings), (config.DEFAULT_PORT, (), ()))
        self.assertNotIn(settings.target, settings.sibling_order + settings.embedded_order)
        self.assertNotIn("data_dir", example["tools"])

    def test_importing_the_command_reads_no_settings(self):
        code = ("from unittest import mock\nfrom subsync_repair import config\n"
                "with mock.patch.object(config, 'read_config', side_effect=AssertionError('read at import')):\n"
                "    import subsync_repair.cli, subsync_repair.server\n    import runpy\n"
                "    runpy.run_path('subsync-repair.py', run_name='not_main')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


class Settings(WithLibrary):
    def test_the_target_language_is_required_and_validated(self):
        for cfg in ({"libraries": [str(self.library)]}, {"target_language": None, "libraries": [str(self.library)]}):
            with self.assertRaisesRegex(ConfigError, "target_language is required"):
                parse_settings(cfg, platform="linux", env={"HOME": "/h"})
        with self.assertRaisesRegex(ConfigError, "target_language: unknown language 'xx'"):
            self.parse(target_language="xx")
        self.assertEqual(self.parse(target_language="DE").target, "de")

    def test_libraries_are_a_list_of_absolute_folders_given_as_text_or_objects(self):
        settings = self.parse(libraries=[str(self.library), {"path": str(self.library)}])
        self.assertEqual(list(settings.libraries), [self.library, self.library])
        for bad in (None, [], ["relative/path"], [{"name": str(self.library)}], [5], str(self.library)):
            with self.assertRaisesRegex(ConfigError, "libraries", msg=repr(bad)):
                self.parse(libraries=bad)

    def test_missing_library_folders_and_unknown_library_keys_are_warnings(self):
        missing = str(self.tmp / "missing")
        warnings = self.parse(libraries=[missing, {"path": str(self.library), "sources": ["ja"]}]).warnings
        self.assertEqual(warnings, (f"library folder {missing} does not exist",
                                    "setting 'libraries[1].sources' is not used"))

    def test_tools_default_to_names_on_path(self):
        tools = self.parse().tools
        self.assertEqual((tools.alass, tools.ffmpeg, tools.ffprobe, tools.ffsubsync_python, tools.ffsubsync_libs),
                         ("alass-cli", "ffmpeg", "ffprobe", "", ()))

    def test_every_tool_can_be_set(self):
        given = {key: str(self.tmp / key) for key in ("data_dir", "alass", "ffmpeg", "ffprobe", "ffsubsync_python")}
        tools = self.parse(tools={**given, "ffsubsync_libs": [str(self.tmp / "libs")]}).tools
        self.assertEqual((tools.data_dir, tools.alass, tools.ffmpeg, tools.ffprobe, tools.ffsubsync_python,
                          tools.ffsubsync_libs),
                         (self.tmp / "data_dir", given["alass"], given["ffmpeg"], given["ffprobe"],
                          given["ffsubsync_python"], (str(self.tmp / "libs"),)))
        self.assertEqual(self.parse(tools={"alass": "my-alass"}).tools.alass, "my-alass")

    def test_a_home_relative_path_is_expanded(self):
        self.assertEqual(self.parse(tools={"data_dir": "~/subsync"}).tools.data_dir,
                         Path(os.path.expanduser("~/subsync")))

    def test_malformed_tools_and_sections_are_errors_not_silent_defaults(self):
        for tools in ("text", [], 0, False, {"alass": 5}, {"alass": 0}, {"alass": ""}, {"data_dir": ""},
                      {"ffsubsync_libs": "C:/libs"}, {"ffsubsync_libs": 0}):
            with self.assertRaises(ConfigError, msg=repr(tools)):
                self.parse(tools=tools)
        for sync in ("text", [], 0):
            with self.assertRaises(ConfigError, msg=repr(sync)):
                self.parse(sync=sync)
        self.assertEqual(self.parse(tools={"ffsubsync_python": ""}).tools.ffsubsync_python, "")

    def test_the_data_folder_follows_the_platform_and_its_environment(self):
        home = {"HOME": "/home/me"}
        self.assertEqual(self.parse("win32", {"PROGRAMDATA": "D:\\ProgramData"}).tools.data_dir,
                         Path("D:\\ProgramData") / "subsync-repair")
        self.assertEqual(self.parse("darwin", home).tools.data_dir,
                         Path("/home/me") / "Library" / "Application Support" / "subsync-repair")
        self.assertEqual(self.parse("linux", home).tools.data_dir, Path("/home/me") / ".local" / "share" /
                         "subsync-repair")
        self.assertEqual(self.parse("linux", {**home, "XDG_DATA_HOME": "/xdg"}).tools.data_dir,
                         Path("/xdg") / "subsync-repair")
        self.assertEqual(self.parse("linux", {**home, "XDG_DATA_HOME": "relative"}).tools.data_dir.parent,
                         Path("/home/me") / ".local" / "share")
        for platform, env in (("win32", {}), ("linux", {}), ("darwin", {})):
            with self.assertRaisesRegex(ConfigError, "tools.data_dir", msg=platform):
                self.parse(platform, env)

    def test_a_default_data_folder_in_a_container_home_is_a_warning(self):
        for home in ("/", "/root"):
            self.assertIn("mounted volume", self.parse("linux", {"HOME": home}).warnings[0], home)
        self.assertEqual(self.parse("linux", {"HOME": "/root"}, tools={"data_dir": "/config"}).warnings, ())

    def test_reference_languages_keep_their_order_and_default_to_english_then_any(self):
        self.assertEqual((self.parse().sibling_order, self.parse().embedded_order), (("en", "*"), ("en", "*")))
        settings = self.parse(reference_languages=["en", "fr", "*"], embedded_reference_languages=["fr"])
        self.assertEqual((settings.sibling_order, settings.embedded_order), (("en", "fr", "*"), ("fr",)))
        for bad in (["zz"], "en", [5]):
            with self.assertRaisesRegex(ConfigError, "reference_languages", msg=repr(bad)):
                self.parse(reference_languages=bad)

    def test_extension_ids_must_look_like_chrome_or_firefox_ids_or_the_wildcard(self):
        self.assertEqual(self.parse(sync={"extension_ids": [CHROME_ID, FIREFOX_ID, "*"]}).extension_ids,
                         (CHROME_ID, FIREFOX_ID, "*"))
        self.assertEqual(self.parse().extension_ids, ())
        for bad in ("goodid", CHROME_ID + "\n", "chrome-extension://" + CHROME_ID):
            with self.assertRaisesRegex(ConfigError, "sync.extension_ids", msg=bad):
                self.parse(sync={"extension_ids": [bad]})

    def test_the_port_is_a_real_port(self):
        self.assertEqual(self.parse(sync={"port": 9000}).port, 9000)
        self.assertEqual(self.parse(sync=None).port, config.DEFAULT_PORT)
        for bad in (0, 70000, "8778", True, False, 80.5):
            with self.assertRaisesRegex(ConfigError, "sync.port", msg=repr(bad)):
                self.parse(sync={"port": bad})

    def test_settings_this_tool_does_not_use_are_reported(self):
        settings = self.parse(unused={}, another=5, tools={"other_tool": "x"}, sync={"token": "y"})
        self.assertEqual(settings.warnings, ("setting 'unused' is not used", "setting 'another' is not used",
                                             "setting 'tools.other_tool' is not used",
                                             "setting 'sync.token' is not used"))


if __name__ == "__main__":
    unittest.main()
