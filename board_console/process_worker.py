"""Private process supervisor; stdout/stderr belong exclusively to the capture.

On Windows the supervisor joins a kill-on-close Job BEFORE launching the tool.
Terminating the supervisor therefore also releases serial ports held by descendants.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time


def windows_job():
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    handle = api.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.AssignProcessToJobObject(handle, api.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    # Non-inheritable handle stays open until the supervisor exits.
    return handle


def watch_parent(parent_pid: int):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        parent = api.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        if not parent:
            raise ctypes.WinError(ctypes.get_last_error())

        def wait():
            api.WaitForSingleObject(parent, 0xFFFFFFFF)
            os._exit(125)  # Close Job handle even if the MCP host crashed.
    else:
        def wait():
            while os.getppid() == parent_pid:
                time.sleep(0.2)
            os.killpg(os.getpgrp(), signal.SIGKILL)
    threading.Thread(target=wait, daemon=True).start()


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        job = windows_job() if os.name == "nt" else None
        watch_parent(int(argv[0]))
        # Frozen Windows applications alter the DLL search directory. External
        # SDK programs must use their own DLLs, not the MCP bundle's libraries.
        if getattr(sys, "frozen", False) and os.name == "nt":
            import ctypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            api.SetDllDirectoryW.restype = ctypes.c_int
            if not api.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
        environment = os.environ.copy()
        if getattr(sys, "frozen", False) and os.name != "nt":
            for key in ("LD_LIBRARY_PATH", "LIBPATH"):
                original = environment.pop(key + "_ORIG", None)
                if original is None:
                    environment.pop(key, None)
                else:
                    environment[key] = original
        child = subprocess.Popen(argv[1:], stdin=sys.stdin.buffer, env=environment,
                                 stdout=sys.stdout.buffer, stderr=sys.stderr.buffer)
        code = child.wait()
    except Exception as exc:
        print(f"process supervisor: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        code = 127
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
