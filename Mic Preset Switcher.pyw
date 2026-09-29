"""Mic Preset Switcher

Small front end for my Equalizer APO voice presets. Clicking a preset rewrites
config.txt so APO includes that voice_*.txt file on the CABLE Input device
(mic > Listen > CABLE Input > APO > CABLE Output, which every app uses).

New preset: drop voice_<name>.txt in the APO config folder and add a line to
PRESETS. Anything not listed still shows up, just with a generic description.

Pass the selftest flag (see the bottom of the file) to check the config round trip.
"""

import array
import collections
import ctypes
import math
import os
import re
import shutil
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
import winreg
from tkinter import messagebox

APO_CONFIG = r"C:\Program Files\EqualizerAPO\config"
CONFIG_FILE = os.path.join(APO_CONFIG, "config.txt")
DEVICE = "CABLE Input"

PRESETS = [
    # file                     name                          description
    ("voice_soft_full.txt",   "Soft & Rich",                "My normal voice, warm and clear"),
    ("voice_soft.txt",        "Soft & Rich, no saturation", "Same thing, just a bit drier"),
    ("voice_warm.txt",        "Warm Radio",                 "Close and cosy, podcast vibe"),
    ("voice_clear.txt",       "Clear",                      "Bright, cuts through loud games"),
    ("voice_baby.txt",        "Baby",                       "Tiny baby voice"),
    ("voice_oldman.txt",      "Old Man",                    "Thin and nasal, a bit lower"),
    ("voice_giant.txt",       "Giant",                      "Huge voice in a big hall"),
    ("voice_scary.txt",       "Deep Monster",               "Low and gritty but you can still understand it"),
    ("voice_demon.txt",       "Demon",                      "A full octave down with distortion"),
    ("voice_ghost.txt",       "Ghost",                      "Two voices with a long airy tail"),
    ("voice_scary_high.txt",  "Scary High",                 "High and creepy"),
    ("voice_robot.txt",       "Robot",                      "Metallic buzz"),
    ("voice_alien.txt",       "Alien",                      "Weird harmony with a metallic ring"),
    ("voice_radio.txt",       "Radio",                      "Walkie talkie with some grit"),
    ("voice_telephone.txt",   "Telephone",                  "Sounds like a phone call"),
    ("voice_megaphone.txt",   "Megaphone",                  "Loud, crunchy and boxy"),
    ("voice_cave.txt",        "Cave",                       "Echoes bouncing back at you"),
    ("voice_stadium.txt",     "Stadium",                    "Announcer in a huge arena"),
    ("voice_underwater.txt",  "Underwater",                 "Muffled, like talking under water"),
]

# Krisp in Discord eats reverb/pitch/distortion, so the effect voices get a reminder.
NATURAL = {"voice_soft_full.txt", "voice_soft.txt", "voice_warm.txt", "voice_clear.txt"}

# old copies I keep around (voice_x_SAVED.txt, voice_x_v1.txt) stay out of the list
HIDDEN = re.compile(r"_(SAVED|v\d+)\.txt$", re.IGNORECASE)

LEVEL_MIN, LEVEL_MAX = -12, 6

# BG is also the transparency key: pixels in exactly this colour turn transparent,
# so never use it for anything that should stay solid.
BG = "#1c1c1d"
GLASS_TINT = 0x991C1C1C     # AABBGGRR, ~60% dark tint over whatever is behind
WINDOW_ALPHA = 0.96
CARD, CARD_HOVER, STROKE = "#2b2b2b", "#323232", "#3a3a3a"
TEXT, MUTED, TRACK = "#ffffff", "#a0a0a0", "#5c5c5c"


# @ windows bits

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin():
    # config.txt lives in Program Files, so writing it needs admin
    exe = sys.executable.replace("python.exe", "pythonw.exe")
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, f'"{os.path.abspath(sys.argv[0])}"', None, 1)


