"""
WiFi Channel Analyzer
======================
Scans nearby WiFi networks (2.4 GHz and 5 GHz) using Windows' built-in
`netsh wlan show networks mode=bssid` command, then recommends the least
congested channel on each band.

Requirements:
    - Windows 10/11
    - Python 3.8+ (stdlib only, no extra packages required)
    - WiFi adapter enabled

Run:
    python wifi_channel_analyzer.py

Build a standalone .exe (optional):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name WiFiChannelAnalyzer wifi_channel_analyzer.py

Author: built for Misbah / FPS Motion
"""

import re
import csv
import os
import time
import subprocess
import threading
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime

# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------

class Network:
    __slots__ = ("ssid", "bssid", "signal", "channel", "radio_type", "auth")

    def __init__(self, ssid, bssid, signal, channel, radio_type, auth):
        self.ssid = ssid
        self.bssid = bssid
        self.signal = signal          # 0-100 (%)
        self.channel = channel        # int
        self.radio_type = radio_type
        self.auth = auth

    @property
    def band(self):
        if self.channel is None:
            return "?"
        if 1 <= self.channel <= 14:
            return "2.4 GHz"
        if self.channel >= 32:
            return "5 GHz"
        return "?"


# ----------------------------------------------------------------------
# Scanner - wraps `netsh wlan show networks mode=bssid`
# ----------------------------------------------------------------------

