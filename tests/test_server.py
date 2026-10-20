"""Tests for the sync server (subsync_repair/server.py and `subsync-repair.py serve`): which paths and origins it
accepts, the job queue, and the HTTP API wired to the real repairer (alass is a fake)."""
import argparse
import http.client
import json
import os
import signal
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from subsync_repair import cli, server
from subsync_repair.engine.repairer import Repairer
from subsync_repair.engine.writers import LEFT_LIBRARY
from subsync_repair.external import programs
from subsync_repair.server import CopyLedger, Jobs, Origins, RequestError, SyncService, create_server
from support import (CHROME_ID, FIREFOX_ID, JAPANESE, KOREAN, STARTS, LibraryCase, NoForeignLogging, TempFolder,
                     make_link, make_settings, srt_of)


def wait_until(condition, timeout=10.0):
    """The first true value of `condition()`, or AssertionError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


class Paths(LibraryCase):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "Other"
        self.outside.mkdir()
        self.service = SyncService(self.settings.libraries, self.repairer, None, {})

    def test_a_subtitle_in_a_library_is_accepted_by_its_real_path(self):
        self.assertEqual(self.service.resolve(str(self.sub)), Path(os.path.realpath(self.sub)))

    def test_every_language_and_format_is_accepted(self):
        for name in ("Show S01E01.ko.hi.srt", "Show S01E01.ko.ass", "Show S01E01.ko.smi", "Show S01E01.kor.srt",
                     "Show S01E01.ja.srt", "Show S01E01.de.srt"):
            path = self.write(name, "x")
            self.assertEqual(self.service.resolve(str(path)), Path(os.path.realpath(path)), name)

    def test_paths_that_must_be_refused(self):
        synced = self.write("Show S01E01.ko.synced.srt", "x")
        forced = self.write("Show S01E01.ko.forced.srt", "x")
        elsewhere = self.write("x.ko.srt", "x", folder=self.outside)
        sneaky = str(self.root / ".." / "Other" / "x.ko.srt")
        folder_named_like_a_subtitle = self.root / "tricky.ko.srt"
        folder_named_like_a_subtitle.mkdir()
        for raw, why in ((None, "path is required"), ("", "path is required"), (5, "path must be a string"),
                         ("Show/x.ko.srt", "path must be absolute"),
                         (str(self.sub) + "\0", "path contains a NUL character"),
                         (str(self.dir / "gone.ko.srt"), server.NOT_ACCEPTED), (str(self.video), server.NOT_ACCEPTED),
                         (str(synced), server.NOT_ACCEPTED), (str(forced), server.NOT_ACCEPTED),
                         (str(elsewhere), server.NOT_ACCEPTED), (sneaky, server.NOT_ACCEPTED),
                         (str(self.root), server.NOT_ACCEPTED),
                         (str(folder_named_like_a_subtitle), server.NOT_ACCEPTED)):
            with self.assertRaises(RequestError, msg=repr(raw)) as raised:
                self.service.resolve(raw)
            self.assertEqual(str(raised.exception), why, repr(raw))

    def test_the_answer_never_tells_whether_a_path_outside_the_libraries_exists(self):
        existing = self.write("x.ko.srt", "x", folder=self.outside)
        messages = set()
        for path in (existing, self.outside / "nothing.ko.srt"):
            with self.assertRaises(RequestError) as raised:
                self.service.resolve(str(path))
            messages.add(str(raised.exception))
        self.assertEqual(messages, {server.NOT_ACCEPTED})

    def test_a_folder_that_only_shares_the_librarys_name_prefix_is_outside(self):
        sibling = self.write("x.ko.srt", "x", folder=self.tmp / "library-extra")
        with self.assertRaisesRegex(RequestError, server.NOT_ACCEPTED):
            self.service.resolve(str(sibling))

    def test_a_library_written_with_forward_slashes_still_matches(self):
        settings = make_settings(Path(str(self.root).replace("\\", "/")), self.data, "ko")
        service = SyncService(settings.libraries, self.repairer, None, {})
        self.assertEqual(service.resolve(str(self.sub)), Path(os.path.realpath(self.sub)))

    @unittest.skipUnless(os.name == "nt" or os.uname().sysname == "Darwin", "file names ignore case here")
    def test_a_library_written_in_other_letter_case_still_matches(self):
        settings = make_settings(str(self.root).upper(), self.data, "ko")
        service = SyncService(settings.libraries, self.repairer, None, {})
        self.assertEqual(service.resolve(str(self.sub)), Path(os.path.realpath(self.sub)))

    def test_a_library_folder_that_is_itself_a_link_holds_what_it_points_to(self):
        link = self.tmp / "library-link"
        make_link(link, self.root)
        settings = make_settings(link, self.data, "ko")
        service = SyncService(settings.libraries, self.repairer, None, {})
        self.assertEqual(service.resolve(str(link / "Show" / self.sub.name)), Path(os.path.realpath(self.sub)))

    def test_network_and_device_paths_outside_the_libraries_never_reach_the_file_system(self):
        raws = ["\\\\evil-host\\share\\x.ko.srt", "//evil-host/share/x.ko.srt", "\\\\?\\C:\\x.ko.srt",
                "\\\\.\\C:\\x.ko.srt"] if os.name == "nt" else ["//evil-host/share/x.ko.srt"]
        touched = mock.Mock(side_effect=AssertionError("the file system was asked"))
        with mock.patch("os.path.realpath", touched), mock.patch.object(Path, "is_file", touched), \
                mock.patch.object(Path, "exists", touched):
            for raw in raws:
                with self.assertRaises(RequestError, msg=raw):
                    self.service.resolve(raw)
        touched.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "drive-relative paths exist on Windows only")
    def test_a_drive_relative_path_is_not_absolute(self):
        with self.assertRaisesRegex(RequestError, "absolute"):
            self.service.resolve(self.sub.drive + "x.ko.srt")

    def test_a_link_inside_the_library_that_points_outside_is_refused(self):
        target = self.outside / "Hidden"
        target.mkdir()
        self.write("Show S01E01.ko.srt", "x", folder=target)
        link = self.root / "Linked"
        make_link(link, target)
        with self.assertRaisesRegex(RequestError, server.NOT_ACCEPTED):
            self.service.resolve(str(link / "Show S01E01.ko.srt"))


class OriginRules(unittest.TestCase):
    def test_no_origin_is_a_local_program_and_is_allowed(self):
        self.assertTrue(Origins(()).allows(None))

    def test_extension_origins_are_refused_unless_listed_under_their_own_browsers_scheme(self):
        self.assertFalse(Origins(()).allows(f"chrome-extension://{CHROME_ID}"))
        listed = Origins((CHROME_ID, FIREFOX_ID))
        self.assertTrue(listed.allows(f"chrome-extension://{CHROME_ID}"))
        self.assertTrue(listed.allows(f"moz-extension://{FIREFOX_ID}"))
        self.assertFalse(listed.allows(f"chrome-extension://{'b' * 32}"))
        self.assertFalse(listed.allows(f"moz-extension://{CHROME_ID}"))

    def test_the_wildcard_allows_any_well_formed_extension_origin_and_nothing_else(self):
        anyone = Origins(("*",))
        self.assertTrue(anyone.allows(f"chrome-extension://{'p' * 32}"))
        self.assertTrue(anyone.allows(f"moz-extension://{FIREFOX_ID}"))
        for bad in (f"chrome-extension://{CHROME_ID}\n", f"chrome-extension://{CHROME_ID}/x", "chrome-extension://abc",
                    f"chrome-extension://{CHROME_ID.upper()}", "https://evil.example", "null",
                    f"http://chrome-extension://{CHROME_ID}", f"moz-extension://{CHROME_ID}"):
            self.assertFalse(anyone.allows(bad), repr(bad))


class JobQueue(unittest.TestCase):
    def setUp(self):
        self.gate = threading.Event()
        self.ran = []

    def run_job(self, action, sub):
        self.gate.wait(10)
        self.ran.append((action, sub))
        return {"status": "ok"}

    def jobs(self, run=None, **kwargs):
        jobs = Jobs(run or self.run_job, **kwargs)
        jobs.start()
        self.addCleanup(jobs.stop)
        self.addCleanup(self.gate.set)
        return jobs

    def done(self, jobs, job_id):
        return wait_until(lambda: (job := jobs.get(job_id)) and job["state"] == "done" and job)

    def test_the_same_job_is_queued_once_while_it_waits_or_runs(self):
        jobs = self.jobs()
        first = jobs.submit("measure", "a.ko.srt")
        self.assertEqual(jobs.submit("measure", "a.ko.srt"), first)
        self.assertNotEqual(jobs.submit("fix", "a.ko.srt"), first)
        self.gate.set()
        self.done(jobs, first)
        self.assertNotEqual(jobs.submit("measure", "a.ko.srt"), first)

    def test_more_pending_jobs_than_allowed_are_refused_and_accepted_ones_all_run(self):
        jobs = self.jobs(max_pending=3)
        accepted = [jobs.submit("measure", f"{i}.ko.srt") for i in range(3)]
        with self.assertRaises(server.QueueFull):
            jobs.submit("measure", "x.ko.srt")
        self.gate.set()
        for job_id in accepted:
            self.assertEqual(self.done(jobs, job_id)["result"], {"status": "ok"})
        jobs.submit("measure", "x.ko.srt")

    def test_finished_results_are_pruned_to_the_newest(self):
        self.gate.set()
        jobs = self.jobs(keep_done=2)
        ids = [jobs.submit("measure", f"{i}.ko.srt") for i in range(4)]
        self.done(jobs, ids[-1])
        self.assertEqual([jobs.get(i) is not None for i in ids], [False, False, True, True])

    def test_at_least_one_finished_result_is_kept(self):
        with self.assertRaises(ValueError):
            Jobs(self.run_job, keep_done=0)

    def test_a_failing_job_is_an_error_result_and_the_worker_keeps_going(self):
        def run(action, sub):
            if sub == "bad.ko.srt":
                raise RuntimeError("boom at C:\\private")
            if sub == "worse.ko.srt":
                raise GeneratorExit()
            return {"status": "ok"}

        jobs = self.jobs(run)
        with self.assertLogs("subsync_repair.server", "ERROR"):
            for bad in ("bad.ko.srt", "worse.ko.srt"):
                result = self.done(jobs, jobs.submit("measure", bad))["result"]
                self.assertEqual(result["status"], "error")
                self.assertNotIn("private", json.dumps(result))
        self.assertEqual(self.done(jobs, jobs.submit("measure", "good.ko.srt"))["result"], {"status": "ok"})
        self.assertTrue(jobs.alive)

    def test_stop_cancels_waiting_jobs_ends_the_worker_and_refuses_new_ones(self):
        jobs = Jobs(self.run_job)
        jobs.start()
        worker = jobs.worker
        running = jobs.submit("measure", "running.ko.srt")
        wait_until(lambda: jobs.get(running)["state"] == "running")
        waiting = jobs.submit("measure", "waiting.ko.srt")
        threading.Timer(0.2, self.gate.set).start()
        jobs.stop()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.ran, [("measure", "running.ko.srt")])
        self.assertEqual((jobs.get(waiting)["state"], jobs.get(waiting)["result"]["note"]), ("done", server.STOPPING))
        with self.assertRaises(server.Closed):
            jobs.submit("measure", "late.ko.srt")


class Ledger(TempFolder):
    def test_a_broken_record_is_reported_and_ignored(self):
        path = self.tmp / "copies.json"
        for content in ("not json", "[1]"):
            path.write_text(content, encoding="utf-8")
            with self.assertLogs("subsync_repair", "WARNING"):
                self.assertEqual(CopyLedger(path).copies, {})

    def test_copies_that_are_gone_are_forgotten(self):
        ledger = CopyLedger(self.tmp / "copies.json")
        first, second = self.tmp / "a.synced.srt", self.tmp / "b.synced.srt"
        for path in (first, second):
            path.write_text("x")
            ledger.record(path)
        first.unlink()
        ledger.record(second)
        self.assertEqual(set(CopyLedger(self.tmp / "copies.json").copies), {str(second)})


class Api(LibraryCase, NoForeignLogging):
    def setUp(self):
        super().setUp()
        self.settings = make_settings(self.root, self.data, "ko", sync={"extension_ids": [CHROME_ID]})
        self.server = self.start(self.settings)

    def start(self, settings, repairer=None, **kwargs):
        httpd = create_server(settings, repairer or self.repairer_for(settings),
                              port=0, **kwargs)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        return httpd

    def request(self, method, path, body=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = raw_body if raw_body is not None else (json.dumps(body) if body is not None else None)
        conn.request(method, path, body=payload, headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data) if data else None), dict(r.getheaders())

    def submit(self, action="measure", path=None, headers=None):
        status, body, _ = self.request("POST", "/jobs", {"action": action, "path": str(path or self.sub)}, headers)
        return status, body

    def done(self, job_id):
        return wait_until(lambda: (job := self.request("GET", f"/jobs/{job_id}")[1]) and job["state"] == "done" and job)

    def test_health_reports_the_configured_tools_and_the_worker(self):
        with mock.patch.object(programs, "available", side_effect=lambda command: command == "ffmpeg"):
            status, body, _ = self.request("GET", "/health")
        self.assertEqual((status, body),
                         (200, {"ok": False, "alass": False, "ffmpeg": True, "ffprobe": False, "worker_alive": True}))
        with mock.patch.object(programs, "available", return_value=True):
            self.assertEqual(self.request("GET", "/health")[1],
                             {"ok": True, "alass": True, "ffmpeg": True, "ffprobe": True, "worker_alive": True})

    def test_answers_are_not_cached_or_sniffed_and_name_this_server(self):
        _, _, headers = self.request("GET", "/health")
        self.assertEqual((headers["Cache-Control"], headers["X-Content-Type-Options"]), ("no-store", "nosniff"))
        self.assertTrue(headers["Server"].startswith("subsync-repair/"))
        self.assertNotIn("Python", headers["Server"])

    def test_measure_writes_nothing_and_fix_writes_a_new_file_the_server_names(self):
        original = self.sub.read_bytes()
        status, body = self.submit("measure")
        self.assertEqual(status, 202)
        measured = self.done(body["id"])["result"]
        self.assertEqual((measured["status"], measured["shifts"], measured["output"]),
                         ("would-fix", [[0.0, 5.0]], None))
        elsewhere = self.tmp / "elsewhere.srt"
        status, body, _ = self.request("POST", "/jobs", {"action": "fix", "path": str(self.sub), "out": str(elsewhere)})
        fixed = self.done(body["id"])["result"]
        dest = Path(os.path.realpath(self.dir)) / "Show S01E01.ko.synced.srt"
        self.assertEqual((fixed["status"], fixed["output"]), ("fixed", str(dest)))
        self.assertEqual(self.starts_of(dest), [s + 5.0 for s in STARTS])
        self.assertEqual(self.sub.read_bytes(), original)
        self.assertFalse(elsewhere.exists())

    def test_a_subtitle_in_another_language_is_fixed_into_a_copy_named_in_that_language(self):
        japanese = self.write("Show S01E01.ja.srt", srt_of(STARTS, JAPANESE))
        result = self.done(self.submit("fix", japanese)[1]["id"])["result"]
        self.assertEqual(Path(result["output"]).name, "Show S01E01.ja.synced.srt")

    def test_a_later_fix_replaces_the_servers_own_copy_but_never_a_file_someone_else_changed(self):
        self.done(self.submit("fix")[1]["id"])
        dest = self.dir / "Show S01E01.ko.synced.srt"
        self.aligner.shift = 7.0
        self.assertEqual(self.done(self.submit("fix")[1]["id"])["result"]["status"], "fixed")
        self.assertEqual(self.starts_of(dest), [s + 7.0 for s in STARTS])
        dest.write_text("my own edit", encoding="utf-8")
        result = self.done(self.submit("fix")[1]["id"])["result"]
        self.assertEqual(result["status"], "skip")
        self.assertEqual(dest.read_text(encoding="utf-8"), "my own edit")

    def test_the_servers_own_copy_is_still_its_own_after_a_restart(self):
        self.done(self.submit("fix")[1]["id"])
        self.server.shutdown()
        self.server.server_close()
        self.server = self.start(self.settings)
        self.aligner.shift = 7.0
        self.assertEqual(self.done(self.submit("fix")[1]["id"])["result"]["status"], "fixed")
        self.assertEqual(self.starts_of(self.dir / "Show S01E01.ko.synced.srt"), [s + 7.0 for s in STARTS])

    def test_job_results_carry_no_internal_details_and_the_log_does(self):
        self.aligner.fail = ("alass-cli was not found", "tools.alass is 'C:\\private\\alass-cli.exe'")
        with self.assertLogs("subsync_repair.server", "WARNING") as logs:
            result = self.done(self.submit()[1]["id"])["result"]
        self.assertEqual((result["status"], result["note"]), ("error", "alass-cli was not found"))
        self.assertNotIn("private", json.dumps(result))
        self.assertIn("private", "\n".join(logs.output))

    def test_a_file_deleted_after_queueing_is_reported_as_gone(self):
        gate = threading.Event()
        real = Repairer.measure

        def slow_measure(repairer, sub, entries=None):
            gate.wait(10)
            return real(repairer, sub, entries)

        with mock.patch.object(Repairer, "measure", slow_measure):
            self.submit()
            other = self.write("Show S01E01.ko.hi.srt", srt_of(STARTS, KOREAN))
            status, second = self.submit(path=other)
            other.unlink()
            gate.set()
            result = self.done(second["id"])["result"]
        self.assertEqual((result["status"], result["note"]), ("error", server.GONE))

    def test_a_folder_swapped_for_a_link_while_the_job_runs_gets_no_copy(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        moved = self.tmp / "moved-away"
        real_align = Repairer.align

        def align_then_swap(repairer, *args):
            result = real_align(repairer, *args)
            self.dir.rename(moved)
            make_link(self.dir, outside)
            return result

        with mock.patch.object(Repairer, "align", align_then_swap), \
                self.assertLogs("subsync_repair.server", "WARNING"):
            result = self.done(self.submit("fix")[1]["id"])["result"]
        self.assertEqual((result["status"], result["note"]), ("error", LEFT_LIBRARY))
        self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "creating symlinks needs a privilege on Windows")
    def test_a_dangling_link_named_like_the_copy_is_not_followed(self):
        target = self.tmp / "outside" / "planted.srt"
        target.parent.mkdir()
        (self.dir / "Show S01E01.ko.synced.srt").symlink_to(target)
        result = self.done(self.submit("fix")[1]["id"])["result"]
        self.assertEqual(result["status"], "skip")
        self.assertFalse(target.exists())

    def test_bad_requests_are_refused_with_a_reason_and_nothing_runs(self):
        outside = self.write("x.ko.srt", "x", folder=self.tmp / "Other")
        for body in ({"action": "delete", "path": str(self.sub)}, {"action": "fix", "path": str(outside)},
                     {"action": "fix"}, {"action": "measure", "path": "relative.ko.srt"}):
            status, reply, _ = self.request("POST", "/jobs", body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", reply)
        self.assertEqual(self.aligner.calls, [])

    def test_an_unexpected_failure_while_reading_a_request_is_a_plain_400(self):
        with mock.patch.object(SyncService, "resolve", side_effect=KeyError("C:\\private")), \
                self.assertLogs("subsync_repair.server", "ERROR"):
            status, reply, _ = self.request("POST", "/jobs", {"action": "fix", "path": str(self.sub)})
        self.assertEqual((status, reply), (400, {"error": "bad request"}))

    def test_a_body_that_is_not_a_json_object_is_a_clean_400(self):
        for raw, why in (("[]", "JSON object"), ("null", "JSON object"), ("5", "JSON object"),
                         ("{not json", "not valid JSON"), ("\xff", "not valid JSON"),
                         ("[" * 30000 + "]" * 30000, "not valid JSON")):
            data = raw.encode("latin-1")
            status, reply, _ = self.request("POST", "/jobs", raw_body=data, headers={"Content-Length": str(len(data))})
            self.assertEqual((status, reply["error"].endswith(why)), (400, True), raw[:10])

    def test_a_post_must_say_it_sends_json(self):
        for content_type in ("text/plain", "application/x-www-form-urlencoded", ""):
            status, _, _ = self.request("POST", "/jobs", {"action": "measure", "path": str(self.sub)},
                                        headers={"Content-Type": content_type})
            self.assertEqual(status, 415, content_type)
        self.assertEqual(self.submit(headers={"Content-Type": "application/json; charset=utf-8"})[0], 202)

    def test_content_length_must_be_plain_digits(self):
        for header, expected in (("-1", 400), ("abc", 400), ("", 400), ("1_0", 400), ("\u0661\u0662", 400),
                                 ("99999999", 413)):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.putrequest("POST", "/jobs")
            conn.putheader("Content-Type", "application/json")
            if header:
                conn.putheader("Content-Length", header.encode("utf-8"))
            conn.endheaders()
            r = conn.getresponse()
            self.assertEqual(r.status, expected, header)
            r.read()
            conn.close()

    def test_an_oversized_body_gets_its_answer_every_time(self):
        for _ in range(20):
            status, reply, headers = self.request("POST", "/jobs",
                                                  {"action": "measure", "path": "x" * (server.MAX_BODY + 10)})
            self.assertEqual((status, reply, headers["Connection"]), (413, {"error": "body too large"}, "close"))

    def test_a_body_of_exactly_the_largest_size_is_read(self):
        padding = server.MAX_BODY - len(json.dumps({"action": "measure", "path": ""}))
        body = json.dumps({"action": "measure", "path": "x" * padding}).encode("utf-8")
        self.assertEqual(len(body), server.MAX_BODY)
        status, reply, _ = self.request("POST", "/jobs", raw_body=body)
        self.assertEqual((status, reply), (400, {"error": "path must be absolute"}))

    def test_a_body_sent_to_an_unknown_route_is_drained_so_the_answer_arrives(self):
        status, reply, headers = self.request("POST", "/nope", raw_body=b"x" * (server.MAX_BODY * 3))
        self.assertEqual((status, reply, headers["Connection"]), (404, {"error": "not found"}, "close"))

    def test_what_a_refused_client_still_sends_is_read_until_it_is_done(self):
        rest = b"x" * (server.DRAIN_LIMIT // 2)
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as client:
            client.sendall(b"POST /jobs HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                           + f"Content-Length: {len(rest)}\r\n\r\n".encode("ascii"))
            answer = b""
            while b"body too large" not in answer:
                chunk = client.recv(4096)
                self.assertTrue(chunk, "the server closed before answering")
                answer += chunk
            self.assertIn(b"413", answer.split(b"\r\n")[0])
            for start in range(0, len(rest), 16384):
                client.sendall(rest[start:start + 16384])      # a reset connection would raise here
            client.shutdown(socket.SHUT_WR)
            while client.recv(4096):
                pass

    def test_unknown_routes_and_jobs(self):
        job_id = self.submit()[1]["id"]
        self.assertEqual(self.request("GET", "/nope")[0], 404)
        self.assertEqual(self.request("GET", "/jobs/0123456789ab")[0], 404)
        self.assertEqual(self.request("GET", f"/jobs/{job_id}x")[0], 404)
        self.assertEqual(self.request("GET", f"/jobs/{job_id}/")[0], 404)
        self.assertEqual(self.request("GET", "/jobs/../health")[0], 404)
        self.assertEqual(self.request("POST", "/health", {"a": 1})[0], 404)
        self.assertEqual(self.request("GET", "/health?x=1")[0], 200)

    def test_other_methods_are_refused_with_405(self):
        for method in ("PUT", "DELETE", "PATCH", "HEAD"):
            status, _, headers = self.request(method, "/jobs", {"a": 1} if method != "HEAD" else None)
            self.assertEqual((status, headers.get("Allow")), (405, "GET, POST, OPTIONS"), method)

    def test_a_web_page_origin_is_refused_and_gets_no_cors_headers(self):
        evil = {"Origin": "https://evil.example"}
        status, _, headers = self.request("GET", "/health", headers=evil)
        self.assertEqual(status, 403)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.submit(headers=evil)[0], 403)
        self.assertEqual(self.request("OPTIONS", "/jobs", headers=evil)[0], 403)
        job_id = self.submit()[1]["id"]
        self.assertEqual(self.request("GET", f"/jobs/{job_id}", headers=evil)[0], 403)

    def test_a_cross_site_request_without_an_extension_origin_is_refused(self):
        cross = {"Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("GET", "/health", headers=cross)[0], 403)
        origin = {"Origin": f"chrome-extension://{CHROME_ID}"}
        self.assertEqual(self.request("GET", "/health", headers={**cross, **origin})[0], 200)
        self.assertEqual(self.request("GET", "/health", headers={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_a_listed_extension_is_served_with_matching_cors_headers(self):
        origin = f"chrome-extension://{CHROME_ID}"
        status, _, headers = self.request("GET", "/health", headers={"Origin": origin})
        self.assertEqual((status, headers["Access-Control-Allow-Origin"], headers["Vary"]), (200, origin, "Origin"))
        self.assertEqual(headers["Access-Control-Max-Age"], str(server.CORS_MAX_AGE))
        self.assertEqual(self.request("GET", "/health", headers={"Origin": f"moz-extension://{FIREFOX_ID}"})[0], 403)
        self.assertEqual(self.request("GET", "/health")[0], 200)

    def test_by_default_no_extension_is_served_and_the_start_says_so(self):
        with self.assertLogs("subsync_repair.server", "WARNING") as logs:
            self.start(make_settings(self.root, self.data, "ko"))
        self.assertIn("sync.extension_ids", logs.output[0])
        self.assertEqual(self.request("GET", "/health", headers={"Origin": f"chrome-extension://{CHROME_ID}"})[0], 403)
        self.assertEqual(self.request("GET", "/health")[0], 200)

    def test_a_cors_preflight_succeeds_without_a_body(self):
        status, body, headers = self.request("OPTIONS", "/jobs", headers={"Origin": f"chrome-extension://{CHROME_ID}"})
        self.assertEqual((status, body), (204, None))
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])

    def test_a_request_with_a_foreign_host_header_is_refused_against_dns_rebinding(self):
        for host in ("evil.example", f"evil.example:{self.port}", "127.0.0.1.evil.example",
                     f"127.0.0.1:{self.port + 1}", ""):
            self.assertEqual(self.request("GET", "/health", headers={"Host": host})[0], 403, host)
        for host in ("localhost", f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"LOCALHOST:{self.port}",
                     f"[::1]:{self.port}"):
            self.assertEqual(self.request("GET", "/health", headers={"Host": host})[0], 200, host)

    def test_a_full_queue_answers_429(self):
        gate = threading.Event()
        real = Repairer.measure
        with mock.patch.object(Repairer, "measure", lambda repairer, sub, entries=None: (gate.wait(10),
                                                                                         real(repairer, sub))[1]):
            self.start(self.settings, make_jobs=lambda run: Jobs(run, max_pending=2))
            self.addCleanup(gate.set)       # before the server closes, so its worker can finish
            subs = [self.write(f"Show S01E01.ko.{flag}.srt", srt_of(STARTS, KOREAN)) for flag in ("hi", "sdh", "cc")]
            self.assertEqual([self.submit(path=p)[0] for p in subs], [202, 202, 429])

    def test_closing_the_server_stops_its_worker(self):
        httpd = self.start(self.settings)
        worker = httpd.service.jobs.worker
        httpd.shutdown()
        httpd.server_close()
        self.assertFalse(worker.is_alive())

    def test_a_second_server_cannot_take_the_same_port(self):
        with self.assertRaises(OSError):
            create_server(self.settings, self.repairer, port=self.port).server_close()

    @unittest.skipUnless(os.name == "nt", "port sharing with SO_REUSEADDR is a Windows behaviour")
    def test_no_other_program_can_share_the_port_on_windows(self):
        with socket.socket() as other:
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            with self.assertRaises(OSError):
                other.bind(("127.0.0.1", self.port))


class Serve(LibraryCase, NoForeignLogging):
    """`subsync-repair.py serve`."""

    def setUp(self):
        super().setUp()
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"target_language": "ko", "libraries": [str(self.root)],
                                           "tools": {"data_dir": str(self.data), "alass": str(self.tmp / "no-alass")}}),
                               encoding="utf-8")
        self.log = self.tmp / "serve.log"
        self.started = []

    def create(self, settings, port=None):
        httpd = server.create_server(settings, self.repairer_for(settings), port=0)
        self.started.append(httpd)
        return httpd

    def serve(self, *argv):
        with mock.patch.object(cli, "create_server", self.create):
            return cli.main(["serve", *argv, "--config", str(self.config), "--log", str(self.log)])

    def test_only_real_ports_are_accepted(self):
        self.assertEqual(cli.parse_port("8778"), 8778)
        for bad in ("abc", "0", "70000", "-5", "", "1_0", "\uff18\uff10"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                cli.parse_port(bad)

    def test_a_missing_config_is_a_logged_error_not_a_traceback(self):
        self.config.unlink()
        self.assertEqual(self.serve(), 1)
        self.assertIn("not found", self.log.read_text(encoding="utf-8"))

    def test_it_serves_until_it_is_told_to_stop(self):
        def stop_when_up():
            httpd = wait_until(lambda: self.started and self.started[0])
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
            conn.request("GET", "/health")
            self.answer = conn.getresponse().status
            conn.close()
            if os.name != "nt" and signal.getsignal(signal.SIGTERM) not in (signal.SIG_DFL, signal.SIG_IGN):
                signal.raise_signal(signal.SIGTERM)
            else:
                httpd.shutdown()

        threading.Thread(target=stop_when_up, daemon=True).start()
        self.assertEqual(self.serve("--port", "8779"), 0)
        self.assertEqual(self.answer, 200)
        logged = self.log.read_text(encoding="utf-8")
        self.assertIn("sync server on http://127.0.0.1:", logged)
        self.assertIn("not found", logged, "missing tools are reported at the start")

    def test_a_port_that_is_taken_is_a_logged_error(self):
        def taken(settings, port=None):
            raise OSError(98, "Address already in use")

        with mock.patch.object(cli, "create_server", taken):
            code = cli.main(["serve", "--port", "8779", "--config", str(self.config), "--log", str(self.log)])
        self.assertEqual(code, 1)
        self.assertIn("cannot listen on 127.0.0.1:8779", self.log.read_text(encoding="utf-8"))

    def test_the_port_option_wins_over_the_settings(self):
        ports = []

        def record(settings, port=None):
            ports.append(port)
            raise OSError(98, "Address already in use")

        with mock.patch.object(cli, "create_server", record):
            cli.main(["serve", "--port", "9001", "--config", str(self.config), "--log", str(self.log)])
            cli.main(["serve", "--config", str(self.config), "--log", str(self.log)])
        self.assertEqual(ports, [9001, 8778])


if __name__ == "__main__":
    unittest.main()