def windows_accent():
    # entry 1 of the palette is the lighter shade Windows itself uses in dark mode
    try:
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Accent"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            palette, _ = winreg.QueryValueEx(k, "AccentPalette")
        return "#%02x%02x%02x" % tuple(palette[4:7])
    except OSError:
        return "#60cdff"


# First glass attempt: stretched the DWM frame over the whole client area. The blur looked
# great but every bit of text went fuzzy, because GDI draws with zero alpha. Parked for now.
#
# def enable_full_glass(root):
#     hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
#     full = Margins(*[0xFFFFFFFF] * 4)
#     ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(full))
#     ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(ctypes.c_int(3)), 4)


def style_window(root):
    """Dark title bar matching the window, plus the blurred transparent background."""
    root.wm_attributes("-transparentcolor", BG)
    root.wm_attributes("-alpha", WINDOW_ALPHA)

    class AccentPolicy(ctypes.Structure):
        _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int),
                    ("colour", ctypes.c_uint), ("animation", ctypes.c_int)]

    class CompositionData(ctypes.Structure):
        _fields_ = [("attribute", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]

    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(ctypes.c_int(1)), 4)      # dark title bar
        r, g, b = (int(BG[i:i + 2], 16) for i in (1, 3, 5))
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(ctypes.c_int(r | g << 8 | b << 16)), 4)

        policy = AccentPolicy(4, 2, GLASS_TINT, 0)   # 4 = acrylic blur behind, 2 = apply the tint
        data = CompositionData(19, ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p), ctypes.sizeof(policy))
        ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))
    except (AttributeError, OSError):
        pass    # older Windows: still works, just no blur


# @ config.txt

def read_file(path):
    try:
        with open(path, encoding="utf-8", errors="ignore", newline="") as f:
            return f.read()
    except OSError:
        return ""


def parse_config(text):
    """returns (active preset file, overall level in dB)"""
    active, level = None, 0.0
    for line in text.splitlines():
        if active is None and (m := re.match(r"\s*Include:\s*(.+?)\s*$", line)):
            active = m.group(1)
        elif m := re.match(r"\s*Preamp:\s*(-?[\d.]+)", line):
            level = float(m.group(1))
    return active, level


def render_config(active, presets, level):
    lines = ["# ===== VIRTUAL MIC (CABLE Input) ONLY =====",
             f"Device: {DEVICE}",
             "",
             "# Managed by Mic Preset Switcher. The active preset has no # in front."]
    lines += [f"{'' if f == active else '# '}Include: {f}" for f, _, _ in presets]
    if level:
        lines += ["", "# Overall mic level (Mic Preset Switcher slider)", f"Preamp: {level:g} dB"]
    lines += ["", "# ===== EVERYTHING ELSE: no processing =====", "Device: all", ""]
    return "\r\n".join(lines)

"""
# class ConfigWriter:
#     def __init__(self):
#         self.backed_up = False
#         self.last = None
#
#     def write(self, active, presets, level):
#         text = render_config(active, presets, level)
#         if text == self.last:
#             return
#         with open(CONFIG_FILE, "w", encoding="utf8", newline="") as f:
#             f.write(text)
#         self.last = text

"""

_backed_up = False

def write_config(active, presets, level):
    global _backed_up
    text = render_config(active, presets, level)
    # compare against the file, not the last thing written; I edit it outside the app sometimes
    if text == read_file(CONFIG_FILE):
        return
    if not _backed_up:
        shutil.copyfile(CONFIG_FILE, os.path.join(APO_CONFIG, "config_before_switcher.txt"))
        _backed_up = True
    # Plain overwrite on purpose. A temp file + rename makes APO reload twice.
    with open(CONFIG_FILE, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def available_presets():
    files = {f.lower(): f for f in os.listdir(APO_CONFIG)}
    known = {p[0].lower() for p in PRESETS}
    found = [p for p in PRESETS if p[0].lower() in files]
    for low, f in sorted(files.items()):
        if low.startswith("voice_") and low.endswith(".txt") and low not in known and not HIDDEN.search(f):
            found.append((f, f[6:-4].replace("_", " ").title(), "Custom preset"))
    return found


# @ audio: level meter and "hear myself"

class WaveFormat(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_ushort), ("channels", ctypes.c_ushort), ("rate", ctypes.c_uint),
                ("bytes_per_sec", ctypes.c_uint), ("align", ctypes.c_ushort), ("bits", ctypes.c_ushort),
                ("extra", ctypes.c_ushort)]