class WifiScanner:
    """Runs netsh and parses the output into Network objects."""

    SSID_RE = re.compile(r"^SSID \d+\s*:\s*(.*)$")
    BSSID_RE = re.compile(r"^BSSID \d+\s*:\s*([0-9A-Fa-f:]{17})")
    SIGNAL_RE = re.compile(r"^Signal\s*:\s*(\d+)%")
    CHANNEL_RE = re.compile(r"^Channel\s*:\s*(\d+)")
    RADIO_RE = re.compile(r"^Radio type\s*:\s*(.+)$")
    AUTH_RE = re.compile(r"^Authentication\s*:\s*(.+)$")

    def scan(self):
        """Returns a list[Network]. Raises RuntimeError on failure."""
        try:
            result = subprocess.run(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except FileNotFoundError:
            raise RuntimeError("netsh not found. This tool only works on Windows.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Scan timed out. Try again.")

        # netsh often prints the real error to stdout, not stderr, even on
        # a non-zero exit code -- so check both and surface whichever has text.
        combined_output = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
        combined_output = combined_output.strip()

        if result.returncode != 0 or not (result.stdout or "").strip():
            hint = self._diagnose(combined_output)
            raise RuntimeError(
                f"netsh returned exit code {result.returncode}.\n\n"
                f"Raw output:\n{combined_output or '(empty -- no output at all)'}\n\n"
                f"{hint}"
            )

        return self._parse(result.stdout)

    @staticmethod
    def _diagnose(output):
        """Best-effort human-readable hint based on netsh's known error phrases."""
        low = output.lower()
        needs_location = "location" in low and ("permission" in low or "privacy" in low)
        needs_admin = "elevation" in low or "administrator" in low

        if needs_location and needs_admin:
            return ("Fix (both needed):\n"
                    "1. Turn on Location Services: Settings -> Privacy & security -> Location "
                    "-> turn ON 'Location services', and turn ON 'Let desktop apps access your "
                    "location' further down that page. Shortcut: run 'start ms-settings:privacy-location'.\n"
                    "2. Run this app as Administrator (right-click -> Run as administrator).")
        if needs_location:
            return ("Fix: Turn on Location Services -- Settings -> Privacy & security -> Location "
                    "-> turn ON 'Location services', and turn ON 'Let desktop apps access your "
                    "location' further down that page. Shortcut: run 'start ms-settings:privacy-location'.")
        if needs_admin:
            return "Fix: Run this app as Administrator (right-click -> Run as administrator)."
        if "wireless autoconfig" in low or "wlansvc" in low:
            return ("Fix: the Wireless AutoConfig service isn't running. Open Services "
                    "(services.msc), find 'WLAN AutoConfig', and set it to Running / Automatic.")
        if "no wireless interface" in low or "there is no wireless" in low:
            return ("Fix: Windows doesn't see a WiFi adapter at all. Check Device Manager "
                    "for the network adapter, update/reinstall its driver, and confirm "
                    "it's not disabled there.")
        if not output:
            return ("Fix: try running the app as Administrator (right-click -> Run as "
                    "administrator). Also open Command Prompt and run this exact command "
                    "manually to see the real error:\n"
                    "  netsh wlan show networks mode=bssid")
        return ("Try running Command Prompt as Administrator and pasting this command "
                "to see the exact error Windows gives:\n"
                "  netsh wlan show networks mode=bssid")

    def _parse(self, text):
        networks = []
        current_ssid = None
        current_auth = None
        pending = None  # dict for the BSSID currently being filled in

        def flush():
            if pending and pending.get("channel") is not None:
                networks.append(Network(
                    ssid=current_ssid or "(hidden)",
                    bssid=pending.get("bssid", "?"),
                    signal=pending.get("signal", 0),
                    channel=pending.get("channel"),
                    radio_type=pending.get("radio", "?"),
                    auth=current_auth or "?",
                ))

        for raw_line in text.splitlines():
            line = raw_line.strip()

            m = self.SSID_RE.match(line)
            if m:
                flush()
                pending = None
                current_ssid = m.group(1).strip() or "(hidden)"
                continue

            m = self.AUTH_RE.match(line)
            if m:
                current_auth = m.group(1).strip()
                continue

            m = self.BSSID_RE.match(line)
            if m:
                flush()
                pending = {"bssid": m.group(1)}
                continue

            if pending is None:
                continue

            m = self.SIGNAL_RE.match(line)
            if m:
                pending["signal"] = int(m.group(1))
                continue

            m = self.CHANNEL_RE.match(line)
            if m:
                pending["channel"] = int(m.group(1))
                continue

            m = self.RADIO_RE.match(line)
            if m:
                pending["radio"] = m.group(1).strip()
                continue

        flush()
        return networks


# ----------------------------------------------------------------------
# Current connection - wraps `netsh wlan show interfaces`
# ----------------------------------------------------------------------

def get_current_connection():
    """Returns a dict of the currently-connected WiFi interface's fields
    (SSID, BSSID, Channel, Signal, Radio type, etc.), or None if not
    connected / not available. Best-effort; never raises."""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception:
        return None

    if result.returncode != 0 or not (result.stdout or "").strip():
        return None

    info = {}
    for raw_line in result.stdout.splitlines():
        if ":" not in raw_line:
            continue
        key, _, val = raw_line.partition(":")
        key, val = key.strip(), val.strip()
        if key and val:
            info[key] = val

    if info.get("State", "").lower() != "connected":
        return None
    return info


def is_admin():
    """Returns True/False if elevation status can be determined, else None."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return None


def open_location_settings():
    """Opens Windows' Location privacy settings page. Best-effort; never raises."""
    try:
        os.startfile("ms-settings:privacy-location")
    except Exception:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:privacy-location"])
        except Exception:
            pass


# ----------------------------------------------------------------------
# Speed Test - download/upload/latency via Cloudflare's public speed-test
# endpoints (the same infrastructure speed.cloudflare.com uses in-browser).
# Stdlib only, no extra packages required.
# ----------------------------------------------------------------------

class SpeedTester:
    DOWNLOAD_URL = "https://speed.cloudflare.com/__down"
    UPLOAD_URL = "https://speed.cloudflare.com/__up"
    PING_URL = "https://speed.cloudflare.com/__down?bytes=0"

    # Cloudflare's edge bot-protection rejects urllib's default User-Agent
    # ("Python-urllib/3.x") with a 403. Presenting browser-like headers
    # (matching what speed.cloudflare.com's own page sends) avoids that.
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "*/*",
        "Referer": "https://speed.cloudflare.com/",
        "Origin": "https://speed.cloudflare.com",
    }

    def measure_ping(self, samples=6, timeout=5):
        """Returns {'min','avg','max','jitter'} in milliseconds."""
        times_ms = []
        last_error = None
        for _ in range(samples):
            start = time.perf_counter()
            try:
                req = urllib.request.Request(self.PING_URL, headers=self.HEADERS)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp.read()
                times_ms.append((time.perf_counter() - start) * 1000)
            except Exception as e:
                last_error = e
                continue
        if not times_ms:
            detail = f" ({last_error})" if last_error else ""
            raise RuntimeError(
                f"Could not reach the speed test server{detail}. "
                "Check your internet connection, or a firewall/antivirus may be blocking it."
            )
        return {
            "min": min(times_ms),
            "avg": sum(times_ms) / len(times_ms),
            "max": max(times_ms),
            "jitter": max(times_ms) - min(times_ms),
        }

    def measure_download(self, size_bytes=35_000_000, max_seconds=8, progress_cb=None):
        """Returns Mbps. Streams up to size_bytes or stops after max_seconds,
        whichever comes first, so it works reasonably on both fast and slow links."""
        url = f"{self.DOWNLOAD_URL}?bytes={size_bytes}"
        downloaded = 0
        start = time.perf_counter()
        try:
            req = urllib.request.Request(url, headers=self.HEADERS)
            with urllib.request.urlopen(req, timeout=15) as resp:
                while True:
                    chunk = resp.read(262_144)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    elapsed = time.perf_counter() - start
                    if progress_cb and elapsed > 0:
                        progress_cb(downloaded, size_bytes, (downloaded * 8 / 1_000_000) / elapsed)
                    if elapsed >= max_seconds:
                        break
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Download test failed: server returned HTTP {e.code} ({e.reason}).")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise RuntimeError(
                f"Download test failed: {reason}. A firewall or antivirus (e.g. ESET) may be "
                "blocking this app's outbound connection -- try adding an exception for it, "
                "or temporarily disable the firewall to confirm."
            )

        elapsed = time.perf_counter() - start
        if elapsed <= 0 or downloaded == 0:
            raise RuntimeError("Download test failed: no data received.")
        return (downloaded * 8 / 1_000_000) / elapsed


    def measure_upload(self, size_bytes=8_000_000, timeout=25):
        """Returns Mbps. Sends random bytes since real payload content doesn't matter."""
        payload = os.urandom(size_bytes)
        headers = dict(self.HEADERS)
        headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self.UPLOAD_URL, data=payload, method="POST", headers=headers)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Upload test failed: server returned HTTP {e.code} ({e.reason}).")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise RuntimeError(
                f"Upload test failed: {reason}. A firewall or antivirus (e.g. ESET) may be "
                "blocking or resetting this app's outbound connection -- try adding an "
                "exception for it, or temporarily disable it to confirm."
            )

        elapsed = time.perf_counter() - start
        if elapsed <= 0:
            raise RuntimeError("Upload test failed: no timing data.")
        return (size_bytes * 8 / 1_000_000) / elapsed


