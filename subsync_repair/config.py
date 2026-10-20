"""Settings from config.json (config.example.json shows the keys). Nothing is read at import time: each command loads
the file in main(), and a missing or malformed file is a ConfigError that names the problem."""
import json
import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from subsync_repair.external.alass import DEFAULT_ALASS
from subsync_repair.external.ffmpeg import DEFAULT_FFMPEG, DEFAULT_FFPROBE
from subsync_repair.formats.languages import ANY_LANGUAGE, language_code
from subsync_repair.storage.library import Libraries

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
APP_NAME = "subsync-repair"
DEFAULT_PORT = 8778
DEFAULT_REFERENCE_ORDER = ("en", ANY_LANGUAGE)
ANY_EXTENSION = "*"
EXTENSION_IDS = {"chrome-extension": re.compile(r"[a-p]{32}"),
                 "moz-extension": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")}
CONTAINER_HOMES = ("/", "/root")    # HOME in many containers, whose own folders go away with the container
KNOWN_KEYS = {
    "": {"target_language", "libraries", "skip_dirs", "reference_languages", "embedded_reference_languages", "tools",
         "sync"},
    "tools": {"data_dir", "alass", "ffmpeg", "ffprobe", "ffsubsync_python", "ffsubsync_libs"},
    "sync": {"extension_ids", "port"},
}
LIBRARY_KEYS = {"path"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Tools:
    data_dir: Path                  # backups, the verdict cache, the lock, the hook queue and the server's records
    ffmpeg: str = DEFAULT_FFMPEG
    ffprobe: str = DEFAULT_FFPROBE
    alass: str = DEFAULT_ALASS
    ffsubsync_python: str = ""      # an interpreter that has ffsubsync; empty leaves sibling references unchecked
    ffsubsync_libs: tuple = ()      # folders added to that interpreter's import path


@dataclass(frozen=True)
class Settings:
    target: str                     # the language a library scan repairs
    libraries: Libraries            # the only folders the tools touch
    skip_dirs: frozenset            # lower-case folder names a library scan leaves out
    tools: Tools
    sibling_order: tuple            # languages of a subtitle file next to the video that may be the sync reference
    embedded_order: tuple           # the same for a track inside the video (a subtitle is never its own reference)
    extension_ids: tuple            # browser extensions the sync server answers; "*" for any
    port: int
    warnings: tuple = ()


def load_settings(path=DEFAULT_CONFIG, platform=sys.platform, env=None):
    path = Path(path)
    return parse_settings(read_config(path), platform, os.environ if env is None else env, base=path.parent)


def read_config(path):
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"{path} not found: copy config.example.json to {path.name} and edit it") from None
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(f"{path} must be saved as UTF-8") from None
    try:
        cfg = json.loads(text)
    except ValueError as e:
        hint = " (write Windows paths with / or \\\\)" if "Invalid \\escape" in str(e) else ""
        raise ConfigError(f"{path} is not valid JSON: {e}{hint}") from None
    if not isinstance(cfg, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return cfg


def parse_settings(cfg, platform=sys.platform, env=None, base=ROOT):
    """Settings from the parsed `cfg`; relative paths in it are relative to the folder `base` of the config file."""
    try:
        return _parse(cfg, platform, os.environ if env is None else env, Path(base))
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _parse(cfg, platform, env, base):
    if cfg.get("target_language") is None:
        raise ValueError("target_language is required (for example \"en\")")
    tools, sync = section(cfg, "tools"), section(cfg, "sync")
    roots, library_warnings = libraries(cfg.get("libraries"))
    data_dir, data_warnings = data_folder(tools, platform, env, base)
    return Settings(
        target=language_code(cfg["target_language"], "target_language"),
        libraries=Libraries(roots),
        skip_dirs=frozenset(d.lower() for d in strings(cfg.get("skip_dirs"), "skip_dirs")),
        tools=Tools(
            data_dir=data_dir,
            ffmpeg=command(tools, "ffmpeg", DEFAULT_FFMPEG, base),
            ffprobe=command(tools, "ffprobe", DEFAULT_FFPROBE, base),
            alass=command(tools, "alass", DEFAULT_ALASS, base),
            ffsubsync_python=command(tools, "ffsubsync_python", "", base, may_be_empty=True),
            ffsubsync_libs=tuple(relative_to(lib, base) for lib in strings(tools.get("ffsubsync_libs"),
                                                                            "tools.ffsubsync_libs"))),
        sibling_order=language_list(cfg, "reference_languages"),
        embedded_order=language_list(cfg, "embedded_reference_languages"),
        extension_ids=extension_ids(sync.get("extension_ids")),
        port=port(sync.get("port")),
        warnings=unknown_keys(cfg) + library_warnings + data_warnings)


def section(cfg, key):
    value = cfg.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def tool_setting(tools, key):
    """The text of tools.`key`, or None when it is left out."""
    value = tools.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"tools.{key} must be a string")
    return value


def command(tools, key, default, base, may_be_empty=False):
    """A program: a name looked up on PATH, or a path (relative ones start at the config file's folder)."""
    value = tool_setting(tools, key)
    if value is None:
        return default
    if not value:
        if may_be_empty:
            return ""
        raise ValueError(f"tools.{key} is empty: leave it out to use {default!r}")
    has_folder = value.startswith("~") or "/" in value or os.sep in value
    return relative_to(value, base) if has_folder else value


def relative_to(value, base):
    path = Path(os.path.expanduser(value))
    return str(path if path.is_absolute() else base / path)


def data_folder(tools, platform, env, base):
    """(the data folder, warnings about it)."""
    value = tool_setting(tools, "data_dir")
    if value is not None:
        if not value:
            raise ValueError("tools.data_dir is empty: leave it out for the default folder")
        return Path(relative_to(value, base)), ()
    folder = default_data_dir(platform, env)
    if platform != "win32" and env.get("HOME") in CONTAINER_HOMES:
        return folder, (f"the data folder defaults to {folder}: in a container, set tools.data_dir to a folder on a "
                        f"mounted volume",)
    return folder, ()


def default_data_dir(platform, env):
    """Where backups and the verdict cache live unless tools.data_dir says otherwise."""
    if platform == "win32":
        if not env.get("PROGRAMDATA"):
            raise ValueError("PROGRAMDATA is not set: set tools.data_dir")
        return Path(env["PROGRAMDATA"]) / APP_NAME
    home = env.get("HOME")
    if not home:
        raise ValueError("HOME is not set: set tools.data_dir")
    if platform == "darwin":
        return Path(home) / "Library" / "Application Support" / APP_NAME
    xdg = env.get("XDG_DATA_HOME", "")
    return (Path(xdg) if posixpath.isabs(xdg) else Path(home) / ".local" / "share") / APP_NAME


def strings(value, name):
    """A list of non-empty strings; None counts as an empty list."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)


def language_list(cfg, key):
    """Language codes in order; "*" stands for any other language present."""
    value = cfg.get(key)
    if value is None:
        return DEFAULT_REFERENCE_ORDER
    return tuple(code if code == ANY_LANGUAGE else language_code(code, key) for code in strings(value, key))


def port(value):
    if value is None:
        return DEFAULT_PORT
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise ValueError("sync.port must be a port number (1 to 65535)")
    return value


def libraries(items):
    """(library folders, warnings): each folder given as an absolute path or as {"path": ...}."""
    if not isinstance(items, list) or not items:
        raise ValueError("libraries is required: a list of absolute folder paths")
    roots, warnings = [], []
    for i, item in enumerate(items):
        path = item.get("path") if isinstance(item, dict) else item
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError(f"libraries[{i}] must be an absolute folder path")
        if isinstance(item, dict):
            warnings += [f"setting 'libraries[{i}].{key}' is not used" for key in item if key not in LIBRARY_KEYS]
        if not os.path.isdir(path):
            warnings.append(f"library folder {path} does not exist")
        roots.append(Path(path))
    return roots, tuple(warnings)


def is_extension_id(value):
    return any(pattern.fullmatch(value) for pattern in EXTENSION_IDS.values())


def extension_ids(value):
    ids = strings(value, "sync.extension_ids")
    bad = [i for i in ids if i != ANY_EXTENSION and not is_extension_id(i)]
    if bad:
        raise ValueError(f"sync.extension_ids: {bad[0]!r} is neither a Chrome extension id, a Firefox extension UUID "
                         f"nor \"{ANY_EXTENSION}\"")
    return ids


def unknown_keys(cfg):
    warnings = [f"setting {key!r} is not used" for key in cfg if key not in KNOWN_KEYS[""]]
    for name in ("tools", "sync"):
        warnings += [f"setting '{name}.{key}' is not used" for key in section(cfg, name) if key not in KNOWN_KEYS[name]]
    return tuple(warnings)