class WaveHdr(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("length", ctypes.c_uint), ("recorded", ctypes.c_uint),
                ("user", ctypes.c_void_p), ("flags", ctypes.c_uint), ("loops", ctypes.c_uint),
                ("next", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


class DevCaps(ctypes.Structure):
    # WAVEINCAPSW and WAVEOUTCAPSW share this layout up to the name, and the name is all I need
    _fields_ = [("mid", ctypes.c_ushort), ("pid", ctypes.c_ushort), ("version", ctypes.c_uint),
                ("name", ctypes.c_wchar * 32), ("formats", ctypes.c_uint), ("channels", ctypes.c_ushort),
                ("reserved", ctypes.c_ushort), ("support", ctypes.c_uint)]


class Audio:
    """Reads the finished voice back off CABLE Output, so the meter shows what people hear,
    and plays it to the headphones when asked. It never plays into a CABLE device: that
    would loop straight back into the mic."""

    RATE, BLOCK = 48000, 960            # 20 ms blocks, mono 16 bit
    IN_BUFS, OUT_BUFS = 4, 6
    DONE, INQUEUE = 0x1, 0x10

    def __init__(self):
        self.level_db = -90.0
        self.monitor = False            # UI flips this; the thread opens/closes the output
        self.error = None
        self._stop = threading.Event()

        # winmm handles are pointers. Without argtypes ctypes passes them as 32 bit ints.
        w, H, P = ctypes.windll.winmm, ctypes.c_void_p, ctypes.c_void_p
        w.waveInOpen.argtypes = w.waveOutOpen.argtypes = [ctypes.POINTER(H), ctypes.c_uint, P, P, P, ctypes.c_uint]
        for fn in ("waveInPrepareHeader", "waveInAddBuffer", "waveOutPrepareHeader", "waveOutWrite"):
            getattr(w, fn).argtypes = [H, P, ctypes.c_uint]
        for fn in ("waveInStart", "waveInReset", "waveInClose", "waveOutReset", "waveOutClose"):
            getattr(w, fn).argtypes = [H]
        self.w = w
        self.fmt = WaveFormat(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)

        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _find(self, kind, want, avoid=None):
        caps, fallback = DevCaps(), None
        for i in range(getattr(self.w, f"wave{kind}GetNumDevs")()):
            getattr(self.w, f"wave{kind}GetDevCapsW")(i, ctypes.byref(caps), ctypes.sizeof(caps))
            name = caps.name.lower()
            if want.lower() in name:
                return i
            if fallback is None and not (avoid and avoid.lower() in name):
                fallback = i
        return fallback

    def _buffers(self, n):
        bufs = [ctypes.create_string_buffer(self.BLOCK * 2) for _ in range(n)]
        return bufs, [WaveHdr(ctypes.cast(b, ctypes.c_void_p), self.BLOCK * 2) for b in bufs]

    def _open_output(self, size):
        hout = ctypes.c_void_p()
        dev = self._find("Out", "Speakers", avoid="CABLE")
        if dev is None or self.w.waveOutOpen(ctypes.byref(hout), dev, ctypes.byref(self.fmt), None, None, 0):
            return None, None, None
        bufs, hdrs = self._buffers(self.OUT_BUFS)
        for h in hdrs:
            self.w.waveOutPrepareHeader(hout, ctypes.byref(h), size)
        return hout, bufs, hdrs

    def _run(self):
        w, size = self.w, ctypes.sizeof(WaveHdr)
        k32 = ctypes.windll.kernel32
        k32.CreateEventW.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]

        ready = k32.CreateEventW(None, False, False, None)     # winmm sets this when a block fills
        hin = ctypes.c_void_p()
        dev = self._find("In", "CABLE Output")
        if dev is None or w.waveInOpen(ctypes.byref(hin), dev, ctypes.byref(self.fmt), ready, None, 0x50000):
            self.error = "CABLE Output not found"
            k32.CloseHandle(ready)
            return

        in_bufs, in_hdrs = self._buffers(self.IN_BUFS)
        for h in in_hdrs:
            w.waveInPrepareHeader(hin, ctypes.byref(h), size)
            w.waveInAddBuffer(hin, ctypes.byref(h), size)
        w.waveInStart(hin)

        hout = out_bufs = out_hdrs = None
        i = 0
        try:
            while not self._stop.is_set():
                h = in_hdrs[i]
                if not h.flags & self.DONE:
                    k32.WaitForSingleObject(ready, 100)
                    continue
                # first version just polled, which woke the thread 250 times a second:
                # if not h.flags & self.DONE:
                #     time.sleep(0.004)
                #     continue

                data = in_bufs[i].raw[:h.recorded]
                if data:
                    s = array.array("h", data)
                    rms = math.sqrt(math.sumprod(s, s) / len(s)) / 32768
                    self.level_db = 20 * math.log10(rms) if rms > 1e-5 else -90.0

                if self.monitor and hout is None:
                    hout, out_bufs, out_hdrs = self._open_output(size)
                    if hout is None:
                        self.monitor = False        # UI notices on its next tick
                elif not self.monitor and hout is not None:
                    w.waveOutReset(hout)
                    w.waveOutClose(hout)
                    hout = None

                if hout is not None and data:
                    # if every output buffer is still queued, skip this block rather than let latency pile up
                    free = next((j for j, oh in enumerate(out_hdrs) if not oh.flags & self.INQUEUE), None)
                    if free is not None:
                        ctypes.memmove(out_bufs[free], data, len(data))
                        out_hdrs[free].length = len(data)
                        w.waveOutWrite(hout, ctypes.byref(out_hdrs[free]), size)

                h.flags &= ~self.DONE
                w.waveInAddBuffer(hin, ctypes.byref(h), size)
                i = (i + 1) % self.IN_BUFS
        finally:
            if hout is not None:
                w.waveOutReset(hout)
                w.waveOutClose(hout)
            w.waveInReset(hin)
            w.waveInClose(hin)
            k32.CloseHandle(ready)

    def close(self):
        self._stop.set()
        self.thread.join(timeout=1)


# @ drawing
# The Tk canvas has no anti aliasing, so circles and rounded corners are drawn as
# tiny prerendered images. They get cached, so each one is only built once.

def level_text(db):
    return "0 dB" if abs(db) < 0.05 else f"{db:+.1f} dB"


def rgb(h):
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def coverage(edge, d):
    return min(1.0, max(0.0, edge - d + 0.5))


class Shapes:
    def __init__(self, root):
        self.root = root
        self.cache = {}

    def _image(self, key, size, pixel):
        if key not in self.cache:
            img = tk.PhotoImage(master=self.root, width=size, height=size)
            rows = ("{" + " ".join("#%02x%02x%02x" % pixel(x, y) for x in range(size)) + "}" for y in range(size))
            img.put(" ".join(rows))
            self.cache[key] = img
        return self.cache[key]

    def disc(self, radius, bg, layers):
        """Concentric circles, layers = [(radius, colour), ...] biggest first."""
        size = math.ceil(radius * 2)
        c = size / 2
        base, rings = rgb(bg), [(r, rgb(col)) for r, col in layers]

        def pixel(x, y):
            d = math.hypot(x + 0.5 - c, y + 0.5 - c)
            px = base
            for r, col in rings:
                if (cov := coverage(r, d)):
                    px = mix(px, col, cov)
            return px

        return self._image(("disc", radius, bg, tuple(layers)), size, pixel)

    def corner(self, r, fill, bg, quad, outline):
        # outside + 1px border + fill all in one image, so nothing sticks out past the curve
        outside, border, inside = rgb(bg), rgb(outline or fill), rgb(fill)
        cx = r if quad in ("tl", "bl") else 0
        cy = r if quad in ("tl", "tr") else 0

        def pixel(x, y):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            return mix(mix(outside, border, coverage(r, d)), inside, coverage(r - 1, d))

        return self._image(("corner", r, fill, bg, quad, outline), r, pixel)

    def rounded(self, canvas, x1, y1, x2, y2, r, fill, bg, tags=(), outline=None):
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        b = 1 if outline else 0
        if outline:
            canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=outline, width=0, tags=tags)
            canvas.create_rectangle(x1, y1 + r, x2, y2 - r, fill=outline, width=0, tags=tags)
        canvas.create_rectangle(x1 + r, y1 + b, x2 - r, y2 - b, fill=fill, width=0, tags=tags)
        canvas.create_rectangle(x1 + b, y1 + r, x2 - b, y2 - r, fill=fill, width=0, tags=tags)
        for quad, x, y in (("tl", x1, y1), ("tr", x2 - r, y1), ("bl", x1, y2 - r), ("br", x2 - r, y2 - r)):
            canvas.create_image(x, y, image=self.corner(r, fill, bg, quad, outline), anchor="nw", tags=tags)


