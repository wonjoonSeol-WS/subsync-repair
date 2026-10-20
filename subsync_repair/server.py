"""The sync server: a local HTTP API around the repairer for a browser extension (GET /health, POST /jobs,
GET /jobs/<id>). A "fix" job writes a new <subtitle>.synced.srt next to the subtitle and never touches the original."""
import collections
import contextlib
import json
import logging
import os
import queue
import re
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from subsync_repair import __version__
from subsync_repair.config import ANY_EXTENSION, EXTENSION_IDS
from subsync_repair.engine.repairer import Repairer, repairable
from subsync_repair.engine.verdicts import Status, Verdict
from subsync_repair.external import programs
from subsync_repair.formats import names
from subsync_repair.storage.fileio import read_json, signature, write_json
from subsync_repair.storage.library import lexical_key, listing

MAX_BODY = 64 * 1024
DRAIN_LIMIT = 1024 * 1024   # bytes of an unwanted request body read and dropped before the connection closes
MAX_PENDING = 50            # queued plus running jobs; more are refused with 429
KEEP_DONE = 100             # finished results kept for polling
REQUEST_TIMEOUT = 10        # seconds each read or write on a client's connection may take
STOP_TIMEOUT = 10           # seconds server_close waits for a running job
JOB_ID_LENGTH = 12
CORS_MAX_AGE = 600
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
JOB_PATH = re.compile(rf"/jobs/([0-9a-f]{{{JOB_ID_LENGTH}}})")
JSON_TYPE = "application/json"
LEDGER_FILE = "synced-copies.json"
NOT_ACCEPTED = "not a subtitle file in a configured library"
GONE = "the subtitle is no longer available"
STOPPING = "the server is stopping"

log = logging.getLogger(__name__)


def failure(sub, note):
    """The result of a job that failed for the reason `note`."""
    return Verdict(Status.ERROR, note, cacheable=False).as_dict(sub)


class QueueFull(Exception):
    pass


class Closed(Exception):
    pass


class BodyTooLarge(Exception):
    pass


class RequestError(Exception):
    """A request the server refuses (400) with this message."""


def extension_origin(origin):
    """The extension id of a well-formed browser-extension origin, or None."""
    scheme, separator, ident = (origin or "").partition("://")
    pattern = EXTENSION_IDS.get(scheme)
    return ident if separator and pattern and pattern.fullmatch(ident) else None


class Origins:
    """No Origin (a local program), or the origin of a listed browser extension under its own browser's scheme."""

    def __init__(self, extension_ids):
        self.ids = frozenset(extension_ids)

    def allows(self, origin):
        if not origin:
            return True
        ident = extension_origin(origin)
        return ident is not None and (ANY_EXTENSION in self.ids or ident in self.ids)


class CopyLedger:
    """The corrected copies this server wrote, with their signatures: a later fix may replace only those. Copies that
    are gone when the server starts are forgotten."""

    def __init__(self, path):
        self.path = Path(path)
        self.copies = self.load()

    def load(self):
        data = read_json(self.path, "record of copies")
        if data is not None and not isinstance(data, dict):
            log.warning("ignoring the record of copies %s: it is not a JSON object", self.path)
        if not isinstance(data, dict):
            return {}
        return {path: sig for path, sig in data.items() if isinstance(sig, str) and os.path.lexists(path)}

    def signature_of(self, dest):
        return self.copies.get(str(dest), "")

    def record(self, dest):
        self.copies[str(dest)] = signature(dest)
        write_json(self.path, self.copies)


