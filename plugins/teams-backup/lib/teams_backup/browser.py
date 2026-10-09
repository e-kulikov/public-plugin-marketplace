"""Start (or reuse) a real Chrome with a dedicated profile and a DevTools port.

Environments:
  * ``wsl``     Chrome runs on the Windows host, driven through powershell.exe;
                a port proxy bound to the WSL gateway makes its DevTools port
                reachable from Linux (skipped when WSL already reaches it, e.g. mirrored networking).
  * ``linux`` / ``macos`` / ``windows``  Chrome is started locally.

The profile is separate from the user's everyday one and persistent, so the
Teams login survives between backups.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import time

from .cdp import cdp_alive

DEFAULT_PORT = 9222          # Chrome's DevTools port (loopback on the host)
DEFAULT_PROXY_PORT = 9223    # WSL-visible port forwarded to DEFAULT_PORT
APP_DIR_NAME = "teams-backup"
FIREWALL_RULE = "teams-backup CDP"
TEAMS_URL = "https://teams.microsoft.com/"

PS_CANDIDATES = [
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
]
WIN_CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


class BrowserSetupError(RuntimeError):
    pass


def detect_env():
    s = platform.system()
    if s == "Linux":
        try:
            if "microsoft" in open("/proc/version").read().lower():
                return "wsl"
        except OSError:
            pass
        return "linux"
    return {"Darwin": "macos", "Windows": "windows"}.get(s, "linux")


# ----------------------------------------------------------------------------
# Windows interop (WSL)
# ----------------------------------------------------------------------------

def find_powershell():
    for p in PS_CANDIDATES:
        if os.path.exists(p):
            return p
    for name in ("powershell.exe", "pwsh.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise BrowserSetupError("powershell.exe not found. WSL interop is required to start Chrome on Windows.")


def ps(script, timeout=60):
    """Run a PowerShell snippet on the Windows host; returns (rc, stdout, stderr)."""
    r = subprocess.run([find_powershell(), "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.replace("\r", "").strip(), r.stderr.replace("\r", "").strip()


def wsl_path(win_path):
    r = subprocess.run(["wslpath", "-u", win_path], capture_output=True, text=True)
    return r.stdout.strip()


def win_path(posix_path):
    r = subprocess.run(["wslpath", "-w", posix_path], capture_output=True, text=True)
    return r.stdout.strip()


def windows_host_ip():
    """The Windows host as seen from a NAT-mode WSL (default gateway)."""
    r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
    m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", r.stdout)
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# Directories
# ----------------------------------------------------------------------------

def app_dirs(env=None):
    """Where the profile and the download staging folder live.

    ``*_host`` is the path as the browser sees it (a Windows path under WSL),
    ``*_here`` is the same folder as this process can open it.
    """
    env = env or detect_env()
    if env == "wsl":
        rc, out, err = ps("[Environment]::GetFolderPath('LocalApplicationData')")
        if rc != 0 or not out:
            raise BrowserSetupError("cannot read %%LOCALAPPDATA%% via PowerShell: %s" % err)
        base_host = out + "\\" + APP_DIR_NAME
    elif env == "windows":
        base_host = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP_DIR_NAME)
    elif env == "macos":
        base_host = os.path.expanduser("~/Library/Application Support/" + APP_DIR_NAME)
    else:
        base_host = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), APP_DIR_NAME)
    sep = "\\" if env in ("wsl", "windows") else "/"

    def sub(name):
        host = base_host + sep + name
        return host, (wsl_path(host) if env == "wsl" else host)

    profile_host, profile_here = sub("chrome-profile")
    downloads_host, downloads_here = sub("downloads")
    state = os.path.join(os.path.expanduser("~"), ".cache", APP_DIR_NAME)
    return {"profile_host": profile_host, "profile_here": profile_here,
            "downloads_host": downloads_host, "downloads_here": downloads_here, "state": state}


# ----------------------------------------------------------------------------
# Chrome
# ----------------------------------------------------------------------------

def find_chrome(env):
    if env == "wsl":
        rc, out, _ = ps(
            "$p=@('%s') + @(\"$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe\") | "
            "Where-Object { Test-Path $_ } | Select-Object -First 1; $p" % "','".join(WIN_CHROME_CANDIDATES))
        if out:
            return out
        raise BrowserSetupError("Google Chrome not found on Windows. Install it from https://www.google.com/chrome/")
    if env == "windows":
        for p in WIN_CHROME_CANDIDATES:
            if os.path.exists(p):
                return p
    if env == "macos":
        p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(p):
            return p
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    raise BrowserSetupError("Chrome/Chromium not found. Install Google Chrome and retry.")


def chrome_args(port, profile_host, url=None):
    return ["--remote-debugging-port=%d" % port, "--user-data-dir=%s" % profile_host,
            "--no-first-run", "--no-default-browser-check", url or TEAMS_URL]


def launch_chrome(env, port, profile_host, profile_here, url=None):
    exe = find_chrome(env)
    os.makedirs(profile_here, exist_ok=True)
    args = chrome_args(port, profile_host, url)
    if env == "wsl":
        quoted = ",".join("'%s'" % a.replace("'", "''") for a in args)
        rc, out, err = ps("Start-Process -FilePath '%s' -ArgumentList %s" % (exe.replace("'", "''"), quoted))
        if rc != 0:
            raise BrowserSetupError("could not start Chrome via PowerShell: %s" % err)
    else:
        subprocess.Popen([exe] + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=(env != "windows"))


# ----------------------------------------------------------------------------
# WSL port proxy
# ----------------------------------------------------------------------------

def proxy_configured(listen_port, connect_port):
    """True when a v4tov4 port proxy ``*:listen_port -> 127.0.0.1:connect_port`` already exists."""
    rc, out, _ = ps("netsh interface portproxy show v4tov4")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] == str(listen_port) and parts[3] == str(connect_port):
            return True
    return False


def elevation_script(host_ip, listen_port, connect_port):
    return (
        "netsh interface portproxy delete v4tov4 listenaddress=%(ip)s listenport=%(lp)d | Out-Null; "
        "netsh interface portproxy add v4tov4 listenaddress=%(ip)s listenport=%(lp)d "
        "connectaddress=127.0.0.1 connectport=%(cp)d; "
        "if (-not (Get-NetFirewallRule -DisplayName '%(rule)s' -ErrorAction SilentlyContinue)) { "
        "New-NetFirewallRule -DisplayName '%(rule)s' -Direction Inbound -Action Allow -Protocol TCP "
        "-LocalPort %(lp)d -LocalAddress %(ip)s -RemoteAddress LocalSubnet | Out-Null }"
        % {"ip": host_ip, "lp": listen_port, "cp": connect_port, "rule": FIREWALL_RULE})


def ensure_proxy(host_ip, listen_port, connect_port):
    """Create the port proxy + firewall rule (needs admin). Returns True if it ran, False if already set."""
    if proxy_configured(listen_port, connect_port):
        return False
    script = elevation_script(host_ip, listen_port, connect_port)
    rc, gs, _ = ps("(Get-Command gsudo -ErrorAction SilentlyContinue).Source")
    if gs:
        rc, out, err = ps("& '%s' powershell -NoProfile -Command \"%s\"" % (gs, script.replace('"', '`"')), timeout=180)
    else:
        rc, out, err = ps("Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command',\"%s\"" % script.replace('"', '`"'),
                          timeout=180)
    if not proxy_configured(listen_port, connect_port):
        raise BrowserSetupError(
            "Could not create the WSL->Windows port proxy (admin rights are required).\n"
            "Run this in an elevated PowerShell on Windows, then retry:\n  %s" % script)
    return True


# ----------------------------------------------------------------------------
# Public entry points
# ----------------------------------------------------------------------------

def candidate_urls(env, port, proxy_port):
    urls = ["http://127.0.0.1:%d" % port]
    if env == "wsl":
        ip = windows_host_ip()
        if ip:
            urls += ["http://%s:%d" % (ip, proxy_port)]
    return urls


def find_live_cdp(env, port, proxy_port):
    for u in candidate_urls(env, port, proxy_port):
        if cdp_alive(u):
            return u
    return None


def ensure_browser(port=DEFAULT_PORT, proxy_port=DEFAULT_PROXY_PORT, log=print, url=None):
    """Return ``{"cdp_url", "env", "started", "dirs"}``; launch Chrome if nothing is listening."""
    env = detect_env()
    dirs = app_dirs(env)
    start_url, url = url, find_live_cdp(env, port, proxy_port)
    started = False
    if not url:
        log("No DevTools browser found. Starting Chrome with a separate profile: %s" % dirs["profile_host"])
        launch_chrome(env, port, dirs["profile_host"], dirs["profile_here"], start_url)
        started = True
        for _ in range(60):  # Chrome needs a moment; on WSL the port is loopback-only on the host
            time.sleep(1)
            url = find_live_cdp(env, port, proxy_port)
            if url:
                break
            if env == "wsl":
                rc, out, _ = ps("try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:%d/json/version -TimeoutSec 2).StatusCode } catch { 0 }" % port)
                if out.strip() == "200":
                    break
        if env == "wsl" and not url:
            ip = windows_host_ip()
            if not ip:
                raise BrowserSetupError("cannot determine the Windows host address from WSL")
            log("Chrome is up on the Windows loopback; opening a WSL-visible port (%s:%d) ..." % (ip, proxy_port))
            ran = ensure_proxy(ip, proxy_port, port)
            if ran:
                log("Port proxy and firewall rule created.")
            for _ in range(15):
                url = find_live_cdp(env, port, proxy_port)
                if url:
                    break
                time.sleep(1)
        if not url:
            raise BrowserSetupError("Chrome started but its DevTools port is not reachable.")
    return {"cdp_url": url, "env": env, "started": started, "dirs": dirs}


def stop_browser(port=DEFAULT_PORT, proxy_port=DEFAULT_PROXY_PORT, log=print):
    """Close the backup Chrome (only the one using our profile) and drop the port proxy."""
    env = detect_env()
    dirs = app_dirs(env)
    if env == "wsl":
        prof = dirs["profile_host"].replace("'", "''")
        ps("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
           "Where-Object { $_.CommandLine -like '*%s*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % prof)
        log("Closed Chrome instances that use %s" % dirs["profile_host"])
        log("The port proxy and firewall rule are left in place (they are inert without Chrome). "
            "Remove them with: netsh interface portproxy delete v4tov4 listenport=%d ; "
            "Remove-NetFirewallRule -DisplayName '%s' (admin)." % (proxy_port, FIREWALL_RULE))
    else:
        subprocess.run(["pkill", "-f", "--user-data-dir=" + dirs["profile_host"]], capture_output=True)
        log("Closed Chrome instances that use %s" % dirs["profile_host"])
