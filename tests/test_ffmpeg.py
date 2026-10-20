"""Tests for subsync_repair/external/ffmpeg.py with a fake ffprobe/ffmpeg (process.run is replaced)."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair.external import ffmpeg, process
from subsync_repair.external.programs import ToolError
from subsync_repair.formats import srt
from support import TempFolder, completed, srt_of


def answer(payload=None, code=0, stderr=""):
    payload = payload if payload is not None else {"format": {"duration": "60.5"}, "streams": []}
    return completed(payload if isinstance(payload, str) else json.dumps(payload), stderr, code)


def stream(index, lang, codec="subrip", title="", forced=0, frames=None):
    tags = {"language": lang, "title": title, **({"NUMBER_OF_FRAMES-eng": str(frames)} if frames else {})}
    return {"index": index, "codec_type": "subtitle", "codec_name": codec, "tags": tags,
            "disposition": {"forced": forced}}


def english(code):
    return code == "en"


class Probe(TempFolder):
    def setUp(self):
        super().setUp()
        self.video = self.tmp / "v.mkv"
        self.video.write_bytes(b"x")
        self.media = ffmpeg.Media("my-ffmpeg", "my-ffprobe")

    def test_a_probe_uses_the_configured_tool_and_is_cached_until_the_file_changes(self):
        with mock.patch.object(process, "run", return_value=answer()) as run:
            self.assertEqual(self.media.duration(self.video), 60.5)
            self.media.probe(self.video)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "my-ffprobe")
            self.video.write_bytes(b"a replaced, longer file")
            self.media.probe(self.video)
            self.assertEqual(run.call_count, 2)

    def test_a_failed_or_strange_probe_is_logged_and_not_cached(self):
        for reply in (answer("", code=1, stderr="moov atom not found"), answer("[1, 2]"), answer("not json")):
            with mock.patch.object(process, "run", return_value=reply) as run, \
                    self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
                self.assertEqual(self.media.probe(self.video), {})
                self.assertEqual(self.media.probe(self.video), {})
                self.assertEqual(run.call_count, 2)
        with mock.patch.object(process, "run", return_value=answer()):
            self.assertEqual(self.media.duration(self.video), 60.5)

    def test_an_ffprobe_that_cannot_run_is_a_tool_error_and_a_missing_video_has_nothing(self):
        for failure, message in ((FileNotFoundError("ffprobe"), "could not be started"),
                                 (subprocess.TimeoutExpired("ffprobe", 60), "took longer")):
            with mock.patch.object(process, "run", side_effect=failure):
                with self.assertRaisesRegex(ToolError, message) as raised:
                    self.media.probe(self.video)
                self.assertNotIn("my-ffprobe", raised.exception.message)
        self.assertIsNone(self.media.duration(self.tmp / "missing.mkv"))

    def test_the_cache_starts_over_when_it_is_full(self):
        sizes = []
        with mock.patch.object(ffmpeg, "MEMO_LIMIT", 2), mock.patch.object(process, "run", return_value=answer()):
            probes = ffmpeg.Media()
            for i in range(5):
                f = self.tmp / f"{i}.mkv"
                f.write_bytes(b"x")
                probes.probe(f)
                sizes.append(len(probes._probes))
        self.assertEqual(sizes, [1, 2, 1, 2, 1])

    def test_dialogue_tracks_leave_out_forced_signs_commentary_image_and_malformed_tracks(self):
        streams = [stream(1, "eng", title="Signs & Songs"), stream(2, "eng", forced=1),
                   stream(3, "eng", "hdmv_pgs_subtitle"), stream(4, "eng", title="Commentary"),
                   stream(5, "eng", title="Designated Survivor"), stream(6, "jpn"), {"index": 7, "codec_type": "audio"},
                   {**stream(8, "eng"), "index": "x"}, {**stream(9, "eng"), "index": None}, "not a stream"]
        with mock.patch.object(process, "run", return_value=answer({"streams": streams})):
            self.assertEqual([s.index for s in self.media.dialogue_tracks(self.video, english)], [5])
            self.assertEqual([s.index for s in self.media.dialogue_tracks(self.video, lambda c: c == "ja")], [6])
            self.assertEqual([s.language for s in self.media.subtitle_streams(self.video)][:2], ["en", "en"])


class Ffmpeg(TempFolder):
    def setUp(self):
        super().setUp()
        self.media = ffmpeg.Media()

    def test_success_needs_both_a_zero_exit_and_an_output_file(self):
        out = self.tmp / "o.srt"
        with mock.patch.object(process, "run", return_value=answer(code=0)) as run, \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING") as logs:
            self.assertFalse(self.media.to_srt("in.mkv", out))
        self.assertIn("wrote no output", logs.output[0])
        self.assertIn("-nostdin", run.call_args.args[0])
        out.write_text("x")
        with mock.patch.object(process, "run", return_value=answer(code=0)):
            self.assertTrue(self.media.to_srt("in.mkv", out))
        with mock.patch.object(process, "run", return_value=answer(code=1, stderr="Invalid data found")), \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING") as logs:
            self.assertFalse(self.media.to_srt("in.mkv", out))
        self.assertIn("Invalid data found", logs.output[0])

    def test_a_stream_index_zero_is_mapped(self):
        with mock.patch.object(process, "run", return_value=answer(code=1)) as run, \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            self.media.to_srt("in.mkv", "o.srt", 0)
        self.assertIn("0:0", run.call_args.args[0])

    def test_an_ffmpeg_that_cannot_run_is_a_tool_error(self):
        for failure in (FileNotFoundError("ffmpeg"), subprocess.TimeoutExpired("ffmpeg", 600)):
            with mock.patch.object(process, "run", side_effect=failure), self.assertRaises(ToolError):
                self.media.to_srt("in.mkv", "o.srt")


class Tracks(TempFolder):
    """Which tracks get extracted, with a fake ffmpeg that writes the requested outputs."""

    def setUp(self):
        super().setUp()
        self.video = self.tmp / "v.mkv"
        self.video.write_bytes(b"x")
        self.media = ffmpeg.Media()
        self.cue_counts = {}
        self.commands = []
        self.write_only_first = False

    def fake_run(self, cmd, timeout):
        if "-show_entries" in cmd:
            return answer({"streams": self.streams})
        self.commands.append(cmd)
        maps = [i for i, arg in enumerate(cmd) if arg == "-map"]
        for i in maps[:1] if self.write_only_first and len(maps) > 1 else maps:
            index = int(cmd[i + 1].split(":")[1])
            n = self.cue_counts[index]
            Path(cmd[i + 2]).write_text(srt_of([float(s) for s in range(n)], [f"t{s}" for s in range(n)]),
                                        encoding="utf-8")
        return answer(code=0)

    def tracks(self, accept=english):
        with mock.patch.object(process, "run", side_effect=self.fake_run):
            return [(s.index, len(cues)) for s, cues in self.media.tracks_by_size(self.video, accept, self.tmp)]

    def test_without_frame_counts_every_track_is_read_in_one_pass_and_the_fullest_comes_first(self):
        self.streams = [stream(1, "eng"), stream(2, "eng"), stream(3, "jpn")]
        self.cue_counts = {1: 60, 2: 90, 3: 500}
        self.assertEqual(self.tracks(), [(2, 90), (1, 60)])
        self.assertEqual(len(self.commands), 1)

    def test_tracks_already_read_from_the_same_video_are_not_read_again(self):
        self.streams = [stream(1, "eng"), stream(2, "eng")]
        self.cue_counts = {1: 60, 2: 90}
        self.assertEqual(self.tracks(), self.tracks())
        self.assertEqual(len(self.commands), 1)
        self.video.write_bytes(b"a replaced video")
        self.tracks()
        self.assertEqual(len(self.commands), 2)

    def test_with_frame_counts_only_the_tracks_asked_for_are_read_in_the_order_of_the_counts(self):
        self.streams = [stream(1, "eng", frames=900), stream(2, "eng", frames=60), stream(3, "eng", frames=300)]
        self.cue_counts = {1: 900, 2: 60, 3: 300}
        with mock.patch.object(process, "run", side_effect=self.fake_run):
            found = self.media.tracks_by_size(self.video, english, self.tmp)
            first = next(found)
            self.assertEqual((first[0].index, len(first[1]), len(self.commands)), (1, 900, 1))
            self.assertEqual([s.index for s, _ in found], [3, 2])

    def test_frame_counts_decide_the_order_even_when_the_tracks_disagree(self):
        self.streams = [stream(1, "eng", frames=900), stream(2, "eng", frames=60)]
        self.cue_counts = {1: 55, 2: 300}
        self.assertEqual(self.tracks(), [(1, 55), (2, 300)])

    def test_when_some_tracks_lack_frame_counts_all_are_read_and_sorted_by_their_cues(self):
        self.streams = [stream(1, "eng", frames=900), stream(2, "eng")]
        self.cue_counts = {1: 60, 2: 90}
        self.assertEqual(self.tracks(), [(2, 90), (1, 60)])
        self.assertEqual(len(self.commands), 1)

    def test_with_frame_counts_a_track_that_cannot_be_read_is_passed_over(self):
        self.streams = [stream(1, "eng", frames=60), stream(2, "eng", frames=900)]
        self.cue_counts = {1: 60}
        real = self.fake_run

        def broken_second(cmd, timeout):
            if "0:2" in cmd:
                self.commands.append(cmd)
                return answer(code=1, stderr="broken track")
            return real(cmd, timeout)

        with mock.patch.object(process, "run", side_effect=broken_second), \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            found = [(s.index, len(c)) for s, c in self.media.tracks_by_size(self.video, english, self.tmp)]
        self.assertEqual(found, [(1, 60)])

    def test_when_the_single_pass_fails_each_track_is_tried_on_its_own(self):
        self.streams = [stream(1, "eng"), stream(2, "eng")]
        self.cue_counts = {1: 60, 2: 90}
        real = self.fake_run

        def first_pass_fails(cmd, timeout):
            if cmd.count("-map") > 1:
                self.commands.append(cmd)
                return answer(code=1, stderr="one track is broken")
            return real(cmd, timeout)

        with mock.patch.object(process, "run", side_effect=first_pass_fails), \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            found = [(s.index, len(c)) for s, c in self.media.tracks_by_size(self.video, english, self.tmp)]
        self.assertEqual(found, [(2, 90), (1, 60)])
        self.assertEqual(len(self.commands), 3)

    def test_a_single_pass_that_writes_only_some_tracks_is_redone_track_by_track(self):
        self.streams = [stream(1, "eng"), stream(2, "eng")]
        self.cue_counts = {1: 60, 2: 90}
        self.write_only_first = True
        with self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            self.assertEqual(self.tracks(), [(2, 90), (1, 60)])
        self.assertEqual(len(self.commands), 3)

    def test_a_single_track_that_fails_is_not_tried_again(self):
        self.streams = [stream(1, "eng")]

        def fails(cmd, timeout):
            if "-show_entries" in cmd:
                return answer({"streams": self.streams})
            self.commands.append(cmd)
            return answer(code=1, stderr="broken")

        with mock.patch.object(process, "run", side_effect=fails), \
                self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            self.assertEqual(list(self.media.tracks_by_size(self.video, english, self.tmp)), [])
        self.assertEqual(len(self.commands), 1)


class ReadSubtitle(TempFolder):
    def test_srt_and_ass_are_read_without_ffmpeg_and_line_endings_are_reported(self):
        m = ffmpeg.Media("no-such-ffmpeg")
        text = m.read_subtitle(srt_of([1.0], ["안녕하세요"]).replace("\n", "\r\n").encode("cp949"), ".srt", "ko",
                               self.tmp)
        self.assertEqual((text.cues[0].text, text.encoding, text.crlf), ("안녕하세요", "cp949", True))
        ass_text = ("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                    "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,안녕하세요\n")
        self.assertEqual(m.read_subtitle(ass_text.encode("utf-8"), ".ass", "ko", self.tmp).cues,
                         [srt.Cue(1.0, 2.0, "안녕하세요")])

    def test_old_mac_line_endings_are_not_windows_ones(self):
        text = ffmpeg.Media().read_subtitle(srt_of([1.0, 3.0], ["hello", "there"]).replace("\n", "\r").encode(),
                                           ".srt", "en", self.tmp)
        self.assertEqual((len(text.cues), text.crlf), (2, False))

    def test_a_conversion_failure_is_an_error(self):
        failing = mock.patch.object(process, "run", return_value=answer(code=1))
        with failing, self.assertLogs("subsync_repair.external.ffmpeg", "WARNING"):
            with self.assertRaisesRegex(ffmpeg.ConversionError, "could not convert the .smi file"):
                ffmpeg.Media().read_subtitle("<SAMI><P Class=KRCC>안녕하세요".encode("utf-8"), ".smi", "ko", self.tmp)


if __name__ == "__main__":
    unittest.main()
