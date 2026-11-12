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


# ---------------------------------------------------------------- Linux
UDEV_RULE = '/etc/udev/rules.d/70-psu-studio.rules'
# 70- so it runs before 73-seat-late.rules, which turns TAG uaccess into an ACL for the logged-in user
UDEV_TEXT = ('# PSU Studio: let the logged-in user talk to CH340 USB-serial (Multicomp Pro / OWON supplies)\n'
             'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", MODE="0660", TAG+="uaccess"\n')


def _linux_ch34x_on_usb():
    """Is a WCH CH34x chip plugged in at all (even if no ttyUSB appeared)?"""
    for f in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        try:
            with open(f) as fh:
                if fh.read().strip().lower() == "1a86":
                    return True
        except OSError:
            pass
    return False


def _linux_status(ports):
    ch = [p for p in ports if p["ch340"]]
    if ch:
        dev = ch[0]["device"]
        if not os.access(dev, os.R_OK | os.W_OK):
            return dict(state="perm", port=dev,
                        msg=f"Found the supply on {dev} but you don't have permission to use it. "
                            "Click 'Fix USB access' (asks for your password once).")
        return dict(state="ok", msg=f"Supply found on {dev}", port=dev)
    if _linux_ch34x_on_usb():
        brl = any(os.path.exists(p) for p in ("/usr/bin/brltty", "/bin/brltty", "/usr/sbin/brltty"))
        if brl:
            return dict(state="brltty",
                        msg="The supply is plugged in but BRLTTY (a braille-display service) grabbed its USB chip. "
                            "If you don't use a braille display: sudo apt remove brltty, then unplug and replug the supply.")
        return dict(state="nodev", msg="USB chip seen but no serial port appeared. Try unplugging and replugging.")
    return dict(state="linux", msg="Linux has the CH340 driver built in. Plug the supply in by USB and switch it on.")


def _linux_fix_access():
    import shutil
    pk = shutil.which("pkexec")
    manual = (f"Run in a terminal:\n  echo '{UDEV_TEXT.splitlines()[1]}' | sudo tee {UDEV_RULE}\n"
              "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
              "or: sudo usermod -aG dialout $USER and log out/in.")
    if not pk:
        return dict(ok=False, msg="pkexec isn't available. " + manual)
    script = (f"printf '%s' '{UDEV_TEXT}' > {UDEV_RULE} && udevadm control --reload-rules && "
              "udevadm trigger --subsystem-match=tty --action=add")
    try:
        r = subprocess.run([pk, "sh", "-c", script], capture_output=True, text=True, timeout=180)
    except Exception as e:
        return dict(ok=False, msg=f"Couldn't run pkexec: {e}. " + manual)
    if r.returncode != 0:
        return dict(ok=False, msg="Permission fix was cancelled or failed. " + manual)
    return dict(ok=True, msg="USB access fixed. If it still says no permission, unplug and replug the supply.")


def status():
    ports = list_ports()
    if sys.platform.startswith("linux"):
        return _linux_status(ports)
    ch = [p for p in ports if p["ch340"]]
    if ch:
        return dict(state="ok", msg=f"Supply found on {ch[0]['device']}",
                    port=ch[0]["device"], installer=bool(find_installer()))
    if os.name != "nt":
        return dict(state="nonwin",
                    msg="macOS ships a CH340 driver already. Plug the supply in.",
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
    if sys.platform.startswith("linux"):
        return _linux_fix_access()
    exe = find_installer()
    if os.name != "nt":
        return dict(ok=False, msg="No driver needed on this OS.")
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
