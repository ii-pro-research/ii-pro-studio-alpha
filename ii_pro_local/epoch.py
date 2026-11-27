"""Read the Windows boot boundary. This module cannot stop or start a process."""

import ctypes
import hashlib
import json
import subprocess
import sys
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from ii_pro_core.errors import Conflict

BOOT_TOLERANCE = timedelta(seconds=5)


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() != timedelta(0):
        raise ValueError("Zoned UTC boot time required")
    return result


def startup_proof(sample, captured_at, uptime_ms, now):
    """Corroborate a full startup even when the native GUID does not change."""
    try:
        wmi, start, event_at, kind_at = (timestamp(sample[k]) for k in ("wmi", "start", "start_event_at", "kind_event_at"))
        estimated = now - timedelta(milliseconds=uptime_ms)
        if (type(sample["boot_type"]) is not int or sample["boot_type"] != 0
                or not all(type(sample[k]) is int and sample[k] > 0 for k in ("start_record", "kind_record"))
                or sample["start_record"] == sample["kind_record"] or start <= captured_at + BOOT_TOLERANCE
                or any(value <= captured_at or value > now or abs(value - start) > BOOT_TOLERANCE
                       for value in (wmi, event_at, kind_at, estimated))):
            raise ValueError("Inconsistent startup evidence")
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise Conflict("Full Windows startup evidence is incomplete or inconsistent") from exc
    return {"started_at": start.isoformat(), "wmi": wmi.isoformat(), "start_event_at": event_at.isoformat(),
            "kind_event_at": kind_at.isoformat(), "start_record": sample["start_record"],
            "kind_record": sample["kind_record"], "boot_type": sample["boot_type"]}


def windows_startup():
    # Fixed, read-only queries. Import only named timestamps, record IDs and boot type.
    script = r"""
$ErrorActionPreference = 'Stop'
$wmi = Get-CimInstance -ClassName Win32_OperatingSystem
$start = Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-General';Id=12} -MaxEvents 1
$kind = Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Boot';Id=27} -MaxEvents 1
function EventValue($event, $name) {
    $values = @(([xml]$event.ToXml()).Event.EventData.Data | Where-Object { $_.Name -eq $name })
    if ($values.Count -ne 1 -or -not $values[0].'#text') { throw 'Missing boot event field' }
    $values[0].'#text'
}
@{wmi=$wmi.LastBootUpTime.ToUniversalTime().ToString('o'); start=(EventValue $start 'StartTime');
  start_event_at=$start.TimeCreated.ToUniversalTime().ToString('o'); start_record=$start.RecordId;
  kind_event_at=$kind.TimeCreated.ToUniversalTime().ToString('o'); kind_record=$kind.RecordId;
  boot_type=[int](EventValue $kind 'BootType')} | ConvertTo-Json -Compress
"""
    if sys.platform != "win32":
        raise Conflict("Windows startup evidence is unavailable")
    directory = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(directory, len(directory))
    if not 0 < size < len(directory):
        raise Conflict("Windows system directory is unavailable")
    executable = Path(directory.value) / "WindowsPowerShell/v1.0/powershell.exe"
    try:
        output = subprocess.check_output([str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                         stderr=subprocess.DEVNULL, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        return json.loads(output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise Conflict("Windows startup evidence could not be read") from exc


def windows_boot():
    if sys.platform != "win32":
        raise Conflict("Windows boot evidence is unavailable")

    class BootEnvironment(ctypes.Structure):
        _fields_ = [("boot_id", ctypes.c_ubyte * 16), ("firmware", wintypes.DWORD), ("flags", ctypes.c_ulonglong)]

    try:
        query = ctypes.WinDLL("ntdll").NtQuerySystemInformation
        query.argtypes = (wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))
        query.restype = wintypes.LONG
        boot, size = BootEnvironment(), wintypes.ULONG()
        if query(90, ctypes.byref(boot), ctypes.sizeof(boot), ctypes.byref(size)) or size.value != ctypes.sizeof(boot):
            raise Conflict("Windows boot identity could not be verified")
        uptime = ctypes.WinDLL("kernel32").GetTickCount64
        uptime.restype = ctypes.c_ulonglong
        stamp = bytes(boot.boot_id)
        if not any(stamp):
            raise Conflict("Windows boot identity is empty")
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            host, kind = winreg.QueryValueEx(key, "MachineGuid")
        if kind != winreg.REG_SZ or not isinstance(host, str) or not host.strip():
            raise Conflict("Windows host identity is unavailable")
        return {"host": hashlib.sha256(host.lower().encode()).hexdigest(), "boot": stamp.hex(), "uptime_ms": uptime()}
    except (OSError, AttributeError) as exc:
        raise Conflict("Windows boot identity is unavailable") from exc