# @ the window

class App(tk.Tk):
    PAD, CARD_H, GAP, RADIUS = 20, 60, 4, 6
    SLIDER_Y = 52
    DRAG_THRESHOLD = 4
    VIZ_BARS, VIZ_W, VIZ_GAP, VIZ_H = 14, 3, 2, 18

    def __init__(self):
        super().__init__()
        self.title("Mic Presets")
        self.configure(bg=BG)
        self.accent = windows_accent()
        self.shapes = Shapes(self)

        families = set(tkfont.families(self))
        display = "Segoe UI Variable Display" if "Segoe UI Variable Display" in families else "Segoe UI"
        body = "Segoe UI Variable Text" if "Segoe UI Variable Text" in families else "Segoe UI"
        self.f_title = tkfont.Font(family=display, size=20, weight="bold")
        self.f_name = tkfont.Font(family=body, size=11)
        self.f_body = tkfont.Font(family=body, size=9)
        self.f_value = tkfont.Font(family=body, size=10)

        self.presets = available_presets()
        self.active, self.level = parse_config(read_file(CONFIG_FILE))
        self.hover = None
        self.press = None

        list_h = len(self.presets) * (self.CARD_H + self.GAP)
        self.geometry(f"460x{min(840, 90 + list_h + 168)}")
        self.minsize(380, 420)

        self.header = tk.Canvas(self, height=76, bg=BG, highlightthickness=0)
        self.header.pack(fill="x", padx=self.PAD, pady=(14, 0))
        self.list = tk.Canvas(self, bg=BG, highlightthickness=0, yscrollincrement=1)
        self.list.pack(fill="both", expand=True, padx=self.PAD)
        self.controls = tk.Canvas(self, height=132, bg=BG, highlightthickness=0)
        self.controls.pack(fill="x", padx=self.PAD, pady=(12, self.PAD))

        self.header.bind("<Configure>", lambda e: self.draw_header())
        self.list.bind("<Configure>", lambda e: self.draw_list())
        self.controls.bind("<Configure>", lambda e: self.draw_controls())
        self.list.bind("<Motion>", lambda e: self.set_hover(self.index_at(e)))
        self.list.bind("<Leave>", lambda e: self.set_hover(None))
        self.list.bind("<MouseWheel>", lambda e: self.list.yview_scroll(-e.delta // 4, "units"))

        # press and move = drag the window, press without moving = pick the preset
        for canvas in (self.header, self.list):
            canvas.bind("<ButtonPress-1>", self.on_press)
            canvas.bind("<B1-Motion>", self.on_drag)
            canvas.bind("<ButtonRelease-1>", self.on_release)

        # tag bindings survive delete("all"), so these only need doing once
        c = self.controls
        c.tag_bind("slider", "<Button-1>", self.on_slide)
        c.tag_bind("slider", "<B1-Motion>", self.on_slide)
        c.tag_bind("slider", "<ButtonRelease-1>", self.on_slide_end)
        c.tag_bind("slider", "<Double-Button-1>", lambda e: self.commit_level(0.0))
        for tag, action in (("btn_refresh", self.refresh),
                            ("btn_folder", lambda: os.startfile(APO_CONFIG)),
                            ("monitor", self.toggle_monitor)):
            c.tag_bind(tag, "<Button-1>", lambda e, a=action: a())
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))

        self.audio = Audio()
        self.monitor_drawn = False
        self.viz_ids = []
        self.hist = collections.deque([0.0] * self.VIZ_BARS, maxlen=self.VIZ_BARS)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.tick)

        self.update_idletasks()
        style_window(self)

    def on_close(self):
        self.audio.close()
        self.destroy()

    # live meter, ~30 fps. Only moves bars that already exist.
    def tick(self):
        if self.state() == "iconic":
            self.after(250, self.tick)
            return
        if self.audio.monitor != self.monitor_drawn:    # e.g. headphones failed to open
            self.draw_monitor()

        self.hist.append(min(1.0, max(0.0, (self.audio.level_db + 55) / 43)))   # quiet (55 dBFS down) maps to 0, loud (12 dBFS down) to 1
        for k, (item, v) in enumerate(zip(self.viz_ids, self.hist)):
            x = self.viz_x + k * (self.VIZ_W + self.VIZ_GAP)
            h = 1 + v * self.VIZ_H / 2
            self.list.coords(item, x, self.viz_cy - h, x + self.VIZ_W, self.viz_cy + h)
        self.after(33, self.tick)

    def draw_header(self, note=None):
        c = self.header
        c.delete("all")
        c.create_text(0, 24, text="Mic Presets", font=self.f_title, fill=TEXT, anchor="w")
        name = next((n for f, n, _ in self.presets if f == self.active), "none")
        c.create_text(1, 58, text=note or f"Using {name}", font=self.f_body, fill=MUTED, anchor="w")

    # @ preset list

    def card_top(self, i):
        return i * (self.CARD_H + self.GAP)

    def draw_list(self):
        self.list.delete("all")
        for i in range(len(self.presets)):
            self.draw_card(i)
        bottom = max(1, self.card_top(len(self.presets)) - self.GAP)
        self.list.configure(scrollregion=(0, 0, self.list.winfo_width(), bottom))

    def draw_card(self, i):
        c, tag = self.list, f"card{i}"
        c.delete(tag)
        f, name, desc = self.presets[i]
        w, y = c.winfo_width(), self.card_top(i)
        mid = y + self.CARD_H // 2
        active = f == self.active
        fill = CARD_HOVER if i == self.hover else CARD

        self.shapes.rounded(c, 0, y, w, y + self.CARD_H, self.RADIUS, fill, BG, tag, outline=STROKE)
        if active:
            self.shapes.rounded(c, 1, y + 18, 4, y + self.CARD_H - 18, 1, self.accent, fill, tag)
        rings = [(10, self.accent), (4.5, "#000000")] if active else [(10, "#8a8a8a"), (9, fill)]
        c.create_image(20, mid, image=self.shapes.disc(10, fill, rings), anchor="w", tags=tag)
        c.create_text(52, y + 21, text=name, font=self.f_name, fill=TEXT, anchor="w", tags=tag)
        c.create_text(52, y + 41, text=desc, font=self.f_body, fill=MUTED, anchor="w", tags=tag)

        if active:
            span = self.VIZ_BARS * (self.VIZ_W + self.VIZ_GAP) - self.VIZ_GAP
            self.viz_x, self.viz_cy = w - 16 - span, mid
            self.viz_ids = [c.create_rectangle(0, 0, 0, 0, fill=self.accent, width=0, tags=tag)
                            for _ in range(self.VIZ_BARS)]

    def index_at(self, event):
        y = self.list.canvasy(event.y)
        i = int(y // (self.CARD_H + self.GAP))
        return i if 0 <= i < len(self.presets) and y - self.card_top(i) <= self.CARD_H else None

    def redraw(self, *cards):
        for i in cards:
            if i is not None:
                self.draw_card(i)

    def set_hover(self, i):
        if i != self.hover:
            old, self.hover = self.hover, i
            self.redraw(old, i)
            self.list.configure(cursor="hand2" if i is not None else "")

    def on_press(self, e):
        self.press = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y(), False)

    def on_drag(self, e):
        if not self.press:
            return
        x0, y0, wx, wy, moving = self.press
        dx, dy = e.x_root - x0, e.y_root - y0
        if moving or abs(dx) > self.DRAG_THRESHOLD or abs(dy) > self.DRAG_THRESHOLD:
            self.press = (x0, y0, wx, wy, True)
            self.geometry(f"+{wx + dx}+{wy + dy}")

    def on_release(self, e):
        moved = self.press and self.press[4]
        self.press = None
        if not moved and e.widget is self.list:
            self.select(self.index_at(e))

    def select(self, i):
        if i is None or self.presets[i][0] == self.active:
            return
        old = next((j for j, p in enumerate(self.presets) if p[0] == self.active), None)
        self.active = self.presets[i][0]
        self.redraw(old, i)
        self.update_idletasks()     # paint the new selection first so the click feels instant
        if self.save():
            hint = "" if self.active in NATURAL else ", turn off noise suppression in Discord"
            self.draw_header(f"Using {self.presets[i][1]}{hint}")

    # @ bottom panel: level slider, buttons, monitor switch

    def slider_x(self):
        return 16, self.controls.winfo_width() - 16

    def draw_controls(self):
        c = self.controls
        c.delete("all")
        w = c.winfo_width()
        self.shapes.rounded(c, 0, 0, w, 76, self.RADIUS, CARD, BG, outline=STROKE)
        c.create_text(16, 22, text="Mic level", font=self.f_name, fill=TEXT, anchor="w")
        c.create_text(w - 16, 22, text=level_text(self.level), font=self.f_value, fill=MUTED, anchor="e", tags="value")
        self.draw_slider()

        for x, label, tag in ((0, "Refresh", "btn_refresh"), (96, "Open folder", "btn_folder")):
            self.shapes.rounded(c, x, 92, x + 88, 124, 4, CARD, BG, tag, outline=STROKE)
            c.create_text(x + 44, 108, text=label, font=self.f_body, fill=TEXT, tags=tag)
        self.draw_monitor()

    def draw_monitor(self):
        c = self.controls
        c.delete("monitor")
        on = self.monitor_drawn = self.audio.monitor
        x2 = c.winfo_width()
        x1 = x2 - 40
        if on:
            self.shapes.rounded(c, x1, 98, x2, 118, 10, self.accent, BG, "monitor")
        else:
            self.shapes.rounded(c, x1, 98, x2, 118, 10, CARD, BG, "monitor", outline="#8a8a8a")
        knob = self.shapes.disc(6, self.accent if on else CARD, [(6, "#000000" if on else "#cfcfcf")])
        c.create_image(x2 - 10 if on else x1 + 10, 108, image=knob, tags="monitor")
        c.create_text(x1 - 10, 108, text="Hear myself", font=self.f_body, fill=TEXT, anchor="e", tags="monitor")

    def toggle_monitor(self):
        self.audio.monitor = not self.audio.monitor
        self.draw_monitor()
        if self.audio.error:
            self.draw_header(self.audio.error)
        elif self.audio.monitor:
            self.draw_header("Hearing your processed voice. A small delay is normal")
        else:
            self.draw_header()

    def level_to_x(self, level):
        x1, x2 = self.slider_x()
        return x1 + (level - LEVEL_MIN) / (LEVEL_MAX - LEVEL_MIN) * (x2 - x1)

    def draw_slider(self):
        c, y = self.controls, self.SLIDER_Y
        c.delete("slider")
        x1, x2 = self.slider_x()
        zero = self.level_to_x(0)
        c.create_rectangle(x1 - 10, y - 12, x2 + 10, y + 12, fill=CARD, width=0, tags="slider")   # bigger hit area
        c.create_rectangle(x1, y - 2, x2, y + 2, fill=TRACK, width=0, tags="slider")
        c.create_rectangle(zero, y - 2, zero, y + 2, fill=self.accent, width=0, tags=("slider", "fill"))
        c.create_rectangle(zero, y - 6, zero + 1, y + 6, fill=MUTED, width=0, tags="slider")
        knob = self.shapes.disc(10, CARD, [(10, "#454545"), (9, "#454545"), (6, self.accent)])
        c.create_image(zero, y, image=knob, tags=("slider", "knob"))
        self.move_slider()

    def move_slider(self):
        # only move the existing pieces; rebuilding the slider on every mouse move made it stutter
        c, y = self.controls, self.SLIDER_Y
        pos, zero = self.level_to_x(self.level), self.level_to_x(0)
        c.coords("fill", min(zero, pos), y - 2, max(zero, pos), y + 2)
        c.coords("knob", pos, y)
        c.itemconfigure("value", text=level_text(self.level))

    def on_slide(self, e):
        x1, x2 = self.slider_x()
        frac = min(1.0, max(0.0, (e.x - x1) / (x2 - x1)))
        self.level = LEVEL_MIN + frac * (LEVEL_MAX - LEVEL_MIN)
        self.move_slider()

    def on_slide_end(self, e):
        self.on_slide(e)
        self.commit_level(round(self.level * 2) / 2)

    def commit_level(self, level):
        self.level = 0.0 if abs(level) < 0.5 else level
        self.move_slider()
        if self.save():
            self.draw_header(f"Mic level {level_text(self.level)}" if self.level else "Mic level reset")

    def save(self):
        try:
            write_config(self.active, self.presets, self.level)
            return True
        except OSError as err:
            messagebox.showerror("Could not save", f"{err}\n\nRun the switcher as administrator.")
            self.refresh()
            return False

    def refresh(self):
        self.presets = available_presets()
        self.active, self.level = parse_config(read_file(CONFIG_FILE))
        self.draw_header()
        self.draw_list()
        self.draw_controls()


def main():
    if not os.path.isdir(APO_CONFIG):
        messagebox.showerror("Equalizer APO not found", f"Missing folder:\n{APO_CONFIG}")
        return
    if not is_admin():
        relaunch_as_admin()
        return
    try:
        # sharp text. Layout is plain pixels tuned at 100% scaling;
        # anything above that needs the sizes multiplied by dpi / 96
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    App().mainloop()


def selftest():
    presets = PRESETS + [("voice_my voice.txt", "My Voice", "Custom preset")]
    for active, level in (("voice_demon.txt", 0.0), ("voice_my voice.txt", -4.5), ("voice_baby.txt", 6.0)):
        assert parse_config(render_config(active, presets, level)) == (active, level), (active, level)
    assert parse_config("# Include: voice_a.txt\nInclude: voice_b.txt\n") == ("voice_b.txt", 0.0)
    assert level_text(0.02) == "0 dB" and level_text(-4.5) == "-4.5 dB"
    print("selftest ok")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
