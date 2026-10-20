"""The data folder, which holds the backups, the verdict cache, the lock, the hook queue and the sync server's records.
On Windows its default under %PROGRAMDATA% is shared by the Bazarr service and the people who run the tool."""
import logging
import os
import subprocess
from pathlib import Path

from subsync_repair.external import process
from subsync_repair.storage.fileio import sweep_stale

USERS = "*S-1-5-32-545"                  # BUILTIN\Users, by SID so that a localised Windows finds it too
ICACLS_TIMEOUT = 60
TRUSTED_OWNERS = frozenset({
    "S-1-5-18",                          # LocalSystem, which Bazarr's service runs as
    "S-1-5-32-544",                      # BUILTIN\Administrators
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",    # TrustedInstaller
})

log = logging.getLogger(__name__)


class DataDirError(Exception):
    pass


def prepare(path):
    """The data folder `path`, made when it is missing. On Windows a new folder lets every local user modify what it
    holds, and an existing one is refused when another account that is no administrator owns it."""
    path = Path(path)
    if path.is_dir():
        if os.name == "nt":
            refuse_foreign_owner(path)
        sweep_stale(path)
        return path
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        share_with_users(path)
    return path


def share_with_users(path):
    try:
        r = process.run(["icacls", path, "/grant", f"{USERS}:(OI)(CI)M"], ICACLS_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("could not let other users modify %s: icacls did not run (%s)", path, e)
        return False
    if r.returncode != 0:
        log.warning("could not let other users modify %s: %s", path, process.tail(r))
        return False
    return True


def refuse_foreign_owner(path):
    try:
        owner = owner_sid(path)
        trusted = TRUSTED_OWNERS | {current_sid()} | administrators()
    except OSError as e:
        log.warning("could not check who owns %s: %s", path, e)
        return
    if owner not in trusted:
        raise DataDirError(f"{path} belongs to another account ({owner}), which could change what this tool reads and "
                           f"writes there: remove the folder or set tools.data_dir")


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    netapi32 = ctypes.WinDLL("netapi32")
    SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION = 1, 1
    TOKEN_QUERY, TOKEN_USER = 0x0008, 1
    MAX_PREFERRED_LENGTH = 0xFFFFFFFF
    PSID = ctypes.c_void_p

    advapi32.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(PSID),
                                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(PSID)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.LookupAccountSidW.argtypes = [wintypes.LPCWSTR, PSID, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(ctypes.c_int)]
    advapi32.LookupAccountSidW.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    netapi32.NetLocalGroupGetMembers.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                                 ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                                 ctypes.POINTER(ctypes.c_size_t)]
    netapi32.NetLocalGroupGetMembers.restype = wintypes.DWORD
    netapi32.NetApiBufferFree.argtypes = [ctypes.c_void_p]

    def sid_text(sid):
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)

    def owner_sid(path):
        owner, descriptor = PSID(), ctypes.c_void_p()
        status = advapi32.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
                                                ctypes.byref(owner), None, None, None, ctypes.byref(descriptor))
        if status:
            raise ctypes.WinError(status)
        try:
            return sid_text(owner)
        finally:
            kernel32.LocalFree(descriptor)

    def current_sid():
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return sid_text(ctypes.cast(buffer, ctypes.POINTER(PSID))[0])     # TOKEN_USER starts with the SID
        finally:
            kernel32.CloseHandle(token)

    def administrators():
        """The SIDs of the direct members of the local Administrators group."""
        group = PSID()
        if not advapi32.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(group)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name = account_name(group)
        finally:
            kernel32.LocalFree(group)
        members, read, total = ctypes.c_void_p(), wintypes.DWORD(), wintypes.DWORD()
        status = netapi32.NetLocalGroupGetMembers(None, name, 0, ctypes.byref(members), MAX_PREFERRED_LENGTH,
                                                  ctypes.byref(read), ctypes.byref(total), None)
        if status:
            raise ctypes.WinError(status)
        try:
            sids = ctypes.cast(members, ctypes.POINTER(PSID))     # LOCALGROUP_MEMBERS_INFO_0 holds one SID
            return {sid_text(sids[i]) for i in range(read.value)}
        finally:
            netapi32.NetApiBufferFree(members)

    def account_name(sid):
        name, domain = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
        name_size, domain_size, use = wintypes.DWORD(256), wintypes.DWORD(256), ctypes.c_int()
        if not advapi32.LookupAccountSidW(None, sid, name, ctypes.byref(name_size), domain, ctypes.byref(domain_size),
                                          ctypes.byref(use)):
            raise ctypes.WinError(ctypes.get_last_error())
        return name.value
