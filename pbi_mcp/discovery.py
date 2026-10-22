"""Locate the Power BI Desktop install (for its bundled ADOMD.NET client) and
enumerate running Power BI Desktop instances: each PBIDesktop.exe process,
its file (command line, else window title), and the loopback port its child
msmdsrv.exe (the local Analysis Services engine) is listening on.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

ADOMD_DLL_NAMES = (
    "Microsoft.PowerBI.AdomdClient.dll",
    "Microsoft.AnalysisServices.AdomdClient.dll",
)

def _classic_install_dirs() -> list[Path]:
    """Where the .exe/.msi installer puts Power BI Desktop (any drive/locale)."""
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles(x86)"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ]
    dirs: list[Path] = []
    for r in roots:
        if r:
            d = Path(r) / "Microsoft Power BI Desktop" / "bin"
            if d not in dirs:
                dirs.append(d)
    return dirs


def find_adomd_dll() -> Path:
    """Return the path to a working ADOMD.NET client DLL bundled with
    Power BI Desktop. Checks the classic (.exe installer) location first,
    then falls back to asking Windows for the Microsoft Store package
    location, since that folder's ACLs block plain directory listing.
    """
    for install_dir in _classic_install_dirs():
        for name in ADOMD_DLL_NAMES:
            candidate = install_dir / name
            if candidate.exists():
                return candidate

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop | "
                "Select-Object -First 1 -ExpandProperty InstallLocation)",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        install_location = result.stdout.strip()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Could not query Power BI Desktop's install location via PowerShell."
        ) from exc

    if not install_location:
        raise RuntimeError(
            "Power BI Desktop does not appear to be installed (checked the "
            "classic install path and the Microsoft Store package registry). "
            "Install Power BI Desktop and try again."
        )

    bin_dir = Path(install_location) / "bin"
    for name in ADOMD_DLL_NAMES:
        candidate = bin_dir / name
        if candidate.exists():
            return candidate

    raise RuntimeError(
        f"Found Power BI Desktop at {install_location} but no ADOMD.NET "
        f"client DLL in {bin_dir}. Your Power BI Desktop version may not "
        "bundle one; consider installing the standalone ADOMD.NET client."
    )


@dataclass
class PbiInstance:
    port: int
    display_name: str
    pid: int
    file_path: str | None = None

    @property
    def data_source(self) -> str:
        return f"localhost:{self.port}"


def _file_path_from_cmdline(proc) -> str | None:
    """When Power BI Desktop was launched by opening a .pbix/.pbit directly
    (double-click, Start-Process, recent-files list), the file path is a
    command-line argument. This is a more reliable file identifier than the
    window title (no splash-window race, survives long/duplicate names) --
    but it goes stale if the user later uses File > Open to switch files in
    an already-running instance, which is why the window-title path below
    still exists as a fallback.
    """
    try:
        args = proc.cmdline()
    except Exception:
        return None
    for arg in args:
        if arg.lower().endswith((".pbix", ".pbit")):
            return arg
    return None


def _window_title_for_pid(pid: int) -> str | None:
    """Find the visible top-level window title belonging to a process, via
    the Win32 API (pywin32). Power BI Desktop's title is the open file's
    name, e.g. "Sales Report - Power BI Desktop".
    """
    import win32gui
    import win32process

    titles: list[str] = []

    def _callback(hwnd: int, _extra: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
        if found_pid == pid:
            titles.append(title)
        return True

    win32gui.EnumWindows(_callback, None)
    if not titles:
        return None
    # Power BI Desktop briefly (sometimes persistently) owns a splash/loading
    # window titled "Untitled - Power BI Desktop" alongside the real report
    # window, which is titled with just the file name (no suffix) in current
    # builds. Prefer a suffix-less title when one exists; it's the real one.
    unsuffixed = [t for t in titles if not t.endswith(" - Power BI Desktop")]
    candidates = unsuffixed or titles
    return max(candidates, key=len)


def _clean_title(title: str) -> str:
    for suffix in (" - Power BI Desktop",):
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def find_running_instances() -> list[PbiInstance]:
    """Find every running Power BI Desktop process, its window title (the
    open file's name), and the local Analysis Services port its child
    msmdsrv.exe process is listening on.
    """
    import psutil

    instances: list[PbiInstance] = []
    for proc in psutil.process_iter(["pid", "name"]):
        if (proc.info.get("name") or "").lower() != "pbidesktop.exe":
            continue
        pid = proc.info["pid"]
        try:
            parent = psutil.Process(pid)
            msmdsrv = next((c for c in parent.children(recursive=True) if c.name().lower() == "msmdsrv.exe"), None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if msmdsrv is None:
            continue  # Model likely still loading; engine hasn't started yet.

        port = None
        try:
            for conn in msmdsrv.net_connections(kind="inet"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.ip in ("127.0.0.1", "::1"):
                    port = conn.laddr.port
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if port is None:
            continue

        file_path = _file_path_from_cmdline(parent)
        if file_path:
            display_name = Path(file_path).stem
        else:
            title = _window_title_for_pid(pid)
            display_name = _clean_title(title) if title else f"pid-{pid}"

        instances.append(PbiInstance(port=port, display_name=display_name, pid=pid, file_path=file_path))

    return instances