class Jobs:
    """One worker thread runs the queued jobs in order; the newest finished results are kept for polling."""

    def __init__(self, run, max_pending=MAX_PENDING, keep_done=KEEP_DONE):
        if keep_done < 1:
            raise ValueError("keep_done must be at least 1")
        self.run, self.max_pending = run, max_pending
        self.items = {}
        self.done_order = collections.deque(maxlen=keep_done)
        self.lock = threading.Lock()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.worker = None

    @property
    def alive(self):
        return self.worker is not None and self.worker.is_alive()

    def start(self):
        self.worker = threading.Thread(target=self._work, name="sync-jobs", daemon=True)
        self.worker.start()

    def stop(self, timeout=STOP_TIMEOUT):
        """Cancel the queued jobs, then wait for the running one to end."""
        self.stopping.set()
        while True:
            try:
                job = self.pending.get_nowait()
            except queue.Empty:
                break
            if job is not None:
                self._finish(job[0], failure(job[2], STOPPING))
        if self.worker:
            self.pending.put(None)
            self.worker.join(timeout)
            self.worker = None

    def submit(self, action, sub):
        """The id of a new job, or of the same job while it is still waiting or running."""
        with self.lock:
            if self.stopping.is_set():
                raise Closed()
            for job_id, item in self.items.items():
                if item["state"] != "done" and (item["action"], item["path"]) == (action, str(sub)):
                    return job_id
            if sum(1 for item in self.items.values() if item["state"] != "done") >= self.max_pending:
                raise QueueFull()
            job_id = uuid.uuid4().hex[:JOB_ID_LENGTH]
            self.items[job_id] = {"state": "queued", "action": action, "path": str(sub)}
        self.pending.put((job_id, action, sub))
        return job_id

    def get(self, job_id):
        with self.lock:
            item = self.items.get(job_id)
            return dict(item) if item else None

    def _work(self):
        while (job := self.pending.get()) is not None:
            job_id, action, sub = job
            if self.stopping.is_set():
                self._finish(job_id, failure(sub, STOPPING))
                continue
            with self.lock:
                self.items[job_id]["state"] = "running"
            try:
                result = self.run(action, sub)
            except (SystemExit, KeyboardInterrupt):
                self._finish(job_id, failure(sub, STOPPING))
                raise
            except BaseException:       # a failing job must not stop the worker
                log.exception("job %s failed", job_id)
                result = failure(sub, "the job failed")
            self._finish(job_id, result)

    def _finish(self, job_id, result):
        with self.lock:
            if job_id not in self.items:
                return
            if len(self.done_order) == self.done_order.maxlen:
                self.items.pop(self.done_order[0], None)
            self.items[job_id].update(state="done", result=result)
            self.done_order.append(job_id)


class SyncService:
    """What the HTTP handler asks for: path checks, the jobs themselves and the health report."""

    def __init__(self, libraries, repairer, ledger, tool_commands, make_jobs=Jobs):
        self.libraries, self.repairer, self.ledger, self.tool_commands = libraries, repairer, ledger, tool_commands
        self.actions = {"measure": repairer.measure, "fix": self.fix}
        self.jobs = make_jobs(self.run)

    def resolve(self, raw):
        """The subtitle at `raw`, or RequestError: an absolute path to a subtitle this tool repairs inside a library."""
        if raw is None or raw == "":
            raise RequestError("path is required")
        if not isinstance(raw, str):
            raise RequestError("path must be a string")
        if "\0" in raw:
            raise RequestError("path contains a NUL character")
        if not Path(raw).is_absolute():
            raise RequestError("path must be absolute")
        real = self.libraries.resolve(raw)
        if real is None or not real.is_file() or not repairable(real, listing(real.parent)):
            raise RequestError(NOT_ACCEPTED)
        return real

    def run(self, action, sub):
        """A job: the action on the subtitle, which is checked again first since it may have been replaced while the
        job waited."""
        try:
            sub = self.resolve(str(sub))
        except RequestError:
            return failure(sub, GONE)
        verdict = self.actions[action](sub)
        log.info("%s %s: %s, %s", action, sub, verdict.status.value, verdict.note)
        if verdict.detail:
            log.warning("%s %s: %s", action, sub, verdict.detail)
        return verdict.as_dict(sub)

    def fix(self, sub):
        dest = sub.with_name(names.synced_name(sub))
        verdict = self.repairer.copy(sub, dest, self.ledger.signature_of(dest), allowed=self.in_place)
        if verdict.status is Status.FIXED:
            try:
                self.ledger.record(dest)
            except OSError as e:
                log.warning("cannot record %s as this server's copy: %s", dest, e)
        return verdict

    def in_place(self, dest):
        """`dest`'s folder is still the same real folder inside a library (it may be swapped for a link meanwhile)."""
        real = self.libraries.resolve(str(dest.parent))
        return real is not None and lexical_key(real) == lexical_key(dest.parent)

    def health(self):
        found = {name: programs.available(command) for name, command in self.tool_commands.items()}
        alive = self.jobs.alive
        return {"ok": all(found.values()) and alive, **found, "worker_alive": alive}

    def problems(self):
        """What keeps the server from working, for the log at startup."""
        missing = [f"{name} not found ({command!r})" for name, command in self.tool_commands.items()
                   if not programs.available(command)]
        return missing + [f"library folder {root} does not exist" for root in self.libraries if not root.is_dir()]


class Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT
    server_version = f"subsync-repair/{__version__}"
    sys_version = ""

    def log_message(self, format, *args):
        log.debug("%s %s", self.address_string(), format % args)

    @property
    def service(self):
        return self.server.service

    def send_json(self, status, body=None, cors=True, close=False):
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        origin = self.headers.get("Origin")
        if cors and origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Max-Age", str(CORS_MAX_AGE))
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 405:
            self.send_header("Allow", "GET, POST, OPTIONS")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        if status != 204:
            self.send_header("Content-Type", f"{JSON_TYPE}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if status != 204 and self.command != "HEAD":
            self.wfile.write(data)

    def refuse(self, status, body, cors=True):
        """Answer without reading the request body. The connection closes after the answer, and what the client still
        sends is read and dropped (up to DRAIN_LIMIT), so that it gets the answer rather than a reset connection."""
        self.send_json(status, body, cors=cors, close=True)
        with contextlib.suppress(OSError):
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            dropped = 0
            while dropped < DRAIN_LIMIT and (chunk := self.rfile.read1(65536)):
                dropped += len(chunk)

    def admit(self):
        """False, after answering 403, unless the Host is this machine's loopback name, the origin is allowed and a
        browser does not report a request from another site that is no listed extension."""
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        cross_site = self.headers.get("Sec-Fetch-Site") == "cross-site" and extension_origin(origin) is None
        if host in self.server.host_names and self.server.origins.allows(origin) and not cross_site:
            return True
        log.debug("refused %s %r from host %r origin %r", self.command, self.path, self.headers.get("Host"), origin)
        self.refuse(403, {"error": "forbidden"}, cors=False)
        return False

    def do_OPTIONS(self):
        if self.admit():
            self.send_json(204)

    def do_GET(self):
        if not self.admit():
            return
        path = urlsplit(self.path).path
        if path == "/health":
            return self.send_json(200, self.service.health())
        match = JOB_PATH.fullmatch(path)
        job = self.service.jobs.get(match.group(1)) if match else None
        if job:
            return self.send_json(200, job)
        self.send_json(404, {"error": "unknown job" if match else "not found"})

    def do_POST(self):
        if not self.admit():
            return
        if urlsplit(self.path).path != "/jobs":
            return self.refuse(404, {"error": "not found"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != JSON_TYPE:
            return self.refuse(415, {"error": f"Content-Type must be {JSON_TYPE}"})
        try:
            body = self.read_body()
        except BodyTooLarge:
            return self.refuse(413, {"error": "body too large"})
        except RequestError as e:
            return self.refuse(400, {"error": str(e)})
        try:
            action = body.get("action")
            if action not in self.service.actions:
                raise RequestError(f"action must be {' or '.join(self.service.actions)}")
            sub = self.service.resolve(body.get("path"))
        except RequestError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception:
            log.exception("bad request %r", self.path)
            return self.send_json(400, {"error": "bad request"})
        self.submit(action, sub)

    def submit(self, action, sub):
        try:
            job_id = self.service.jobs.submit(action, sub)
        except QueueFull:
            return self.send_json(429, {"error": "too many jobs are waiting"})
        except Closed:
            return self.send_json(503, {"error": STOPPING})
        log.info("job %s: %s %s", job_id, action, sub)
        self.send_json(202, {"id": job_id})

    def not_allowed(self):
        if self.admit():
            self.refuse(405, {"error": "method not allowed"})

    do_HEAD = do_PUT = do_DELETE = do_PATCH = not_allowed

    def read_body(self):
        raw = self.headers.get("Content-Length", "")
        if not (raw.isascii() and raw.isdigit()):
            raise RequestError("Content-Length is required")
        if int(raw) > MAX_BODY:
            raise BodyTooLarge()
        try:
            body = json.loads(self.rfile.read(int(raw)) or b"{}")
        except (ValueError, RecursionError):
            raise RequestError("body is not valid JSON") from None
        if not isinstance(body, dict):
            raise RequestError("body must be a JSON object")
        return body


class SyncServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"     # on Windows it would let a second server bind the same port

    def __init__(self, port, service, origins):
        self.service, self.origins = service, origins
        super().__init__(("127.0.0.1", port), Handler)
        port = self.server_address[1]
        hosts = [f"[{name}]" if ":" in name else name for name in LOOPBACK_NAMES]
        self.host_names = {*hosts, *(f"{host}:{port}" for host in hosts)}

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        super().server_close()
        self.service.jobs.stop()


def create_server(settings, repairer=None, port=None, make_jobs=Jobs):
    """A started sync server on 127.0.0.1 (port 0 picks a free one); server_close() also stops its worker."""
    t = settings.tools
    service = SyncService(settings.libraries, repairer or Repairer(settings), CopyLedger(t.data_dir / LEDGER_FILE),
                          {"alass": t.alass, "ffmpeg": t.ffmpeg, "ffprobe": t.ffprobe}, make_jobs)
    if not settings.extension_ids:
        log.warning("no browser extension may call this server: list its id in sync.extension_ids (or \"*\")")
    server = SyncServer(settings.port if port is None else port, service, Origins(settings.extension_ids))
    service.jobs.start()
    return server