# ----------------------------------------------------------------------
# Analysis - congestion scoring + best-channel recommendation
# ----------------------------------------------------------------------

# 20MHz-spaced overlap: channels within 4 of each other overlap on 2.4GHz
# (channel spacing is 5MHz, a 20MHz-wide signal spans ~4 channels each side).
NONOVERLAPPING_24 = (1, 6, 11)
ALL_24_CHANNELS = list(range(1, 14))

# Common 5GHz channels (20MHz). DFS channels require radar detection and are
# best avoided when a clean non-DFS channel is available.
CHANNELS_5G_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161, 165]
CHANNELS_5G_DFS = [52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144]
ALL_5G_CHANNELS = CHANNELS_5G_NON_DFS + CHANNELS_5G_DFS


def _signal_weight(signal_pct):
    """Convert 0-100% signal into a rough interference weight.
    Stronger nearby networks cause more real-world interference."""
    return max(signal_pct, 5) / 100.0


def _overlap_weight_24(target_ch, network_ch):
    diff = abs(target_ch - network_ch)
    if diff >= 5:
        return 0.0
    # triangular falloff: 0 diff -> 1.0, 4 diff -> 0.2
    return max(0.0, 1.0 - diff * 0.2)


def analyze_24ghz(networks):
    """Returns (scores dict {channel: score}, best_channel, all networks 2.4)."""
    nets24 = [n for n in networks if n.band == "2.4 GHz"]
    scores = {}
    for ch in ALL_24_CHANNELS:
        score = 0.0
        for n in nets24:
            score += _signal_weight(n.signal) * _overlap_weight_24(ch, n.channel)
        scores[ch] = round(score, 2)

    # Prefer the standard non-overlapping set (1/6/11) when picking the winner,
    # since mixing on non-standard channels causes more interference for everyone.
    best = min(NONOVERLAPPING_24, key=lambda c: (scores[c], c))
    return scores, best, nets24


