"""
CH340 / CH34x USB-serial driver check + one-click install (Windows).

Order of preference for the installer:
  1. Any .exe dropped in  <app>/drivers/  (e.g. CH341SER.EXE or
     CH34x_Install_Windows_v3_4.EXE from the Multicomp Pro zip). It gets
     bundled into the .exe when you run build.bat.
  2. If none is bundled, open WCH's official download page.
"""
import glob
import os
import subprocess
import sys
import webbrowser

from device import CH34X_VID, list_ports

WCH_PAGE = "https://www.wch-ic.com/downloads/CH341SER_EXE.html"


def app_dir():
    # PyInstaller one-file unpacks to _MEIPASS
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return base
    return os.path.dirname(os.path.abspath(__file__))


def find_installer():
    roots = [os.path.join(app_dir(), "drivers"),
             os.path.join(os.path.dirname(app_dir()), "drivers"),
             os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "drivers")]
    for root in roots:
        for pat in ("CH341SER*.exe", "CH34x*.exe", "*.exe", "*.EXE"):
            hits = sorted(glob.glob(os.path.join(root, pat)))
            if hits:
                return hits[0]
    return None


def _pnputil_has_ch34x():
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["pnputil", "/enum-drivers"], capture_output=True,
                             text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return None
    low = out.lower()
    return ("ch341ser" in low) or ("ch343ser" in low) or ("wch.cn" in low) or ("wch" in low and "ports" in low)


def status():
    ports = list_ports()
    ch = [p for p in ports if p["ch340"]]
    if ch:
        return dict(state="ok", msg=f"Supply found on {ch[0]['device']}",
                    port=ch[0]["device"], installer=bool(find_installer()))
    if os.name != "nt":
        return dict(state="nonwin",
                    msg="Not on Windows: Linux/macOS ship a CH340 driver already. Plug the supply in.",
                    installer=bool(find_installer()))
    has = _pnputil_has_ch34x()
    # A device with WCH VID but no driver shows as "unknown" and won't be a COM port.
    if has:
        return dict(state="installed",
                    msg="Driver installed. Plug the supply in by USB and switch it on.",
                    installer=bool(find_installer()))
    return dict(state="missing",
                msg="CH340 driver not found. Click Install Driver.",
                installer=bool(find_installer()))


def install():
    """Launch the bundled installer elevated. Returns a status dict."""
    exe = find_installer()
    if os.name != "nt":
        return dict(ok=False, msg="Driver install is only needed on Windows.")
    if not exe:
        webbrowser.open(WCH_PAGE)
        return dict(ok=False, opened_page=True,
                    msg="No installer bundled. Opened WCH's official page: download CH341SER.EXE, "
                        "run it and click INSTALL (or drop it into the drivers folder and rebuild).")
    import ctypes
    # SW_SHOWNORMAL=1. 'runas' triggers the UAC prompt.
    # Show the vendor dialog (no silent flag) so the user can see INSTALL succeed.
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None,
                                             os.path.dirname(exe), 1)
    if rc <= 32:
        return dict(ok=False, msg=f"Could not start installer (code {rc}). Was the UAC prompt declined?")
    # Ask Windows to rescan devices so a plugged-in supply picks the driver up.
    try:
        subprocess.Popen(["pnputil", "/scan-devices"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
    return dict(ok=True, msg="Installer launched. Click INSTALL in the WCH window, then unplug and replug the supply.")