def analyze_5ghz(networks):
    """Returns (scores dict {channel: score}, best_channel, all networks 5G)."""
    nets5 = [n for n in networks if n.band == "5 GHz"]
    seen_channels = sorted(set(n.channel for n in nets5)) or []
    candidate_channels = sorted(set(ALL_5G_CHANNELS) | set(seen_channels))

    scores = {}
    for ch in candidate_channels:
        score = 0.0
        for n in nets5:
            if n.channel == ch:
                score += _signal_weight(n.signal)
        scores[ch] = round(score, 2)

    # Prefer non-DFS channels; only fall back to DFS if all non-DFS are busy.
    non_dfs_candidates = [c for c in CHANNELS_5G_NON_DFS]
    best = min(non_dfs_candidates, key=lambda c: (scores.get(c, 0.0), c))
    if scores.get(best, 0.0) > 0.0:
        # see if a completely empty DFS channel exists instead
        empty_dfs = [c for c in CHANNELS_5G_DFS if scores.get(c, 0.0) == 0.0]
        if empty_dfs and scores.get(best, 0.0) > 0.0:
            best = min(empty_dfs)
    return scores, best, nets5


# ----------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------

BG = "#1e1f26"
PANEL = "#262832"
FG = "#e8e8ee"
MUTED = "#9a9db0"
ACCENT = "#5eb4ff"
GOOD = "#5ed49c"
WARN = "#ffb454"
BAD = "#ff6b6b"
GRID = "#3a3d4a"
WARN_BG = "#332c1c"
WARN_BORDER = "#5a4a24"


class ChannelBarChart(tk.Canvas):
    """Simple bar chart drawn on a tkinter Canvas (no extra dependencies)."""

    def __init__(self, master, **kwargs):
        super().__init__(master, bg=PANEL, highlightthickness=0, **kwargs)

    def draw(self, scores, best_channel, title, highlight_set=()):
        self.delete("all")
        w = int(self["width"]) if self["width"] else self.winfo_width()
        h = int(self["height"]) if self["height"] else self.winfo_height()
        if w < 10 or not scores:
            return

        pad_left, pad_bottom, pad_top = 34, 26, 24
        chart_w = w - pad_left - 10
        chart_h = h - pad_bottom - pad_top

        max_score = max(scores.values()) if scores else 1.0
        max_score = max_score if max_score > 0 else 1.0

        channels = list(scores.keys())
        n = len(channels)
        bar_w = max(chart_w / n - 4, 3)

        self.create_text(w / 2, 12, text=title, fill=FG, font=("Segoe UI", 10, "bold"))

        for i, ch in enumerate(channels):
            score = scores[ch]
            bar_h = (score / max_score) * chart_h if max_score else 0
            x0 = pad_left + i * (chart_w / n) + 2
            x1 = x0 + bar_w
            y1 = pad_top + chart_h
            y0 = y1 - bar_h

            if ch == best_channel:
                color = GOOD
            elif score / max_score > 0.6:
                color = BAD
            elif score / max_score > 0.25:
                color = WARN
            else:
                color = ACCENT if ch in highlight_set else "#4a4d5c"

            self.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
            # channel label (rotate-free, just small text, skip some if crowded)
            if n <= 15 or i % 2 == 0:
                self.create_text((x0 + x1) / 2, y1 + 12, text=str(ch), fill=FG, font=("Segoe UI", 7))

        # baseline
        self.create_line(pad_left, pad_top + chart_h, pad_left + chart_w, pad_top + chart_h, fill=GRID)


class WifiAnalyzerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("WiFi Channel Analyzer")
        self.geometry("980x720")
        self.minsize(860, 620)
        self.configure(bg=BG)

        self.scanner = WifiScanner()
        self.speed_tester = SpeedTester()
        self.last_networks = []

        self._build_style()
        self._build_layout()
        self.after(300, self.run_scan)
        self.after(600, self.refresh_connection_info)

    # ---------------- UI construction ----------------

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview",
                         background=PANEL, fieldbackground=PANEL, foreground=FG,
                         rowheight=24, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                         background="#31333f", foreground=FG, font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", "#3a5b8c")])
        style.configure("TButton", font=("Segoe UI", 9), padding=6)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=FG,
                         padding=(16, 8), font=("Segoe UI", 9, "bold"), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#0b0c10")])

    def _build_layout(self):
        # Top bar: title + subtitle + admin status chip
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 4))

        title_col = tk.Frame(top, bg=BG)
        title_col.pack(side="left")
        tk.Label(title_col, text="WiFi Channel Analyzer", bg=BG, fg=FG,
                 font=("Segoe UI", 16, "bold")).pack(anchor="w")
        tk.Label(title_col, text="Find the clearest channel, then test your speed to confirm it helped.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")

        self.admin_chip = tk.Label(top, bg=BG, font=("Segoe UI", 9, "bold"))
        self.admin_chip.pack(side="right", anchor="n")

        self._build_warning_banner()

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(4, 10))

        scanner_tab = tk.Frame(self.notebook, bg=BG)
        speed_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(scanner_tab, text="  📡  Channel Scanner  ")
        self.notebook.add(speed_tab, text="  ⚡  Speed Test  ")

        self._build_scanner_tab(scanner_tab)
        self._build_speed_tab(speed_tab)

        self.bind("<Configure>", lambda e: self._redraw_charts())
        self._update_admin_chip()

    def _build_warning_banner(self):
        """Dismissible banner explaining why Windows needs Location Services +
        admin rights, shown proactively instead of only after a failed scan."""
        self.banner = tk.Frame(self, bg=WARN_BG, highlightbackground=WARN_BORDER,
                                highlightthickness=1, padx=14, pady=10)
        self.banner.pack(fill="x", padx=16, pady=(8, 4))
        self.banner.columnconfigure(1, weight=1)

        tk.Label(self.banner, text="⚠", bg=WARN_BG, fg=WARN, font=("Segoe UI", 16, "bold"))\
            .grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 12))

        tk.Label(
            self.banner, bg=WARN_BG, fg=FG, font=("Segoe UI", 9), justify="left", anchor="w",
            text=("Before scanning: Windows treats WiFi network lists as location data. "
                  "Two things need to be true, or the scan will fail:\n"
                  "1)  Location Services is ON  (Settings → Privacy & security → Location)\n"
                  "2)  This app is running as Administrator"),
        ).grid(row=0, column=1, sticky="w")

        btn_row = tk.Frame(self.banner, bg=WARN_BG)
        btn_row.grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Button(btn_row, text="Open Location Settings", command=open_location_settings).pack(side="left")

        close_btn = tk.Label(self.banner, text="✕", bg=WARN_BG, fg=MUTED,
                              font=("Segoe UI", 12, "bold"), cursor="hand2")
        close_btn.grid(row=0, column=2, sticky="ne")
        close_btn.bind("<Button-1>", lambda e: self.banner.pack_forget())

    def _update_admin_chip(self):
        admin = is_admin()
        if admin is True:
            self.admin_chip.config(text="●  Running as Administrator", fg=GOOD)
        elif admin is False:
            self.admin_chip.config(text="●  NOT running as Administrator", fg=BAD)
        else:
            self.admin_chip.config(text="")

    def _build_scanner_tab(self, parent):
        # Controls
        ctrl = tk.Frame(parent, bg=BG)
        ctrl.pack(fill="x", padx=4, pady=(6, 6))

        self.scan_btn = ttk.Button(ctrl, text="🔄 Scan Now", command=self.run_scan)
        self.scan_btn.pack(side="right", padx=(6, 0))

        self.export_btn = ttk.Button(ctrl, text="Export CSV", command=self.export_csv)
        self.export_btn.pack(side="right")

        self.status_var = tk.StringVar(value="Starting...")
        status_lbl = tk.Label(ctrl, textvariable=self.status_var, bg=BG, fg=MUTED,
                               font=("Segoe UI", 8), anchor="w")
        status_lbl.pack(side="left")

        # Recommendation cards
        rec_frame = tk.Frame(parent, bg=BG)
        rec_frame.pack(fill="x", padx=4, pady=6)
        rec_frame.columnconfigure(0, weight=1)
        rec_frame.columnconfigure(1, weight=1)

        self.card24 = self._make_recommendation_card(rec_frame, "2.4 GHz Recommendation")
        self.card24.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.card5 = self._make_recommendation_card(rec_frame, "5 GHz Recommendation")
        self.card5.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Charts
        chart_frame = tk.Frame(parent, bg=BG)
        chart_frame.pack(fill="x", padx=4, pady=(0, 10))
        chart_frame.columnconfigure(0, weight=1)
        chart_frame.columnconfigure(1, weight=1)

        self.chart24 = ChannelBarChart(chart_frame, height=150)
        self.chart24.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.chart5 = ChannelBarChart(chart_frame, height=150)
        self.chart5.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        # Network list
        list_frame = tk.Frame(parent, bg=BG)
        list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))

        cols = ("ssid", "band", "channel", "signal", "bssid", "radio", "auth")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings")
        headings = {
            "ssid": ("SSID", 160), "band": ("Band", 70), "channel": ("Ch", 40),
            "signal": ("Signal", 70), "bssid": ("BSSID", 140),
            "radio": ("Radio", 90), "auth": ("Security", 130),
        }
        for c in cols:
            text, width = headings[c]
            self.tree.heading(c, text=text, command=lambda cc=c: self._sort_tree(cc, False))
            self.tree.column(c, width=width, anchor="w")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_speed_tab(self, parent):
        # Currently-connected network info
        conn_shell = self._card_shell(parent, ACCENT)
        conn_shell.pack(fill="x", padx=4, pady=(6, 10))
        conn_frame = conn_shell.inner
        conn_frame.columnconfigure(0, weight=1)

        self.conn_info_var = tk.StringVar(value="Checking current connection...")
        tk.Label(conn_frame, textvariable=self.conn_info_var, bg=PANEL, fg=FG,
                 font=("Segoe UI", 9), anchor="w", justify="left").grid(row=0, column=0, sticky="w")
        ttk.Button(conn_frame, text="Refresh", command=self.refresh_connection_info)\
            .grid(row=0, column=1, sticky="e", padx=(10, 0))

        # Controls
        ctrl = tk.Frame(parent, bg=BG)
        ctrl.pack(fill="x", padx=4, pady=(0, 10))
        self.speed_btn = ttk.Button(ctrl, text="▶  Start Speed Test", command=self.run_speed_test)
        self.speed_btn.pack(side="left")
        self.speed_status_var = tk.StringVar(value="Ready. This uses ~40MB of data per test.")
        tk.Label(ctrl, textvariable=self.speed_status_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left", padx=12)

        # Metric cards
        metrics = tk.Frame(parent, bg=BG)
        metrics.pack(fill="x", padx=4, pady=(0, 10))
        metrics.columnconfigure(0, weight=1)
        metrics.columnconfigure(1, weight=1)
        metrics.columnconfigure(2, weight=1)

        self.ping_card = self._make_metric_card(metrics, "Ping / Latency")
        self.ping_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.download_card = self._make_metric_card(metrics, "Download")
        self.download_card.grid(row=0, column=1, sticky="nsew", padx=6)
        self.upload_card = self._make_metric_card(metrics, "Upload")
        self.upload_card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        # History log - lets you compare results before/after switching channels
        log_frame = tk.Frame(parent, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        tk.Label(log_frame, text="Test History (compare before/after switching channels)",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 4))

        cols = ("time", "ssid", "channel", "ping", "download", "upload")
        self.speed_tree = ttk.Treeview(log_frame, columns=cols, show="headings", height=7)
        headings = {
            "time": ("Time", 80), "ssid": ("SSID", 150), "channel": ("Ch", 40),
            "ping": ("Ping (ms)", 90), "download": ("Down (Mbps)", 110), "upload": ("Up (Mbps)", 100),
        }
        for c in cols:
            text, width = headings[c]
            self.speed_tree.heading(c, text=text)
            self.speed_tree.column(c, width=width, anchor="w")

        vsb2 = ttk.Scrollbar(log_frame, orient="vertical", command=self.speed_tree.yview)
        self.speed_tree.configure(yscrollcommand=vsb2.set)
        self.speed_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")

    def _card_shell(self, parent, accent):
        """A panel with a thin colored accent bar on the left edge."""
        outer = tk.Frame(parent, bg=PANEL)
        tk.Frame(outer, bg=accent, width=4).pack(side="left", fill="y")
        inner = tk.Frame(outer, bg=PANEL, padx=14, pady=10)
        inner.pack(side="left", fill="both", expand=True)
        outer.inner = inner
        return outer

    def _make_metric_card(self, parent, title):
        card = self._card_shell(parent, ACCENT)
        inner = card.inner
        tk.Label(inner, text=title, bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        big = tk.Label(inner, text="—", bg=PANEL, fg=ACCENT, font=("Segoe UI", 22, "bold"))
        big.pack(anchor="w", pady=(2, 0))
        detail = tk.Label(inner, text="", bg=PANEL, fg=FG, font=("Segoe UI", 8), justify="left")
        detail.pack(anchor="w")
        card.big_label = big
        card.detail_label = detail
        return card

    def _make_recommendation_card(self, parent, title):
        card = self._card_shell(parent, GOOD)
        inner = card.inner
        tk.Label(inner, text=title, bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(anchor="w")
        big = tk.Label(inner, text="—", bg=PANEL, fg=GOOD, font=("Segoe UI", 26, "bold"))
        big.pack(anchor="w", pady=(2, 0))
        detail = tk.Label(inner, text="", bg=PANEL, fg=FG, font=("Segoe UI", 9), justify="left", wraplength=420)
        detail.pack(anchor="w", pady=(4, 0))
        card.big_label = big
        card.detail_label = detail
        return card

    # ---------------- Scanning ----------------

    def run_scan(self):
        self.scan_btn.config(state="disabled")
        self.status_var.set("Scanning...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            networks = self.scanner.scan()
            error = None
        except RuntimeError as e:
            networks = []
            error = str(e)
        self.after(0, self._on_scan_done, networks, error)

    def _on_scan_done(self, networks, error):
        self.scan_btn.config(state="normal")
        if error:
            self.status_var.set("Scan failed.")
            messagebox.showerror("Scan Error", error)
            return

        self.last_networks = networks
        ts = datetime.now().strftime("%H:%M:%S")
        self.status_var.set(f"Found {len(networks)} network(s) across both bands — last scan {ts}")

        self._populate_tree(networks)
        self._update_recommendations(networks)

    # ---------------- Rendering ----------------

    def _populate_tree(self, networks):
        self.tree.delete(*self.tree.get_children())
        for n in sorted(networks, key=lambda x: -x.signal):
            self.tree.insert("", "end", values=(
                n.ssid, n.band, n.channel, f"{n.signal}%", n.bssid, n.radio_type, n.auth
            ))

    def _sort_tree(self, col, reverse):
        items = [(self.tree.set(k, col), k) for k in self.tree.get_children("")]
        try:
            items.sort(key=lambda t: float(str(t[0]).rstrip("%")), reverse=reverse)
        except ValueError:
            items.sort(key=lambda t: t[0], reverse=reverse)
        for index, (_, k) in enumerate(items):
            self.tree.move(k, "", index)
        self.tree.heading(col, command=lambda: self._sort_tree(col, not reverse))

    def _update_recommendations(self, networks):
        self._scores24, self._best24, nets24 = analyze_24ghz(networks)
        self._scores5, self._best5, nets5 = analyze_5ghz(networks)

        self.card24.big_label.config(text=f"Channel {self._best24}")
        self.card24.detail_label.config(text=(
            f"{len(nets24)} network(s) seen on 2.4 GHz.\n"
            f"Congestion score: {self._scores24[self._best24]} (lower is better).\n"
            f"Standard non-overlapping channels are 1, 6, and 11 — this pick has the least "
            f"combined interference from nearby networks."
        ))

        self.card5.big_label.config(text=f"Channel {self._best5}")
        dfs_note = " (DFS channel — may briefly switch if radar is detected)" if self._best5 in CHANNELS_5G_DFS else ""
        self.card5.detail_label.config(text=(
            f"{len(nets5)} network(s) seen on 5 GHz.\n"
            f"Congestion score: {self._scores5.get(self._best5, 0.0)} (lower is better).\n"
            f"Non-DFS channels are preferred for stability{dfs_note}."
        ))

        self._redraw_charts()

    def _redraw_charts(self):
        if hasattr(self, "_scores24"):
            self.chart24.draw(self._scores24, self._best24, "2.4 GHz channel congestion",
                               highlight_set=NONOVERLAPPING_24)
        if hasattr(self, "_scores5"):
            self.chart5.draw(self._scores5, self._best5, "5 GHz channel congestion",
                              highlight_set=CHANNELS_5G_NON_DFS)

    # ---------------- Speed Test ----------------

    def refresh_connection_info(self):
        threading.Thread(target=self._connection_info_worker, daemon=True).start()

    def _connection_info_worker(self):
        conn = get_current_connection()
        self.after(0, self._update_connection_info, conn)

    def _update_connection_info(self, conn):
        self._current_conn = conn
        if conn is None:
            self.conn_info_var.set(
                "Not currently connected to a WiFi network (or unable to detect connection)."
            )
            return
        ssid = conn.get("SSID", "?")
        channel = conn.get("Channel", "?")
        signal = conn.get("Signal", "?")
        radio = conn.get("Radio type", "?")
        rx = conn.get("Receive rate (Mbps)", "?")
        tx = conn.get("Transmit rate (Mbps)", "?")
        self.conn_info_var.set(
            f"Connected to:  {ssid}   |   Channel {channel}   |   Signal {signal}   |   "
            f"{radio}   |   Link rate {rx}/{tx} Mbps (rx/tx)"
        )

    def run_speed_test(self):
        self.speed_btn.config(state="disabled")
        self.speed_status_var.set("Starting test...")
        for card in (self.ping_card, self.download_card, self.upload_card):
            card.big_label.config(text="—")
            card.detail_label.config(text="")
        threading.Thread(target=self._speed_test_worker, daemon=True).start()

    def _speed_test_worker(self):
        conn = get_current_connection()
        self.after(0, self._update_connection_info, conn)

        tester = self.speed_tester
        results = {}
        errors = []

        self.after(0, self.speed_status_var.set, "Testing latency...")
        try:
            ping = tester.measure_ping()
            results["ping"] = ping
            self.after(0, self._update_ping_ui, ping)
        except RuntimeError as e:
            errors.append(str(e))

        self.after(0, self.speed_status_var.set, "Testing download speed...")
        try:
            def dl_cb(downloaded, total, mbps):
                self.after(0, self._update_download_progress, downloaded, mbps)
            dl_mbps = tester.measure_download(progress_cb=dl_cb)
            results["download"] = dl_mbps
            self.after(0, self._update_download_ui, dl_mbps)
        except RuntimeError as e:
            errors.append(str(e))

        self.after(0, self.speed_status_var.set, "Testing upload speed...")
        try:
            up_mbps = tester.measure_upload()
            results["upload"] = up_mbps
            self.after(0, self._update_upload_ui, up_mbps)
        except RuntimeError as e:
            errors.append(str(e))

        self.after(0, self._speed_test_done, results, errors, conn)

    def _update_ping_ui(self, ping):
        self.ping_card.big_label.config(text=f"{ping['avg']:.0f} ms")
        self.ping_card.detail_label.config(
            text=f"min {ping['min']:.0f} · max {ping['max']:.0f} · jitter {ping['jitter']:.0f} ms"
        )

    def _update_download_progress(self, downloaded, mbps):
        self.download_card.big_label.config(text=f"{mbps:.1f} Mbps")
        self.download_card.detail_label.config(text=f"{downloaded / 1_000_000:.1f} MB transferred...")

    def _update_download_ui(self, mbps):
        self.download_card.big_label.config(text=f"{mbps:.1f} Mbps")
        self.download_card.detail_label.config(text="Done")

    def _update_upload_ui(self, mbps):
        self.upload_card.big_label.config(text=f"{mbps:.1f} Mbps")
        self.upload_card.detail_label.config(text="Done")

    def _speed_test_done(self, results, errors, conn):
        self.speed_btn.config(state="normal")
        if errors:
            self.speed_status_var.set("Completed with errors — see details.")
            messagebox.showwarning(
                "Speed Test", "Some parts of the test couldn't complete:\n\n" + "\n\n".join(errors)
            )
        else:
            self.speed_status_var.set(f"Test complete — {datetime.now().strftime('%H:%M:%S')}")

        ssid = conn.get("SSID", "-") if conn else "-"
        channel = conn.get("Channel", "-") if conn else "-"
        ping_txt = f"{results['ping']['avg']:.0f}" if "ping" in results else "-"
        down_txt = f"{results['download']:.1f}" if "download" in results else "-"
        up_txt = f"{results['upload']:.1f}" if "upload" in results else "-"
        self.speed_tree.insert("", 0, values=(
            datetime.now().strftime("%H:%M:%S"), ssid, channel, ping_txt, down_txt, up_txt
        ))

    # ---------------- Export ----------------

    def export_csv(self):
        if not self.last_networks:
            messagebox.showinfo("Nothing to export", "Run a scan first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV file", "*.csv")],
            initialfile=f"wifi_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["SSID", "Band", "Channel", "Signal %", "BSSID", "Radio Type", "Security"])
            for n in self.last_networks:
                writer.writerow([n.ssid, n.band, n.channel, n.signal, n.bssid, n.radio_type, n.auth])
        messagebox.showinfo("Exported", f"Saved to:\n{path}")


if __name__ == "__main__":
    app = WifiAnalyzerApp()
    app.mainloop()
