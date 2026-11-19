#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guns.lol Username Checker
github.com/efekrbas/guns.lol-username-checker

A high-accuracy CLI tool that finds unclaimed guns.lol usernames using a
headless Chrome (Selenium) engine.

    python gunslol.py
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import json
import math
import os
import platform
import random
import re
import select
import signal
import socket
import string
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from queue import Queue
from urllib.parse import quote

__version__ = "2.3.0"


# =============================================================================
#  Windows launch detection (stdlib only, so it works before deps are loaded)
# =============================================================================

_PYTHON_LAUNCHERS = frozenset({"py.exe", "pyw.exe", "python.exe", "pythonw.exe", "python3.exe"})


def _windows_process_name(kernel32, pid):
    """Returns the lower-cased executable name of a process ('' if unknown)."""
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return os.path.basename(buffer.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def is_double_clicked():
    """Check if script was launched by double-clicking (Windows only).

    A console opened by Explorer only hosts Python itself - plus the py.exe
    launcher or a venv redirector, depending on the file association. When the
    script is started from cmd / PowerShell / Windows Terminal, the shell is
    attached to the console as well.
    """
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        kernel32.GetConsoleProcessList.restype = wintypes.DWORD
        process_list = (wintypes.DWORD * 64)()
        count = kernel32.GetConsoleProcessList(process_list, 64)
        if count == 0 or count > 64:
            return False
        if count == 1:
            return True
        own_pid = os.getpid()
        others = [process_list[i] for i in range(count) if process_list[i] != own_pid]
        return all(_windows_process_name(kernel32, pid) in _PYTHON_LAUNCHERS for pid in others)
    except Exception:
        return False


_launched_by_double_click = is_double_clicked()


def _pause_if_double_clicked():
    """Keeps the console window open when the script was started by double-click."""
    if not _launched_by_double_click:
        return
    try:
        print("\nPress any key to exit...")
        os.system("pause >nul")
    except BaseException:
        pass


try:
    import requests
    from colorama import Fore, init as colorama_init
    from pystyle import Colorate, Cursor, System
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _missing:
    print("Missing dependency: {}".format(getattr(_missing, "name", None) or _missing))
    print("Install the requirements first:  pip install -r requirements.txt")
    print("(or just run install.bat on Windows / ./install.sh on Linux & macOS)")
    _pause_if_double_clicked()
    sys.exit(1)


# =============================================================================
#  Configuration
# =============================================================================

DISPLAY_PREFIX = "guns.lol/"
PROFILE_URL = "https://guns.lol/{}"
REPO_URL = "github.com/efekrbas/guns.lol-username-checker"

BASE_CHARS = string.ascii_lowercase + string.digits
PREMIUM_CHARS = "._-"
ALL_CHARS = BASE_CHARS + PREMIUM_CHARS
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]+$")
MAX_USERNAME_LENGTH = 32

# Files live next to the script, so it also works when started from another folder.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CUSTOMLIST_FILE = os.path.join(BASE_DIR, "customlist.txt")
PROXIES_FILE = os.path.join(BASE_DIR, "proxies.txt")
UNCLAIMED_FILE = os.path.join(BASE_DIR, "unclaimed.txt")
SITES_FILE = os.path.join(BASE_DIR, "sites.json")
WEBHOOKS_FILE = os.path.join(BASE_DIR, "webhooks.txt")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

import generator as listgen
import speed as fastpath

MAX_RETRIES = 3              # page loads per username before reporting error/timeout
PAGE_LOAD_TIMEOUT = 14       # seconds
SCRIPT_TIMEOUT = 4           # seconds
CHALLENGE_WAIT = 8.0         # seconds to wait for the guns.lol / Cloudflare challenge
POLL_INTERVAL = 0.3          # seconds between page checks
MAX_BROWSER_RESTARTS = 5     # consecutive Chrome crashes tolerated

# Phrases that mean "this username is free" (checked in visible text, case-folded).
# Body text is intentionally NOT scanned for every site: profile pages embed Discord
# widgets that contain the words "Not Found" (haunt.gg does), which would be a false hit.
GENERIC_NOT_FOUND_HEADINGS = ("username not found", "user not found", "profile not found",
                              "page not found", "couldn't find", "could not find",
                              "doesn't exist", "does not exist", "no longer available",
                              "not found")
GENERIC_NOT_FOUND_TITLE = ("not found", "404", "page not found", "profile not found",
                           "doesn't exist", "does not exist", "no longer available",
                           "user unavailable")
CHALLENGE_MARKERS = ("please wait", "just a moment", "checking your browser",
                     "attention required", "verify you are human", "blocked",
                     "access denied", "enable javascript")
RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "error 1015")
CHALLENGE_STATUSES = (401, 403, 429, 503)
NOT_FOUND_STATUSES = (404, 410)

WEBHOOK_PATTERN = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/"
    r"\d+/[A-Za-z0-9_-]+/?(?:\?\S*)?$"
)


# =============================================================================
#  Theme
# =============================================================================

RESET = "\033[0m"


def _rgb(r, g, b):
    return "\033[38;2;{};{};{}m".format(r, g, b)


PURPLE = _rgb(150, 0, 255)
LIGHT_PURPLE = _rgb(195, 125, 255)
MUTED = _rgb(130, 115, 155)

purple_static = ["150;0;255"] * 24  # the signature v1 purple


def _gradient(start, end, steps):
    """Returns a list of 'r;g;b' strings blending start -> end."""
    colors = []
    for i in range(steps):
        t = i / max(steps - 1, 1)
        colors.append(";".join(str(round(s + (e - s) * t)) for s, e in zip(start, end)))
    return colors


def _scale(color, factor):
    return ";".join(str(round(int(c) * factor)) for c in color.split(";"))


# CLI colour themes (Settings ▸ Colour theme). Accent is the site-agnostic brand colour.
THEMES = {
    # Deeper, richer palettes: a deep accent with a quieter secondary tone.
    "purple": {"label": "Royal Purple", "accent": (92, 0, 168), "light": (146, 86, 208),
               "muted": (108, 96, 124)},
    "ocean": {"label": "Deep Ocean", "accent": (0, 74, 140), "light": (62, 132, 190),
              "muted": (90, 108, 128)},
    "mint": {"label": "Forest Mint", "accent": (0, 108, 82), "light": (58, 160, 128),
             "muted": (86, 116, 104)},
    "sunset": {"label": "Burnt Ember", "accent": (152, 62, 14), "light": (198, 116, 58),
               "muted": (122, 96, 80)},
    "rose": {"label": "Wine Rose", "accent": (142, 22, 76), "light": (188, 82, 128),
             "muted": (118, 88, 102)},
    "mono": {"label": "Charcoal", "accent": (112, 112, 118), "light": (168, 168, 176),
             "muted": (104, 104, 110)},
}

_CURRENT_THEME = "purple"
_HEADER_TOP = "146;86;208"   # top colour of the current header gradient


# Defaults for the "purple" theme; apply_theme() rebinds all of them at runtime.
PURPLE = _rgb(*THEMES["purple"]["accent"])
LIGHT_PURPLE = _rgb(*THEMES["purple"]["light"])
MUTED = _rgb(*THEMES["purple"]["muted"])
purple_static = ["{};{};{}".format(*THEMES["purple"]["accent"])] * 24
HEADER_GRADIENT = _gradient(THEMES["purple"]["accent"], THEMES["purple"]["light"], 9)
_INTRO_WAVE = (_gradient(tuple(int(c * 0.62) for c in THEMES["purple"]["accent"]),
                         THEMES["purple"]["light"], 12)
               + _gradient(THEMES["purple"]["light"],
                           tuple(int(c * 0.62) for c in THEMES["purple"]["accent"]), 12))


def _theme_palette(name="purple"):
    """Builds every colour constant used by the UI from one accent colour."""
    theme = THEMES.get(name, THEMES["purple"])
    accent, light, muted = theme["accent"], theme["light"], theme["muted"]
    palette = {
        "PURPLE": _rgb(*accent),
        "LIGHT_PURPLE": _rgb(*light),
        "MUTED": _rgb(*muted),
        "purple_static": ["{};{};{}".format(*accent)] * 24,
        "HEADER_GRADIENT": _gradient(accent, light, 9),
        "_HEADER_TOP": "{};{};{}".format(*light),
        "_INTRO_WAVE": _gradient(tuple(int(c * 0.62) for c in accent), light, 12)
                       + _gradient(light, tuple(int(c * 0.62) for c in accent), 12),
    }
    return palette


def apply_theme(name):
    """Applies a colour theme globally. Unknown names keep the current one."""
    global _CURRENT_THEME
    if name not in THEMES:
        return False
    _CURRENT_THEME = name
    globals().update(_theme_palette(name))
    return True


def current_theme():
    return _CURRENT_THEME


apply_theme("purple")

intro_text = r'''
 ____ ___ ___  ____   ____ ___  _   _ _____
| __ )_ _/ _ \/ ___| / ___/ _ \| | | |_   _|
|  _ \| | | | \___ \| |  | | | | | | | | |
| |_) | | |_| |___) | |__| |_| | |_| | | |
|____/___\___/|____/ \____\___/ \___/  |_|
'''

_ART_LINES = [line.rstrip() for line in intro_text.strip("\n").splitlines()]
_ART_WIDTH = max(len(line) for line in _ART_LINES)


class UI:
    """Persistent header state: answered settings + notes survive screen refreshes."""

    settings = []  # [label, value]
    notes = []     # (kind, message)

    @classmethod
    def reset(cls):
        cls.settings = []
        cls.notes = []

    @classmethod
    def setting(cls, label, value):
        for item in cls.settings:
            if item[0] == label:
                item[1] = value
                return
        cls.settings.append([label, value])

    @classmethod
    def note(cls, kind, message):
        if (kind, message) not in cls.notes:  # don't repeat notes while looping through menus
            cls.notes.append((kind, message))


_NOTE_STYLE = {
    "ok": (Fore.GREEN, "+"),
    "info": (LIGHT_PURPLE, "*"),
    "warn": (Fore.YELLOW, "!"),
    "error": (Fore.RED, "x"),
}


def _is_tty(stream):
    try:
        return stream.isatty()
    except Exception:
        return False


def terminal_size():
    try:
        size = os.get_terminal_size(sys.__stdout__.fileno())
        return size.columns, size.lines
    except (AttributeError, OSError, ValueError):
        return 120, 30


def clear_screen():
    if not _is_tty(sys.stdout):
        return
    if os.name == "nt":
        System.Clear()
    else:
        sys.stdout.write("\033[H\033[2J\033[3J")
        sys.stdout.flush()


def set_console_title(text):
    if os.name == "nt":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(text)
        except Exception:
            pass
    elif _is_tty(sys.stdout):
        sys.stdout.write("\033]0;{}\007".format(text))
        sys.stdout.flush()


def build_banner(width, with_prompt=False):
    """Centered ASCII banner; falls back to a compact one on narrow terminals."""
    if width >= _ART_WIDTH + 2:
        lines = list(_ART_LINES) + ["", REPO_URL.center(_ART_WIDTH)]
        block = _ART_WIDTH
    else:
        title = "guns.lol username checker  v" + __version__
        block = max(len(title), len(REPO_URL))
        lines = [title.center(block), REPO_URL.center(block)]
    if with_prompt:
        lines += ["", "> Press Enter".center(block)]
    pad = " " * max((width - block) // 2, 0)
    return "\n".join((pad + line).rstrip() for line in lines)


def show_header():
    """Clears the screen and prints the themed header plus the persistent settings."""
    clear_screen()
    width, _ = terminal_size()
    print()
    print(Colorate.Vertical(HEADER_GRADIENT, build_banner(width)))
    print()
    if UI.settings:
        for label, value in UI.settings:
            print("   {}{}{} {}{}{}".format(MUTED, (label + " ").ljust(17, "."), RESET,
                                           LIGHT_PURPLE, value, RESET))
        print()
    if UI.notes:
        for kind, message in UI.notes:
            color, symbol = _NOTE_STYLE.get(kind, _NOTE_STYLE["info"])
            print("  {}[{}]{} {}".format(color, symbol, RESET, message))
        print()


def _enter_pressed(timeout):
    """Non-blocking check for the Enter key (no helper thread that could hang on exit)."""
    if os.name == "nt":
        import msvcrt

        end = time.monotonic() + timeout
        while True:
            while msvcrt.kbhit():
                if msvcrt.getwch() in ("\r", "\n"):
                    return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
    try:
        readable, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError, TypeError):
        time.sleep(min(timeout, 0.05))  # stdin without a fileno (pipes, IDEs)
        return False
    if readable:
        sys.stdin.readline()
        return True
    return False


# ------------------------------------------------------------- intro animations

def _ansi(triple):
    """'92;0;168' (raw rgb) -> an ANSI truecolour foreground escape."""
    return "\033[38;2;{}m".format(triple)


def _char_palettes(count, frame, fade=1.0):
    """Rotating gradient palette: one colour triple per art line."""
    shift = (frame // 2) % len(_INTRO_WAVE)
    wave = _INTRO_WAVE[shift:] + _INTRO_WAVE[:shift]
    while len(wave) < count:
        wave += wave
    return [_scale(wave[index % len(wave)], fade) for index in range(count)]


def _rainbow(count, frame, fade=1.0):
    """Hue sweep: every line gets its own deep colour from the colour wheel."""
    colors = []
    for index in range(count):
        hue = ((index / max(count, 1)) + frame / 40.0) % 1.0
        red, green, blue = _hsv_to_rgb(hue, 0.78, 0.82)
        colors.append(_scale("{};{};{}".format(red, green, blue), fade))
    return colors


def _hsv_to_rgb(hue, saturation, value):
    import colorsys
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, saturation, value)
    return round(red * 255), round(green * 255), round(blue * 255)


def _centered_block(art_lines, width):
    widest = max((len(line) for line in art_lines), default=0)
    pad = " " * max((width - widest) // 2, 0)
    return [(pad + line).rstrip() for line in art_lines]


def _top_pad(height, line_count):
    return max((height - line_count - 3) // 2, 0)


def _render_frame(lines, colors, top, footer, height, width):
    """Draws one complete frame: fixed height, padded, cleared to end of screen.

    Previously raw 'r;g;b' triples were printed instead of ANSI escapes and old
    shorter frames were never cleared - this is the fix for that bug.
    """
    frame = [""] * max(top, 0)
    for index, line in enumerate(lines):
        color = colors[index % len(colors)] if colors else None
        frame.append((_ansi(color) + line + RESET) if color else line)
    footer_line = MUTED + footer + RESET if footer else ""
    frame.extend(["", footer_line] if footer else [""])
    while len(frame) < height - 1:
        frame.append("")
    sys.stdout.write("\033[H" + "\n".join(frame[:max(height - 1, 1)]) + "\n\033[J")
    sys.stdout.flush()


def _run_animation(painter, art, width, height, wait, fps=0.035):
    """Shared loop: painters draw frames until `wait` says stop."""
    top = _top_pad(height, len(art))
    footer = REPO_URL.center(width)
    frame = 0
    started = time.monotonic()
    while True:
        painter(art, width, height, top, footer, frame, fps)
        frame += 1
        if wait(fps):
            return
        if time.monotonic() - started > 120:  # never hang the terminal forever
            return


def _anim_fade(art, width, height, top, footer, frame, fps):
    fade = min(1.0, 0.10 + frame / 18.0)
    _render_frame(art, _char_palettes(len(art), frame, fade), top, footer, height, width)


def _anim_wave(art, width, height, top, footer, frame, fps):
    _render_frame(art, _char_palettes(len(art), frame), top, footer, height, width)


def _anim_rainbow(art, width, height, top, footer, frame, fps):
    _render_frame(art, _rainbow(len(art), frame), top, footer, height, width)


def _anim_pulse(art, width, height, top, footer, frame, fps):
    import math
    fade = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(frame / 9.0))
    _render_frame(art, _char_palettes(len(art), frame, fade), top, footer, height, width)


def _anim_typewriter(art, width, height, top, footer, frame, fps):
    flat = "\n".join(art)
    total = len(flat)
    revealed = min(3 + frame * 3, total)
    partial = flat[:revealed].split("\n")
    _render_frame(partial, _char_palettes(len(partial), frame), top,
                  footer if revealed >= total else "", height, width)


def _anim_slide(art, width, height, top, footer, frame, fps):
    steps = max(width, 20)
    offset = max(steps - frame * 2, 0)
    pad = " " * max(width - offset, 0) if offset else " " * 0
    moved = [(pad + line).rstrip() if offset else line for line in art]
    _render_frame(moved, _char_palettes(len(art), frame), top,
                  footer if offset == 0 else "", height, width)


_DECODE_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789._-"


def _anim_decode(art, width, height, top, footer, frame, fps):
    import random as _random
    flat = "\n".join(art)
    revealed = min(4 + frame * 4, len(flat))
    chars = []
    for index, char in enumerate(flat):
        if char == "\n":
            chars.append("\n")
        elif index < revealed:
            chars.append(char)
        else:
            chars.append(_random.choice(_DECODE_CHARS))
    _render_frame("".join(chars).split("\n"), _char_palettes(len(art), frame), top,
                  footer if revealed >= len(flat) else "", height, width)


INTRO_ANIMATIONS = OrderedDict([
    ("fade", {"label": "Fade in", "painter": _anim_fade}),
    ("wave", {"label": "Gradient wave", "painter": _anim_wave}),
    ("rainbow", {"label": "Rainbow sweep", "painter": _anim_rainbow}),
    ("pulse", {"label": "Pulse / breathing", "painter": _anim_pulse}),
    ("typewriter", {"label": "Typewriter", "painter": _anim_typewriter}),
    ("slide", {"label": "Slide in", "painter": _anim_slide}),
    ("decode", {"label": "Matrix decode", "painter": _anim_decode}),
    ("none", {"label": "No animation (instant)", "painter": None}),
])


def play_intro(animation="fade"):
    """Runs the selected intro animation. Enter skips it; the cursor is restored."""
    if not (_is_tty(sys.stdout) and _is_tty(sys.stdin)):
        return
    entry = INTRO_ANIMATIONS.get(str(animation or "fade").lower()) or INTRO_ANIMATIONS["fade"]
    width, height = terminal_size()
    width, height = max(width, 40), max(height, 14)
    lines = _centered_block(_ART_LINES, width)
    Cursor.HideCursor()
    try:
        clear_screen()
        if entry["painter"] is None:
            _render_frame(lines, [_HEADER_TOP], _top_pad(height, len(lines)),
                          REPO_URL.center(width), height, width)
            _enter_pressed(1.2)
            return
        _run_animation(entry["painter"], lines, width, height, _enter_pressed)
    finally:
        Cursor.ShowCursor()
        sys.stdout.write("\033[J")
        sys.stdout.flush()


def preview_animation(animation, seconds=1.8):
    """Short, time-boxed preview used by the settings menu (no key press needed)."""
    if not (_is_tty(sys.stdout) and _is_tty(sys.stdin)):
        return
    entry = INTRO_ANIMATIONS.get(animation) or INTRO_ANIMATIONS["fade"]
    width, height = terminal_size()
    width, height = max(width, 40), max(height, 14)
    lines = _centered_block(_ART_LINES, width)
    deadline = time.monotonic() + seconds
    Cursor.HideCursor()
    try:
        clear_screen()
        if entry["painter"] is None:
            _render_frame(lines, [_HEADER_TOP], _top_pad(height, len(lines)),
                          REPO_URL.center(width), height, width)
            time.sleep(0.6)
            return
        _run_animation(entry["painter"], lines, width, height,
                       lambda timeout: time.monotonic() >= deadline)
    finally:
        Cursor.ShowCursor()
        sys.stdout.write("\033[J")
        sys.stdout.flush()


# ------------------------------------------------------------- saved settings

DEFAULT_SETTINGS = {
    "intro": "fade",
    "speed": "adaptive",
    "use_http": True,
    "theme": "purple",
    "sites": ["guns.lol"],
    "delay": 0,
    "letter_count": 4,
    "filter_premium": True,
    "save_to_file": True,
    "use_proxies": False,
    "use_customlist": False,
}


def load_settings(path=SETTINGS_FILE):
    """Loads settings.json, merged over the defaults (invalid values fall back)."""
    settings = dict(DEFAULT_SETTINGS)
    if not os.path.exists(path):
        return settings
    try:
        raw = "\n".join(read_text_lines(path))
        raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
        stored = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return settings
    if not isinstance(stored, dict):
        return settings
    for key, value in stored.items():
        if key in DEFAULT_SETTINGS and value is not None:
            settings[key] = value
    if settings["intro"] not in INTRO_ANIMATIONS:
        settings["intro"] = DEFAULT_SETTINGS["intro"]
    if settings["theme"] not in THEMES:
        settings["theme"] = DEFAULT_SETTINGS["theme"]
    if settings["speed"] not in fastpath.PROFILES:
        settings["speed"] = DEFAULT_SETTINGS["speed"]
    try:
        settings["delay"] = float(settings["delay"])
    except (TypeError, ValueError):
        settings["delay"] = DEFAULT_SETTINGS["delay"]
    if not math.isfinite(settings["delay"]) or settings["delay"] < 0 or settings["delay"] > 3600:
        settings["delay"] = DEFAULT_SETTINGS["delay"]
    try:
        settings["letter_count"] = int(settings["letter_count"])
    except (TypeError, ValueError):
        settings["letter_count"] = DEFAULT_SETTINGS["letter_count"]
    if not 1 <= settings["letter_count"] <= MAX_USERNAME_LENGTH:
        settings["letter_count"] = DEFAULT_SETTINGS["letter_count"]
    if not isinstance(settings["sites"], list):
        settings["sites"] = DEFAULT_SETTINGS["sites"]
    settings["sites"] = [str(name) for name in settings["sites"] if str(name).strip()]
    return settings


def save_settings(settings, path=SETTINGS_FILE):
    """Writes settings.json atomically. Returns True on success."""
    payload = {key: settings.get(key, DEFAULT_SETTINGS[key]) for key in DEFAULT_SETTINGS}
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def print_info(message):
    print("  {}[*]{} {}".format(LIGHT_PURPLE, RESET, message))


def print_ok(message):
    print("  {}[+]{} {}".format(Fore.GREEN, RESET, message))


def print_warn(message):
    print("  {}[!]{} {}".format(Fore.YELLOW, RESET, message))


def print_error(message):
    print("  {}[x]{} {}".format(Fore.RED, RESET, message))


_STATUS_STYLE = {
    "unclaimed": (Fore.GREEN, "+", "unclaimed"),
    "claimed": (Fore.RED, "-", "claimed"),
    "error": (Fore.YELLOW, "!", "error/timeout"),
    "skipped": (Fore.YELLOW, "~", "skipped"),
}


def print_site_row(index, username, results, note=""):
    """One row per username: every selected site with its own colour-coded verdict."""
    pieces = []
    for site, status in results:
        color, symbol, label = _STATUS_STYLE[status]
        name = site.display(username) if site is not None else DISPLAY_PREFIX + username
        pieces.append("{}{}{} {}[{}] {}{}".format(Fore.MAGENTA, name, RESET, color, symbol, label, RESET))
    line = "  {}{}{}  {}#{:04d}{}  {}".format(
        MUTED, datetime.now().strftime("%H:%M:%S"), RESET, PURPLE, index, RESET,
        "  {}·{}  ".format(MUTED, RESET).join(pieces))
    if note:
        line += "  {}{}{}".format(MUTED, note, RESET)
    print(line)


# =============================================================================
#  Usernames
# =============================================================================

def random_letters(n, filter_premium=False):
    """Generates a random string consisting of letters, digits and special characters."""
    if filter_premium:
        if n == 1:
            return random.choice(BASE_CHARS)
        first = random.choice(BASE_CHARS)
        middle = "".join(random.choice(ALL_CHARS) for _ in range(n - 2))
        last = random.choice(BASE_CHARS)
        return first + middle + last
    return "".join(random.choice(ALL_CHARS) for _ in range(n))


def count_combinations(n, filter_premium=False):
    """Number of distinct usernames random_letters() can produce."""
    if not filter_premium:
        return len(ALL_CHARS) ** n
    if n == 1:
        return len(BASE_CHARS)
    return len(BASE_CHARS) ** 2 * len(ALL_CHARS) ** (n - 2)


def is_premium_alias(username):
    """Names starting or ending with '.', '-' or '_' need a premium account."""
    return bool(username) and (username[0] in PREMIUM_CHARS or username[-1] in PREMIUM_CHARS)


_URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?guns\.lol/", re.IGNORECASE)


def normalize_username(value):
    """'https://guns.lol/Efe/' -> 'efe', '@efe' -> 'efe'."""
    name = _URL_PREFIX.sub("", value.strip())
    return name.strip().strip("/").lstrip("@").lower()


def read_text_lines(path):
    """Reads a text file saved as UTF-8 (with or without BOM), UTF-16 or ANSI."""
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return text.splitlines()


def load_customlist(path=CUSTOMLIST_FILE):
    """Returns (usernames, invalid_entries, duplicate_count). Raises FileNotFoundError."""
    usernames, invalid, seen = [], [], set()
    duplicates = 0
    for raw in read_text_lines(path):
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        name = normalize_username(line)
        if not name or len(name) > MAX_USERNAME_LENGTH or not USERNAME_PATTERN.match(name):
            invalid.append(line)
            continue
        if name in seen:
            duplicates += 1
            continue
        seen.add(name)
        usernames.append(name)
    return usernames, invalid, duplicates


class UsernameSource:
    """Yields usernames: the custom list (plus one retry pass for errors) or unique randoms."""

    def __init__(self, letter_count=None, customlist=None, filter_premium=False):
        self.is_custom = bool(customlist)
        self.letter_count = letter_count
        self.filter_premium = filter_premium
        self.queue = deque(customlist or [])
        self.initial = len(self.queue)
        self.served = 0
        self.deferred = set()
        self.seen = set()
        self.exhausted = False
        self.total = None if self.is_custom else count_combinations(letter_count, filter_premium)

    @property
    def in_retry_pass(self):
        return self.is_custom and self.served > self.initial

    def __iter__(self):
        return self

    def __next__(self):
        if self.is_custom:
            if not self.queue:
                raise StopIteration
            self.served += 1
            return self.queue.popleft()
        if len(self.seen) >= self.total:
            self.exhausted = True
            raise StopIteration
        while True:  # never check the same random name twice in one session
            name = random_letters(self.letter_count, self.filter_premium)
            if name not in self.seen:
                self.seen.add(name)
                return name

    def defer(self, username):
        """Queues a username that errored for one more try at the end (custom lists)."""
        if not self.is_custom or username in self.deferred:
            return False
        self.deferred.add(username)
        self.queue.append(username)
        return True


class HitWriter:
    """Appends unclaimed usernames to unclaimed.txt without duplicates."""

    def __init__(self, path=UNCLAIMED_FILE):
        self.path = path
        self.written = 0
        self.failed = False
        self.existing = set()
        if os.path.exists(path):
            try:
                self.existing = {line.strip().lower() for line in read_text_lines(path) if line.strip()}
            except OSError:
                pass

    def add(self, username):
        """Returns 'saved', 'duplicate' or 'failed'."""
        key = username.lower()
        if key in self.existing:
            return "duplicate"
        try:
            prefix = ""
            if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
                with open(self.path, "rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) not in (b"\n", b"\r"):
                        prefix = "\n"
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(prefix + username + "\n")
        except OSError:
            self.failed = True
            return "failed"
        self.existing.add(key)
        self.written += 1
        return "saved"


# =============================================================================
#  Sites / bio-link providers
# =============================================================================

def _fold(text):
    """Case-insensitive, Turkish-safe comparison form ('KULLANICI ADI' == 'kullanıcı adı')."""
    return (text or "").casefold().replace("\u0131", "i").replace("\u0307", "")


class Site:
    """One profile provider: guns.lol, haunt.gg, jugs.lol or any custom bio link.

    Detection uses the navigation HTTP status first (404/410 = free), then the
    visible <h1> text, then - only for sites that opt in - the body text.
    """

    def __init__(self, name, url, not_found=(), title_not_found=(), premium=False,
                 check_body=False, trust_positive=True, status_not_found=NOT_FOUND_STATUSES,
                 available=(), claimed=(), fast_http=False, fast_http_claimed=False,
                 note="", builtin=False):
        # `name` is the routing key for webhooks, settings and saved hits.
        name = str(name or "").strip().lower()
        url = str(url or "").strip()
        if not name:
            raise ValueError("missing name")
        if "{username}" not in url:
            raise ValueError("url must contain the {username} placeholder")
        if not re.match(r"^https?://[^\s{]+$", url.replace("{username}", "name")):
            raise ValueError("url must start with http:// or https://")
        if not isinstance(not_found, (list, tuple)):
            not_found = [not_found] if not_found else []
        if not isinstance(title_not_found, (list, tuple)):
            title_not_found = [title_not_found] if title_not_found else []
        self.name = name
        self.url = url
        self.not_found = tuple(_fold(str(marker)) for marker in not_found if str(marker).strip())
        self.title_not_found = tuple(_fold(str(marker)) for marker in title_not_found if str(marker).strip())
        self.available = tuple(_fold(str(marker)) for marker in available if str(marker).strip())
        self.claimed = tuple(_fold(str(marker)) for marker in claimed if str(marker).strip())
        self.fast_http = bool(fast_http)            # verdict is visible over plain HTTP
        self.fast_http_claimed = bool(fast_http_claimed)
        self.premium = bool(premium)
        self.check_body = bool(check_body)
        self.trust_positive = bool(trust_positive)
        try:
            self.status_not_found = tuple(int(code) for code in status_not_found)
        except (TypeError, ValueError):
            raise ValueError("status_not_found must be a list of HTTP codes") from None
        self.note = str(note or "").strip()
        self.builtin = builtin

    @property
    def label(self):
        return self.name

    def profile_url(self, username):
        return self.url.replace("{username}", quote(username, safe="._-"))

    def display(self, username):
        return "{}/{}".format(self.label, username)

    def __repr__(self):
        return "Site({!r})".format(self.name)


# Built-ins. Each rule was checked against the real site (see README).
BUILTIN_SITES = (
    Site("guns.lol", "https://guns.lol/{username}",
         # A claimed profile contains the h1 "User Not found" from a widget, so headings
         # must match exactly and body text is only scanned for explicit free phrases.
         not_found=("username not found", "kullanici adi bulunamadi"),
         available=("claim this username", "bu kullanici adini"),
         claimed=(" | guns.lol",), premium=True, check_body=False,
         fast_http=True, note="premium aliases start or end with . _ -", builtin=True),
    Site("haunt.gg", "https://haunt.gg/{username}",
         not_found=("this user does not exist", "profile not found",
                    "this page could not be found"),
         check_body=False, builtin=True,
         note="status + title only: profile widgets contain the words 'Not Found'"),
    Site("jugs.lol", "https://jugs.lol/{username}",
         not_found=("not found",), check_body=False, fast_http=True, fast_http_claimed=True,
         builtin=True, note="404 = free, 200 = claimed; checked without a browser"),
)


def load_sites(path=SITES_FILE):
    """Returns (sites, problems).

    Built-ins are always available. sites.json may add any bio-link provider or
    override a built-in (same name) with your own markers:
        {"sites": [{"name": "bio.site", "url": "https://bio.site/{username}",
                    "not_found": ["page not found"], "premium": false}]}
    """
    sites = [Site(name=s.name, url=s.url, not_found=s.not_found, title_not_found=s.title_not_found,
                  available=s.available, claimed=s.claimed, premium=s.premium, check_body=s.check_body,
                  trust_positive=s.trust_positive, status_not_found=s.status_not_found, note=s.note,
                  fast_http=s.fast_http, fast_http_claimed=s.fast_http_claimed,
                  builtin=True) for s in BUILTIN_SITES]
    problems = []
    if not os.path.exists(path):
        return sites, problems
    try:
        raw = "\n".join(read_text_lines(path))
        raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)  # allow // comments
        data = json.loads(raw) if raw.strip() else {"sites": []}
    except (OSError, ValueError) as exc:
        return sites, ["Could not read sites.json ({}). Using the built-in sites.".format(exc)]
    entries = data.get("sites", []) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return sites, ["sites.json must contain a list under \"sites\". Using the built-in sites."]
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append("sites.json entry #{} is not an object and was ignored.".format(position))
            continue
        if entry.get("enabled") is False:
            continue
        try:
            site = Site(
                name=entry.get("name") or entry.get("host") or "site{}".format(position),
                url=entry.get("url") or "https://{}/{{username}}".format(entry.get("host", "")),
                not_found=entry.get("not_found", ()),
                title_not_found=entry.get("title_not_found", ()),
                available=entry.get("available", ()),
                claimed=entry.get("claimed", ()),
                fast_http=entry.get("fast_http", bool(entry.get("http"))),
                fast_http_claimed=entry.get("fast_http_claimed", False),
                premium=entry.get("premium", False),
                check_body=entry.get("check_body", bool(entry.get("not_found"))),
                trust_positive=entry.get("trust_positive", True),
                status_not_found=entry.get("status_not_found", NOT_FOUND_STATUSES),
                note=entry.get("note", ""),
            )
        except ValueError as exc:
            label = entry.get("name") or entry.get("host") or "#{}".format(position)
            problems.append("sites.json entry #{} ({}): {}. Skipped.".format(position, label, exc))
            continue
        for index, existing in enumerate(sites):
            if existing.name == site.name:
                sites[index] = site
                break
        else:
            sites.append(site)
    return sites, problems


# =============================================================================
#  Proxies
# =============================================================================

def parse_proxy(line):
    """Parses 'host:port:user:pass', 'user:pass@host:port' or 'host:port' (http:// optional)."""
    value = line.strip()
    if "://" in value:
        scheme, value = value.split("://", 1)
        if scheme.lower() != "http":
            raise ValueError("unsupported scheme '{}' (only HTTP proxies are supported)".format(scheme))
    value = value.rstrip("/")
    username = password = ""
    if "@" in value:
        credentials, _, address = value.rpartition("@")
        username, sep, password = credentials.partition(":")
        host, sep2, port_text = address.rpartition(":")
        if not sep or not sep2:
            raise ValueError("expected username:password@host:port")
    else:
        parts = value.split(":", 3)
        if len(parts) == 4:
            host, port_text, username, password = parts
        elif len(parts) == 2:
            host, port_text = parts
        else:
            raise ValueError("expected host:port:username:password")
    host, port_text = host.strip(), port_text.strip()
    username, password = username.strip(), password.strip()
    if not host or " " in host:
        raise ValueError("missing proxy host")
    if bool(username) != bool(password):
        raise ValueError("missing proxy username or password")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError("invalid proxy port") from None
    if not 1 <= port <= 65535:
        raise ValueError("invalid proxy port")
    return {"host": host, "port": port, "username": username, "password": password}


def load_proxies(path=PROXIES_FILE):
    """Loads HTTP proxies (one per line). Lines starting with # or // are ignored."""
    proxies, seen = [], set()
    for line_number, raw in enumerate(read_text_lines(path), start=1):
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        try:
            proxy = parse_proxy(line)
        except ValueError as exc:
            raise ValueError("Invalid proxy on line {}: {}.".format(line_number, exc)) from None
        key = (proxy["host"].lower(), proxy["port"], proxy["username"], proxy["password"])
        if key not in seen:
            seen.add(key)
            proxies.append(proxy)
    if not proxies:
        raise ValueError("proxies.txt does not contain any usable proxies.")
    return proxies


def get_requests_proxy_config(proxy):
    """Builds requests-compatible proxy URLs, escaping credentials safely."""
    if proxy.get("username"):
        auth = "{}:{}@".format(quote(proxy["username"], safe=""), quote(proxy["password"], safe=""))
    else:
        auth = ""
    proxy_url = "http://{}{}:{}".format(auth, proxy["host"], proxy["port"])
    return {"http": proxy_url, "https": proxy_url}


def _read_http_head(sock, limit=65536):
    """Reads an HTTP message head. Returns (head, leftover_bytes); (b'', b'') on EOF."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return b"", b""
        data += chunk
        if len(data) > limit:
            raise ValueError("HTTP header too large")
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _status_code(head):
    parts = head.split(b" ", 2)
    return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0


def _body_complete(header_lines, rest):
    """True when the whole request body is already buffered (so it can be re-sent)."""
    length = 0
    for line in header_lines[1:]:
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"transfer-encoding":
            return False
        if name == b"content-length":
            try:
                length = int(value.strip())
            except ValueError:
                return False
    return len(rest) >= length


def _close_socket(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _proxy_auth_header(proxy):
    if not proxy.get("username"):
        return b""
    token = base64.b64encode("{}:{}".format(proxy["username"], proxy["password"]).encode("utf-8"))
    return b"Proxy-Authorization: Basic " + token + b"\r\n"


_HOP_HEADERS = {b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive"}
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class ProxyRotator:
    """Local proxy on 127.0.0.1 that forwards Chrome's traffic to the proxies from proxies.txt.

    Chrome cannot authenticate to proxies by itself, and since Chrome 137 it no
    longer loads extensions passed with --load-extension, which broke the v1
    extension approach. This forwarder adds the Proxy-Authorization header
    itself, rotates the upstream proxy between usernames, fails over to the next
    proxies when one is dead and benches broken ones - standard library only.
    """

    CONNECT_TIMEOUT = 8
    RESPONSE_TIMEOUT = 45
    FAILOVER_DEPTH = 3
    BENCH_SECONDS = 60
    IDLE_TIMEOUT = 90

    def __init__(self, proxies):
        if not proxies:
            raise ValueError("ProxyRotator needs at least one proxy.")
        self.proxies = list(proxies)
        self.port = None
        self._index = 0
        self._lock = threading.Lock()
        self._clients = set()
        self._failures = {}
        self._benched = {}
        self._server = None
        self._closed = threading.Event()

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(64)
        server.settimeout(0.5)
        self._server = server
        self.port = server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="proxy-rotator", daemon=True).start()
        _rotators.add(self)
        return self

    def stop(self):
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._server.close()
        except (OSError, AttributeError):
            pass
        self.drop_connections()
        _rotators.discard(self)

    @property
    def current(self):
        with self._lock:
            return self.proxies[self._index]

    def healthy_count(self):
        now = time.monotonic()
        with self._lock:
            return sum(1 for i in range(len(self.proxies)) if self._benched.get(i, 0) <= now)

    def rotate(self):
        """Switches to the next healthy proxy and drops open tunnels so the next page uses it."""
        if len(self.proxies) < 2:
            return
        with self._lock:
            now = time.monotonic()
            count = len(self.proxies)
            for step in range(1, count + 1):
                candidate = (self._index + step) % count
                if self._benched.get(candidate, 0) <= now:
                    self._index = candidate
                    break
            else:
                self._index = (self._index + 1) % count
        self.drop_connections()

    def drop_connections(self):
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _chain(self):
        """Current proxy first, then the next ones as fail-over (healthy proxies preferred)."""
        with self._lock:
            now = time.monotonic()
            count = len(self.proxies)
            order = [(self._index + i) % count for i in range(count)]
            healthy = [i for i in order if self._benched.get(i, 0) <= now]
            return (healthy or order)[:self.FAILOVER_DEPTH]

    def _report(self, index, ok, fatal=False):
        with self._lock:
            if ok:
                self._failures.pop(index, None)
                self._benched.pop(index, None)
                return
            failures = self._failures.get(index, 0) + 1
            if fatal or failures >= 2:
                self._benched[index] = time.monotonic() + self.BENCH_SECONDS
                failures = 0
            self._failures[index] = failures

    def _accept_loop(self):
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client):
        upstream = None
        with self._lock:
            self._clients.add(client)
        try:
            client.settimeout(30)
            head, rest = _read_http_head(client)
            if not head:
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
            if len(parts) != 3:
                return
            if parts[0].upper() == "CONNECT":
                upstream, leftover = self._open_tunnel(parts[1])
                if upstream is None:
                    client.sendall(_BAD_GATEWAY)
                    return
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if leftover:
                    client.sendall(leftover)
                if rest:
                    upstream.sendall(rest)
            else:
                upstream, response = self._open_forward(head, rest)
                if upstream is None:
                    client.sendall(_BAD_GATEWAY)
                    return
                if response:
                    client.sendall(response)
            self._pipe(client, upstream)
        except Exception:
            pass
        finally:
            with self._lock:
                self._clients.discard(client)
            _close_socket(upstream)
            _close_socket(client)

    def _open_tunnel(self, target):
        for index in self._chain():
            proxy = self.proxies[index]
            sock = None
            try:
                sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=self.CONNECT_TIMEOUT)
                request = "CONNECT {0} HTTP/1.1\r\nHost: {0}\r\n".format(target).encode("latin-1")
                sock.sendall(request + _proxy_auth_header(proxy) + b"Proxy-Connection: Keep-Alive\r\n\r\n")
                head, leftover = _read_http_head(sock)
                status = _status_code(head)
                if status == 200:
                    self._report(index, True)
                    sock.settimeout(None)
                    return sock, leftover
                self._report(index, False, fatal=status in (401, 403, 407))
            except (OSError, ValueError):
                self._report(index, False)
            _close_socket(sock)
        return None, b""

    def _open_forward(self, head, rest):
        """Plain-HTTP request. Returns (upstream_socket, response_bytes_to_relay) or (None, b'')."""
        lines = head.split(b"\r\n")
        kept = [lines[0]] + [line for line in lines[1:]
                             if line.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS]
        replayable = _body_complete(lines, rest)
        for index in self._chain():
            proxy = self.proxies[index]
            sock = None
            try:
                sock = socket.create_connection((proxy["host"], proxy["port"]), timeout=self.CONNECT_TIMEOUT)
                request = b"\r\n".join(kept) + b"\r\nConnection: close\r\n" + _proxy_auth_header(proxy) + b"\r\n"
                sock.sendall(request + rest)
                if not replayable:  # streaming upload: cannot be re-sent, relay as-is
                    sock.settimeout(None)
                    self._report(index, True)
                    return sock, b""
                sock.settimeout(self.RESPONSE_TIMEOUT)
                response_head, leftover = _read_http_head(sock)
                status = _status_code(response_head)
                if response_head and status != 407:
                    self._report(index, True)
                    sock.settimeout(None)
                    return sock, response_head + b"\r\n\r\n" + leftover
                self._report(index, False, fatal=status == 407)
            except (OSError, ValueError):
                self._report(index, False)
            _close_socket(sock)
        return None, b""

    def _pipe(self, client, upstream):
        client.settimeout(None)
        upstream.settimeout(None)
        sockets = [client, upstream]
        while not self._closed.is_set():
            readable, _, broken = select.select(sockets, [], sockets, self.IDLE_TIMEOUT)
            if broken or not readable:
                return
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                (upstream if sock is client else client).sendall(data)


# =============================================================================
#  Discord webhooks
# =============================================================================

def is_valid_webhook_url(url):
    return bool(WEBHOOK_PATTERN.match(url.strip()))


def build_webhook_payload(username, site=None):
    """The purple v1 embed, now pointing at the site that has the hit."""
    profile = site.profile_url(username) if site is not None else "https://guns.lol/{}".format(username)
    premium = "Yes" if is_premium_alias(username) else "No"
    if site is not None and not site.premium:
        premium = "Not applicable"
    return {
        "embeds": [{
            "title": "Available: {} ({})".format(username, profile),
            "url": profile,
            "description": "github.com/efekrbas",
            "color": 0x9B59B6,
            "fields": [
                {"name": "Site", "value": site.name if site is not None else "guns.lol", "inline": True},
                {"name": "Username", "value": "`{}`".format(username), "inline": True},
                {"name": "Length", "value": str(len(username)), "inline": True},
                {"name": "Premium alias", "value": premium, "inline": True},
            ],
            "footer": {"text": "guns.lol username checker v" + __version__},
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }]
    }


# ---------------------------------------------------------------- saved webhooks

WEBHOOKS_HEADER = (
    "# Saved Discord webhooks used by gunslol.py - one per line.\n"
    "# Format:  name | https://discord.com/api/webhooks/... | site1,site2\n"
    "# The third field routes a webhook to specific sites (optional = every site).\n"
    "#   Jugs hits | https://discord.com/api/webhooks/12/ab | jugs.lol\n"
    "#   Everything | https://discord.com/api/webhooks/34/cd | *\n"
    "# This file contains secrets: do not commit or share it.\n"
)

ALL_SITES_TOKEN = "*"


def _parse_route(value):
    """'jugs.lol, guns.lol' -> ('jugs.lol','guns.lol'); '*' / '' / None -> ('*',).

    Accepts a tuple/list as well, so callers can pass routes around safely.
    """
    if value is None:
        return (ALL_SITES_TOKEN,)
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip().lower() for part in value if str(part).strip()]
    else:
        text = str(value).strip()
        if not text or text.lower() in (ALL_SITES_TOKEN, "all", "any", "('*',)"):
            return (ALL_SITES_TOKEN,)
        parts = [part.strip().lower() for part in text.replace(";", ",").split(",") if part.strip()]
    parts = [part for part in dict.fromkeys(parts) if part]
    if not parts or ALL_SITES_TOKEN in parts:
        return (ALL_SITES_TOKEN,)
    return tuple(parts)


def _route_label(route):
    if ALL_SITES_TOKEN in route:
        return "all sites"
    return ", ".join(route) if route else "all sites"


def webhook_serves(entry, site_name):
    """True when a saved webhook should receive hits from this site."""
    route = entry.get("route") or (ALL_SITES_TOKEN,)
    return ALL_SITES_TOKEN in route or (site_name or "").lower() in route


def matching_webhooks(webhooks, site_name):
    return [entry for entry in webhooks if webhook_serves(entry, site_name)]


def webhook_mask(url):
    """Safe display form: never prints the full token to the terminal."""
    clean = url.split("?")[0].rstrip("/")
    _, separator, rest = clean.partition("/api/webhooks/")
    parts = rest.split("/") if separator else []
    if len(parts) >= 2:
        return "discord.com/api/webhooks/{}…/{}…".format(parts[0][:4], parts[1][:4])
    return clean


def _webhook_default_label(url):
    clean = url.split("?")[0].rstrip("/")
    _, separator, rest = clean.partition("/api/webhooks/")
    parts = rest.split("/") if separator else []
    return "Webhook {}".format(parts[0][-4:]) if parts else "Webhook"


def load_saved_webhooks(path=WEBHOOKS_FILE):
    """Returns (entries, invalid_count).

    entries: [{'label', 'url', 'route'}] where route is a tuple of site names or ('*',).
    """
    entries, invalid, seen = [], 0, set()
    if not os.path.exists(path):
        return entries, invalid
    try:
        lines = read_text_lines(path)
    except OSError:
        return entries, 1
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", "//")):
            continue
        fields = [part.strip() for part in line.split("|")]
        label, url, route_text = "", "", ""
        if len(fields) >= 2 and is_valid_webhook_url(fields[1]):
            label, url, route_text = fields[0], fields[1], fields[2] if len(fields) > 2 else ""
        elif " = " in line:
            left, _, right = line.partition(" = ")
            if is_valid_webhook_url(right.strip()):
                label, url = left.strip(), right.strip()
        elif len(fields) == 1 and is_valid_webhook_url(fields[0]):
            url = fields[0]
        url = url.strip()
        if not is_valid_webhook_url(url):
            invalid += 1
            continue
        if url in seen:
            continue
        seen.add(url)
        entries.append({"label": label or _webhook_default_label(url), "url": url,
                        "route": _parse_route(route_text)})
    return entries, invalid


def _rewrite_webhooks(entries, path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(WEBHOOKS_HEADER)
        for entry in entries:
            line = "{} | {}".format(entry["label"], entry["url"])
            if entry.get("route") and ALL_SITES_TOKEN not in entry["route"]:
                line += " | " + ",".join(entry["route"])
            handle.write(line + "\n")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def save_webhook(url, label="", path=WEBHOOKS_FILE, route=None):
    """Saves a webhook (with optional site routing) for next time."""
    if not is_valid_webhook_url(url):
        raise ValueError("invalid Discord webhook URL")
    entries, _ = load_saved_webhooks(path)
    if any(entry["url"] == url.strip() for entry in entries):
        return "exists"
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    if new_file:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(WEBHOOKS_HEADER)
    entry = {"label": (label or _webhook_default_label(url)).strip(), "url": url.strip(),
             "route": _parse_route(route)}
    entries.append(entry)
    _rewrite_webhooks(entries, path)
    return "saved"


def remove_saved_webhooks(urls, path=WEBHOOKS_FILE):
    """Removes the given URLs (all when urls is None). Returns the number removed."""
    entries, _ = load_saved_webhooks(path)
    targets = {url.strip() for url in urls} if urls is not None else None
    keep = [entry for entry in entries if targets is not None and entry["url"] not in targets]
    removed = len(entries) - len(keep)
    if removed:
        try:
            _rewrite_webhooks(keep, path)
        except OSError:
            return 0
    return removed


def set_webhook_route(url, route, path=WEBHOOKS_FILE):
    """Updates which sites an existing webhook serves. Returns True on success."""
    entries, _ = load_saved_webhooks(path)
    changed = False
    for entry in entries:
        if entry["url"] == url.strip():
            entry["route"] = _parse_route(route)
            changed = True
    if changed:
        try:
            _rewrite_webhooks(entries, path)
        except OSError:
            return False
    return changed


def _retry_after(response):
    """Seconds to wait after a Discord 429."""
    for header in ("Retry-After", "X-RateLimit-Reset-After"):
        try:
            return max(float(response.headers[header]), 0.5)
        except (KeyError, TypeError, ValueError):
            pass
    try:
        value = float(response.json().get("retry_after", 2))
        return value / 1000 if value > 60 else max(value, 0.5)  # old API versions use ms
    except (ValueError, TypeError, AttributeError):
        return 2.0


def _request_options(proxies):
    proxy = random.choice(proxies) if proxies else None
    return {"proxies": get_requests_proxy_config(proxy)} if proxy else {}


def send_discord_webhook(webhook_url, username, proxies=None, attempts=4, site=None):
    """Sends a Discord notification. Returns 'ok', 'invalid' or 'failed'."""
    payload = build_webhook_payload(username, site)
    for _ in range(attempts):
        try:
            response = requests.post(webhook_url, json=payload, timeout=10, **_request_options(proxies))
        except requests.RequestException:
            time.sleep(1.5)
            continue
        if response.status_code in (200, 204):
            return "ok"
        if response.status_code == 429:
            time.sleep(min(_retry_after(response), 30))
            continue
        if response.status_code in (400, 401, 403, 404):
            return "invalid"  # deleted webhook: retrying will not help
        time.sleep(1.5)
    return "failed"


def verify_webhook(webhook_url, proxies=None):
    """Asks Discord whether the webhook exists. Returns ('ok'|'invalid'|'unknown', detail)."""
    try:
        response = requests.get(webhook_url, timeout=8, **_request_options(proxies))
    except requests.RequestException as exc:
        return "unknown", type(exc).__name__
    if response.status_code == 200:
        try:
            return "ok", response.json().get("name") or "webhook"
        except (ValueError, AttributeError):
            return "ok", "webhook"
    if response.status_code in (401, 403, 404):
        return "invalid", "HTTP {}".format(response.status_code)
    return "unknown", "HTTP {}".format(response.status_code)


def _normalize_webhooks(webhooks):
    """Accepts one/many strings or {'label','url','route'} dicts and returns unique entries."""
    if isinstance(webhooks, (str, dict)):
        webhooks = [webhooks]
    entries, seen = [], set()
    for item in webhooks or []:
        if isinstance(item, dict):
            label = str(item.get("label") or "")
            url = str(item.get("url") or "")
            route = _parse_route(item.get("route"))
        else:
            label, url, route = "", str(item), (ALL_SITES_TOKEN,)
        url = url.strip()
        if not is_valid_webhook_url(url) or url in seen:
            continue
        seen.add(url)
        entries.append({"label": label.strip() or _webhook_default_label(url), "url": url,
                        "route": route})
    return entries


def verify_webhooks(webhooks, proxies=None):
    """Checks several webhooks in parallel. Returns [(entry, status, detail)]."""
    entries = _normalize_webhooks(webhooks)
    if not entries:
        return []
    with ThreadPoolExecutor(max_workers=min(5, len(entries))) as pool:
        results = list(pool.map(lambda entry: verify_webhook(entry["url"], proxies), entries))
    return [(entry, status, detail) for entry, (status, detail) in zip(entries, results)]


class WebhookNotifier:
    """Delivers a hit to the webhooks dedicated to that site, on a background thread.

    Each saved webhook carries a route: ('*',) for every site, or a tuple of site
    names (jugs.lol hits only ever reach the webhooks routed to jugs.lol).
    """

    def __init__(self, webhooks, proxies=None):
        self.all_webhooks = _normalize_webhooks(webhooks)
        self.proxies = proxies
        self.sent = 0
        self.failed = 0
        self.invalid = []
        self.delivered = {}      # webhook label -> number of notifications sent
        self.skipped = {}        # site name -> webhooks not routed to it
        self._queue = Queue()
        self._thread = threading.Thread(target=self._worker, name="webhooks", daemon=True)
        self._thread.start()

    @property
    def webhooks(self):
        return self.all_webhooks

    @property
    def pending(self):
        return self._queue.unfinished_tasks

    def routes_for(self, site_name):
        """Webhook labels that will receive hits from this site."""
        return [entry["label"] for entry in matching_webhooks(self.all_webhooks, site_name)]

    def notify(self, username, site=None):
        self._queue.put((username, site))

    def _worker(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                username, site = item
                site_name = site.name if site is not None else BUILTIN_SITES[0].name
                targets = matching_webhooks(self.all_webhooks, site_name)
                for entry in self.all_webhooks:
                    if entry not in targets:
                        self.skipped[site_name] = self.skipped.get(site_name, 0) + 1
                        continue
                    result = send_discord_webhook(entry["url"], username, self.proxies, site=site)
                    if result == "ok":
                        self.sent += 1
                        self.delivered[entry["label"]] = self.delivered.get(entry["label"], 0) + 1
                    else:
                        self.failed += 1
                    if result == "invalid":  # deleted / revoked: stop using it
                        self.invalid.append(entry["label"])
                        self.all_webhooks.remove(entry)
            except Exception:
                self.failed += 1
            finally:
                self._queue.task_done()

    def close(self, timeout=30):
        self._queue.put(None)
        self._thread.join(timeout)


# =============================================================================
#  Browser engine
# =============================================================================

_stop_event = threading.Event()   # set by Ctrl+C / console close while checking
_run_active = threading.Event()
_sessions = set()
_rotators = set()
_cleanup_lock = threading.RLock()


def _raise_if_stopping():
    if _stop_event.is_set():
        raise KeyboardInterrupt


def _sleep(seconds):
    if seconds > 0 and _stop_event.wait(seconds):
        raise KeyboardInterrupt


class BrowserLaunchError(RuntimeError):
    pass


def get_random_user_agent():
    """Returns a random modern user-agent for this OS.

    Only used until Chrome is running - right after launch the user-agent and
    client hints are matched to the real installed Chrome version.
    """
    if sys.platform.startswith("win"):
        os_token = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        os_token = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_token = "X11; Linux x86_64"
    major = random.choice((141, 142, 143, 144))
    return ("Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/{}.0.0.0 Safari/537.36".format(os_token, major))


def build_user_agent(full_version):
    """Returns (user_agent, client_hints_metadata, navigator_platform) for a Chrome version."""
    major = full_version.split(".")[0]
    machine = platform.machine().lower()
    arch = "arm" if machine.startswith(("arm", "aarch")) else "x86"
    if sys.platform.startswith("win"):
        build = sys.getwindowsversion().build if hasattr(sys, "getwindowsversion") else 0
        os_token, ch_platform = "Windows NT 10.0; Win64; x64", "Windows"
        ch_version, nav_platform = ("15.0.0" if build >= 22000 else "10.0.0"), "Win32"
    elif sys.platform == "darwin":
        parts = ((platform.mac_ver()[0] or "14.0").split(".") + ["0", "0"])[:3]
        os_token, ch_platform = "Macintosh; Intel Mac OS X 10_15_7", "macOS"
        ch_version, nav_platform = ".".join(parts), "MacIntel"
    else:
        os_token, ch_platform, ch_version, nav_platform = "X11; Linux x86_64", "Linux", "", "Linux x86_64"
    user_agent = ("Mozilla/5.0 ({}) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/{}.0.0.0 Safari/537.36".format(os_token, major))
    names = ("Google Chrome", "Chromium", "Not_A Brand")
    metadata = {
        "brands": [{"brand": names[0], "version": major}, {"brand": names[1], "version": major},
                   {"brand": names[2], "version": "24"}],
        "fullVersionList": [{"brand": names[0], "version": full_version},
                            {"brand": names[1], "version": full_version},
                            {"brand": names[2], "version": "24.0.0.0"}],
        "platform": ch_platform,
        "platformVersion": ch_version,
        "architecture": arch,
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }
    return user_agent, metadata, nav_platform


def _short_error(exc):
    message = (getattr(exc, "msg", None) or str(exc) or type(exc).__name__).strip()
    first = message.splitlines()[0] if message else type(exc).__name__
    return first[:220]


def _launch_hint(message):
    text = message.lower()
    if "binary" in text or "chrome not found" in text or "cannot find chrome" in text:
        return "Google Chrome was not found. Install it from https://www.google.com/chrome/"
    if "session not created" in text:
        return "Chrome and ChromeDriver do not match. Update Chrome, then run again."
    if "unable to obtain" in text or "selenium manager" in text or "driver" in text:
        return "ChromeDriver could not be downloaded. Check your internet connection or run: pip install -U selenium"
    return "Make sure Google Chrome is installed and up to date."


def kill_process_tree(pid):
    """Kills one process and its children (only ours - never every chromedriver on the PC)."""
    if not pid:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        except Exception:
            pass
        return
    try:
        subprocess.run(["pkill", "-KILL", "-P", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


_DEAD_SESSION_MARKERS = ("invalid session id", "no such window", "chrome not reachable", "disconnected",
                         "session deleted", "target window already closed", "no such session",
                         "unable to discover open window", "tab crashed")


class BrowserSession:
    """Owns the headless Chrome instance: launch, crash recovery and cleanup."""

    def __init__(self, proxy_port=None):
        self.proxy_port = proxy_port
        self.driver = None
        self.version = None
        self.restarts = 0
        self._process = None  # chromedriver Popen (Chrome runs as its child)

    def _options(self):
        options = Options()
        for argument in (
            "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--blink-settings=imagesEnabled=false", "--disable-blink-features=AutomationControlled",
            "--disable-background-networking", "--disable-sync", "--disable-default-apps",
            "--disable-translate", "--disable-extensions", "--js-flags=--max-old-space-size=128",
            "--log-level=3", "--silent", "--disable-logging", "--lang=en-US", "--no-first-run",
            "--no-default-browser-check", "--mute-audio", "--window-size=1280,800",
            "--user-agent=" + get_random_user_agent(),
        ):
            options.add_argument(argument)
        if self.proxy_port:
            options.add_argument("--proxy-server=http://127.0.0.1:{}".format(self.proxy_port))
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option("prefs", {
            "profile.managed_default_content_settings.images": 2,
            "intl.accept_languages": "en-US,en",
        })
        options.page_load_strategy = "eager"
        return options

    def start(self):
        service = Service(log_output=subprocess.DEVNULL)
        if os.name == "nt":
            service.creation_flags = subprocess.CREATE_NO_WINDOW
        try:
            driver = webdriver.Chrome(service=service, options=self._options())
        except Exception as exc:
            raise BrowserLaunchError(_short_error(exc)) from exc
        self.driver = driver
        self._process = getattr(getattr(driver, "service", None), "process", None)
        _sessions.add(self)
        try:
            driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            driver.implicitly_wait(0)
            driver.set_script_timeout(SCRIPT_TIMEOUT)
        except Exception:
            pass
        self._match_user_agent()
        return self

    def _match_user_agent(self):
        """Keeps the user-agent and client hints consistent with the real Chrome version."""
        try:
            self.version = str(self.driver.capabilities.get("browserVersion") or "")
            if not self.version.split(".")[0].isdigit():
                return
            user_agent, metadata, nav_platform = build_user_agent(self.version)
            self.driver.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": user_agent,
                "acceptLanguage": "en-US,en;q=0.9",
                "platform": nav_platform,
                "userAgentMetadata": metadata,
            })
        except Exception:
            pass  # the launch-time user-agent stays active

    def process_alive(self):
        process = self._process
        return process is not None and process.poll() is None

    def driver_alive(self):
        if self.driver is None or not self.process_alive():
            return False
        try:
            self.driver.window_handles
            return True
        except Exception:
            return False

    def is_dead(self, exc=None):
        if exc is not None and any(marker in str(exc).lower() for marker in _DEAD_SESSION_MARKERS):
            return True
        return not self.driver_alive()

    def restart(self):
        self.restarts += 1
        if self.restarts > MAX_BROWSER_RESTARTS:
            raise BrowserLaunchError("Chrome crashed {} times in a row.".format(MAX_BROWSER_RESTARTS))
        self.quit(hard=True)
        _raise_if_stopping()
        self.start()

    def mark_healthy(self):
        self.restarts = 0

    def kill(self):
        """Hard-kills our chromedriver + Chrome tree. Safe to call from any thread."""
        process = self._process
        if process is not None and process.poll() is None:
            kill_process_tree(process.pid)

    def quit(self, hard=False):
        """Graceful quit (10 s max), then kills whatever is still alive. Idempotent."""
        driver, self.driver = self.driver, None
        process, self._process = self._process, None
        _sessions.discard(self)
        if driver is not None and not hard:
            closer = threading.Thread(target=self._quiet_quit, args=(driver,), daemon=True)
            closer.start()
            closer.join(10)
        # Only a process we started and that is verifiably still running gets killed,
        # so a recycled PID can never hit an unrelated program.
        if process is not None and process.poll() is None:
            kill_process_tree(process.pid)

    @staticmethod
    def _quiet_quit(driver):
        try:
            driver.quit()
        except Exception:
            pass


def _kill_browser_processes():
    for session in list(_sessions):
        try:
            session.kill()
        except Exception:
            pass


def _cleanup(hard=False):
    """Quits Chrome/chromedriver and stops the local proxy (idempotent)."""
    with _cleanup_lock:
        for session in list(_sessions):
            try:
                session.quit(hard=hard)
            except Exception:
                pass
        for rotator in list(_rotators):
            try:
                rotator.stop()
            except Exception:
                pass


atexit.register(_cleanup)

if os.name == "nt":
    from ctypes import wintypes as _wintypes

    def _console_handler(event):
        try:
            if event in (0, 1):  # CTRL_C_EVENT / CTRL_BREAK_EVENT
                if not _run_active.is_set():
                    return False  # at a prompt: let Python raise KeyboardInterrupt
                _stop_event.set()
                _kill_browser_processes()  # unblocks pending WebDriver calls immediately
                return True
            _stop_event.set()  # console closed / logoff / shutdown
            _cleanup(hard=True)
        except Exception:
            pass
        return False

    try:
        _handler_func = ctypes.WINFUNCTYPE(_wintypes.BOOL, _wintypes.DWORD)(_console_handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_handler_func, True)
    except Exception:
        pass


def _install_signal_handlers():
    def _graceful_exit(signum, frame):
        _stop_event.set()
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _graceful_exit)
        except (OSError, ValueError, RuntimeError):
            pass


# =============================================================================
#  Detection
# =============================================================================

_SNAPSHOT_JS = """
var heads = [];
var nodes = document.getElementsByTagName('h1');
for (var i = 0; i < nodes.length; i++) {
    if (!nodes[i].getClientRects().length) { continue; }
    var text = (nodes[i].innerText || '').trim();
    if (text) { heads.push(text); }
}
var body = document.body ? (document.body.innerText || '') : '';
var status = null;
try {
    var nav = performance.getEntriesByType('navigation')[0];
    if (nav && typeof nav.responseStatus === 'number' && nav.responseStatus > 0) {
        status = nav.responseStatus;
    }
} catch (e) {}
return {title: document.title || '', h1: heads, body: body.slice(0, 5000), status: status};
"""


def classify_page(title, h1_texts, body_text="", status=None, site=None):
    """Returns 'loading', 'challenge', 'ratelimited', 'unclaimed', 'claimed' or 'unknown'.

    Priority: rate limit -> challenge -> HTTP status (404/410 = free) -> visible
    headings -> body (only for sites that opt in) -> 200 = claimed.
    """
    folded_title = _fold(title).strip()
    folded_body = _fold(body_text)
    headings = [_fold(text).strip() for text in h1_texts if text and text.strip()]
    site_not_found = site.not_found if site is not None else (
        "username not found", "kullanici adi bulunamadi", "bulunamadi")
    not_found_statuses = site.status_not_found if site is not None else NOT_FOUND_STATUSES

    if any(marker in folded_title for marker in RATE_LIMIT_MARKERS):
        return "ratelimited"
    if status in not_found_statuses:
        return "unclaimed"
    if status in CHALLENGE_STATUSES:
        return "challenge"
    if folded_title and any(marker in folded_title for marker in CHALLENGE_MARKERS):
        return "challenge"
    # 1. A claimed-profile signal wins over widget text (guns.lol profiles embed
    #    an h1 "User Not found" from a widget, which is not about the username).
    if site is not None and site.claimed and (
            any(marker in folded_title for marker in site.claimed)
            or any(any(marker in heading for marker in site.claimed) for heading in headings)):
        return "claimed"
    # 2. Explicit free phrases ("Claim this username...") anywhere in the page.
    if site is not None and site.available and (
            any(marker in folded_body for marker in site.available)
            or any(any(marker in heading for marker in site.available) for heading in headings)):
        return "unclaimed"
    if site is not None and site_not_found and any(marker in folded_title for marker in site_not_found):
        return "unclaimed"
    # 3. Headings must match a known not-found heading EXACTLY; "User Not found"
    #    from a widget must not count as "Username not found".
    if any(heading in site_not_found for heading in headings):
        return "unclaimed"
    if site is None and any(heading in GENERIC_NOT_FOUND_HEADINGS for heading in headings):
        return "unclaimed"
    if site is not None and not site.not_found and any(
            heading in GENERIC_NOT_FOUND_HEADINGS for heading in headings):
        return "unclaimed"
    body_markers = site_not_found if site is not None else (
        "username not found", "kullanici adi bulunamadi")
    if site is not None and site.check_body and any(marker in folded_body for marker in body_markers):
        return "unclaimed"
    if status == 200:
        return "claimed"
    if status is None:
        if not folded_title:
            return "loading"
        if headings or (site is not None and site.trust_positive):
            return "claimed"
        return "unknown"
    if status in (300, 301, 302, 303, 307, 308) or not folded_title:
        return "loading"
    return "unknown"


def page_snapshot(driver):
    data = driver.execute_script(_SNAPSHOT_JS)
    if not isinstance(data, dict):
        return "", [], "", None
    headings = [str(text) for text in (data.get("h1") or []) if text]
    status = data.get("status")
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return str(data.get("title") or ""), headings, str(data.get("body") or ""), status


def stop_loading(driver):
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass


def wait_for_verdict(driver, timeout=None, site=None):
    """Polls the page until it shows a verdict, a rate limit, or the timeout passes."""
    deadline = time.monotonic() + (CHALLENGE_WAIT if timeout is None else timeout)
    state = "loading"
    while True:
        _raise_if_stopping()
        try:
            state = classify_page(*page_snapshot(driver), site=site)
        except TimeoutException:
            state = "loading"
        except WebDriverException as exc:
            if any(marker in str(exc).lower() for marker in _DEAD_SESSION_MARKERS):
                raise
            state = "loading"  # page is navigating (e.g. challenge redirect)
        if state in ("unclaimed", "ratelimited", "claimed"):
            return state
        if time.monotonic() >= deadline:
            return state
        _sleep(POLL_INTERVAL)


def check_username(browser, username, rotator=None, site=None):
    """Checks one username on one site. Returns 'unclaimed', 'claimed' or 'error'.

    v1 reported 'claimed' when every attempt hit a challenge or rate-limit page;
    now an inconclusive result is always reported as error/timeout.
    """
    profile_site = site if site is not None else BUILTIN_SITES[0]
    url = profile_site.profile_url(username)
    for attempt in range(1, MAX_RETRIES + 1):
        _raise_if_stopping()
        try:
            driver = browser.driver
            try:
                driver.get(url)
            except TimeoutException:
                stop_loading(driver)  # the DOM may already contain the verdict
            state = wait_for_verdict(driver, site=profile_site)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            _raise_if_stopping()
            if browser.is_dead(exc):
                browser.restart()
            state = "network"
        if state in ("claimed", "unclaimed"):
            browser.mark_healthy()
            return state
        if browser.driver is not None:
            stop_loading(browser.driver)
        if attempt == MAX_RETRIES:
            break
        if rotator is not None:
            rotator.rotate()  # try the next proxy / IP
        if state == "ratelimited":
            _sleep(2.5 * attempt)
        elif state == "network":
            _sleep(1.5)
        else:
            _sleep(0.8)
    return "error"


# =============================================================================
#  Checker
# =============================================================================

class Stats:
    def __init__(self):
        self.started = time.monotonic()
        self.index = 0
        self.checked = 0          # unique usernames with at least one real site check
        self.seen = set()
        self.site_checks = 0      # individual site checks
        self.http_checks = 0      # answered over plain HTTP (no browser)
        self.browser_checks = 0   # needed the headless browser
        self.unclaimed = 0        # free usernames found (one per site)
        self.claimed = 0
        self.errors = 0
        self.skipped = 0
        self.sites = []
        self.per_site = {}

    def count(self, site_name, status):
        row = self.per_site.setdefault(site_name, {"unclaimed": 0, "claimed": 0, "error": 0, "skipped": 0})
        if status in row:
            row[status] += 1

    @property
    def elapsed(self):
        return time.monotonic() - self.started


def _format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "{:d}:{:02d}:{:02d}".format(hours, minutes, secs) if hours else "{:02d}:{:02d}".format(minutes, secs)


def update_title(stats=None):
    title = "guns.lol username checker v" + __version__
    if stats is not None:
        title += " | Checked: {} | Unclaimed: {} | Errors: {}".format(stats.checked, stats.unclaimed, stats.errors)
    set_console_title(title)


def print_summary(stats, hits=None, notifier=None, rotator=None):
    minutes = stats.elapsed / 60
    rate = stats.checked / minutes if minutes > 0 else 0.0
    rows = [
        ("Sites", ", ".join(stats.sites) if stats.sites else "guns.lol"),
        ("Checked", "{} usernames".format(stats.checked)),
        ("Site checks", "{} ({} HTTP, {} browser)".format(
            stats.site_checks, stats.http_checks, stats.browser_checks)),
        ("Unclaimed", "{} hit(s)".format(stats.unclaimed)),
        ("Claimed", str(stats.claimed)),
        ("Errors", str(stats.errors)),
        ("Skipped", str(stats.skipped)),
        ("Elapsed", _format_duration(stats.elapsed)),
        ("Speed", "{:.1f} usernames/min".format(rate)),
    ]
    if hits is not None:
        rows.append(("Saved", "{} new in unclaimed.txt".format(hits.written)))
    if notifier is not None:
        rows.append(("Webhooks", "{} sent to {} channel(s), {} failed".format(
            notifier.sent, len(notifier.delivered), notifier.failed)))
        if notifier.skipped:
            rows.append(("Routed away", "{} hit(s) went only to their own site's channel".format(
                sum(notifier.skipped.values()))))
        for label, count in notifier.delivered.items():
            rows.append(("  " + label, "{} notification(s)".format(count)))
    if rotator is not None:
        rows.append(("Proxies", "{}/{} healthy".format(rotator.healthy_count(), len(rotator.proxies))))
    print()
    print("  {}{}{}".format(PURPLE, "=" * 18 + " Summary " + "=" * 18, RESET))
    for label, value in rows:
        print("   {}{}{} {}{}{}".format(MUTED, (label + " ").ljust(14, "."), RESET, LIGHT_PURPLE, value, RESET))
    if len(stats.per_site) > 1:
        for name, row in stats.per_site.items():
            print("   {}{}{} {}{} free · {} claimed · {} error{}".format(
                MUTED, (name + " ").ljust(14, "."), RESET, Fore.GREEN, row["unclaimed"],
                row["claimed"], row["error"], RESET))
    print("  {}{}{}".format(PURPLE, "=" * 45, RESET))


def _sleep_between_checks(interval, using_proxies):
    """Dynamic delay (prevents Cloudflare/guns.lol rate limits on proxies and home IP)."""
    if interval > 0:
        _sleep(interval)
    elif using_proxies:
        _sleep(random.uniform(0.25, 0.45))
    else:
        _sleep(random.uniform(0.8, 1.2))


def check_user_status(letter_count, interval, customlist=None, filter_premium=False,
                      save_to_file=True, webhook_urls=None, proxies=None, sites=None,
                      webhook_url=None, speed="adaptive", use_http=True):
    """Checks usernames on one or more sites.

    Plain HTTP answers the verdict for sites that render it server-side (fast
    path); Chrome only handles the rest. A per-site adaptive pacer spaces every
    request and backs off automatically when a site pushes back.
    """
    if webhook_url and not webhook_urls:  # backwards compatible single-webhook call
        webhook_urls = [{"label": _webhook_default_label(webhook_url), "url": webhook_url}]
    sites = list(sites) if sites else [BUILTIN_SITES[0]]
    stats = Stats()
    stats.sites = [site.name for site in sites]
    source = UsernameSource(letter_count, customlist, filter_premium and any(s.premium for s in sites))
    hits = HitWriter(UNCLAIMED_FILE) if save_to_file else None
    rotator = notifier = browser = engine = None
    _stop_event.clear()
    _run_active.set()
    try:
        if proxies:
            rotator = ProxyRotator(proxies).start()
        print_info("Checking {}: {}.".format(
            "site" if len(sites) == 1 else "{} sites".format(len(sites)),
            ", ".join(site.name for site in sites)))
        engine = fastpath.FastEngine(sites, proxies, speed, interval) if use_http else None
        if engine is not None and engine.enabled:
            print_info("Fast path over HTTP: {}. Browser: {}. Pacing: {}.".format(
                ", ".join(engine.sites_for_http()), ", ".join(engine.skipped) or "not needed", speed))
        if webhook_urls:
            notifier = WebhookNotifier(webhook_urls, proxies)

        browser_failed = [False]

        def ensure_browser():
            """Starts Chrome only when a site actually needs rendering.

            If it cannot start, the run continues over HTTP and the affected
            sites are reported as errors instead of aborting everything.
            """
            nonlocal browser
            if browser is not None:
                return browser
            if browser_failed[0]:
                return None
            print_info("Launching headless Chrome for the sites that need JavaScript...")
            candidate = BrowserSession(proxy_port=rotator.port if rotator else None)
            try:
                candidate.start()
            except BrowserLaunchError as exc:
                browser_failed[0] = True
                print_error("Error launching Chrome: {}".format(exc))
                print_warn(_launch_hint(str(exc)))
                print_warn("Continuing over HTTP; sites that need a browser are reported as errors.")
                return None
            browser = candidate
            _raise_if_stopping()
            print_ok("Chrome {} ready{}. Press Ctrl+C to stop.".format(
                browser.version or "", " via {} proxies".format(len(proxies)) if proxies else ""))
            return browser
        if engine is None or not engine.enabled:
            if ensure_browser() is None:
                print_warn("Nothing can be checked without a browser - stopping this run.")
                return stats
        else:
            print_ok("No browser needed for the first checks - HTTP answers directly.")
        print()

        announced_retry = False
        for username in source:
            _raise_if_stopping()
            if source.in_retry_pass and not announced_retry:
                announced_retry = True
                print()
                print_info("Retrying {} username(s) that timed out...".format(len(source.deferred)))
            stats.index += 1
            results, notes, errored_sites = [], [], []
            queued_webhook = any_error = checked_any = False

            outcomes = []
            pending = []
            for site in sites:
                if filter_premium and site.premium and is_premium_alias(username):
                    outcomes.append((site, "skipped"))
                else:
                    pending.append(site)

            resolved_names = set()
            http_sites = []
            if engine is not None and engine.enabled:
                http_sites = [site for site in pending if site.name in engine.probes]
            if http_sites:
                with ThreadPoolExecutor(max_workers=min(engine.pacer.workers, len(http_sites))) as pool:
                    futures = {pool.submit(engine.check, site, username): site for site in http_sites}
                    for future, site in futures.items():
                        _raise_if_stopping()
                        probe = future.result()
                        stats.site_checks += 1
                        if probe.verdict in ("unclaimed", "claimed"):
                            stats.http_checks += 1
                            outcomes.append((site, probe.verdict))
                            resolved_names.add(site.name)
                        elif probe.verdict == "ratelimited":
                            # Chrome would meet the same 429; don't hammer the site.
                            outcomes.append((site, "error"))
                            resolved_names.add(site.name)
                            stats.http_checks += 1
            pending = [site for site in pending if site.name not in resolved_names]

            for site in pending:
                if browser is None and ensure_browser() is None:
                    outcomes.append((site, "error"))
                    continue
                if rotator is not None:
                    rotator.rotate()  # spread every request across all proxies
                result = check_username(browser, username, rotator, site=site)
                stats.site_checks += 1
                stats.browser_checks += 1
                if engine is not None:
                    engine.pacer.record(site.name, result,
                                        engine.pacer.state(site.name).average_latency)
                outcomes.append((site, result))

            order = {id(site): index for index, site in enumerate(sites)}
            outcomes.sort(key=lambda pair: order.get(id(pair[0]), 0))
            checked_any = any(result not in ("skipped",) for _, result in outcomes)
            for site, result in outcomes:
                if result == "skipped":
                    stats.skipped += 1
                    stats.count(site.name, "skipped")
                    results.append((site, result))
                    continue
                if result == "unclaimed":
                    stats.unclaimed += 1
                    stats.count(site.name, result)
                    if hits is not None:
                        key = site.display(username) if len(sites) > 1 else username
                        outcome = hits.add(key)
                        notes.append({"saved": "saved", "duplicate": "already saved",
                                      "failed": "could not write unclaimed.txt"}[outcome])
                    if notifier is not None:
                        notifier.notify(username, site)
                        if not queued_webhook:
                            notes.append("webhook queued")
                            queued_webhook = True
                elif result == "claimed":
                    stats.claimed += 1
                    stats.count(site.name, result)
                else:
                    any_error = True
                    errored_sites.append(site)
                results.append((site, result))

            notes = list(dict.fromkeys(notes))  # "saved" once even when several sites hit
            if checked_any and username not in stats.seen:
                stats.seen.add(username)
                stats.checked += 1
            deferred = any_error and source.defer(username)
            if any_error and not deferred:  # errors that will not get another chance
                for site in errored_sites:
                    stats.errors += 1
                    stats.count(site.name, "error")
            if deferred:
                notes.append("will retry at the end")
            print_site_row(stats.index, username, results, ", ".join(notes))
            update_title(stats)
            if engine is None or not engine.enabled:
                _sleep_between_checks(interval, bool(proxies))
            elif interval > 0:
                _sleep(interval)

        print()
        if source.is_custom:
            print("{}Customlist check completed. {} usernames checked.{}".format(Fore.CYAN, stats.checked, Fore.RESET))
        elif source.exhausted:
            print("{}All {} possible usernames with {} characters have been checked.{}".format(
                Fore.CYAN, source.total, letter_count, Fore.RESET))
    except KeyboardInterrupt:
        print("\n{}Program terminated.{}".format(Fore.YELLOW, Fore.RESET))
    except BrowserLaunchError as exc:
        print_error("Chrome stopped working: {}".format(exc))
        print_warn(_launch_hint(str(exc)))
    finally:
        _run_active.clear()
        if browser is not None:
            browser.quit()
        if notifier is not None:
            if notifier.pending:
                print_info("Sending {} pending Discord notification(s)...".format(notifier.pending))
            notifier.close()
        if rotator is not None:
            rotator.stop()
        try:
            print_summary(stats, hits, notifier, rotator)
        except Exception:
            pass
    return stats


# =============================================================================
#  Prompts
# =============================================================================

class InputClosed(Exception):
    """stdin was closed (EOF) while waiting for an answer."""


def get_input(prompt, type_=str, validation=None, error_msg="Please enter a valid value.", error=None):
    """Asks a question below the persistent header until the answer is valid.

    The error message now stays visible after the screen refresh, and EOF on
    stdin ends the program instead of looping forever.
    """
    while True:
        show_header()
        if error:
            print("  {}[x] {}{}\n".format(Fore.RED, error, Fore.RESET))
        try:
            value = input("  {}[?]{} {}".format(PURPLE, RESET, prompt)).strip()
        except EOFError:
            raise InputClosed() from None
        if type_ is bool:
            answer = value.lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            error = error_msg
            continue
        try:
            converted = type_(value)
        except (TypeError, ValueError):
            error = error_msg
            continue
        if validation is not None and not validation(converted):
            error = error_msg
            continue
        return converted


def _ask_customlist():
    try:
        names, invalid, duplicates = load_customlist()
    except FileNotFoundError:
        UI.note("error", "customlist.txt not found. Switching to random mode.")
        return None
    except OSError as exc:
        UI.note("error", "Could not read customlist.txt ({}). Switching to random mode.".format(exc))
        return None
    if invalid:
        preview = ", ".join(invalid[:3]) + (" ..." if len(invalid) > 3 else "")
        UI.note("warn", "Ignored {} invalid entr{} (allowed: a-z 0-9 . _ -): {}".format(
            len(invalid), "y" if len(invalid) == 1 else "ies", preview))
    if duplicates:
        UI.note("info", "Removed {} duplicate username(s).".format(duplicates))
    if not names:
        UI.note("warn", "customlist.txt is empty. Switching to random mode.")
        return None
    UI.note("ok", "{} usernames loaded from customlist.txt.".format(len(names)))
    return names


def _parse_indices(answer, count, empty_means_all=False):
    """'1, 3-4' -> [0, 2, 3]; 'A' -> every index; 'N' -> none."""
    text = (answer or "").strip().lower()
    if text in ("a", "all", "*"):
        return list(range(count))
    if not text:
        return list(range(count)) if empty_means_all else []
    if text in ("n", "none"):
        return []
    chosen = []
    for part in re.split(r"[\s,]+", text):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            try:
                start, end = int(first), int(last)
            except ValueError:
                raise ValueError("not a number") from None
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ValueError("not a number") from None
        if start > end:
            start, end = end, start
        for number in range(start, end + 1):
            if not 1 <= number <= count:
                raise ValueError("out of range")
            if number - 1 not in chosen:
                chosen.append(number - 1)
    return sorted(chosen)


def _menu(title, options, settings):
    """Draws a numbered menu under the header. Returns the chosen number or None."""
    show_header()
    print("  {}{}{}".format(PURPLE, "=" * 14 + " " + title + " " + "=" * 14, RESET))
    for number, label in options:
        print("   {}[{}]{} {}".format(PURPLE, number, RESET, label))
    print("  {}{}{}".format(PURPLE, "=" * (30 + len(title)), RESET))
    try:
        answer = input("  {}[?]{} Choose an option: ".format(PURPLE, RESET)).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        number = int(answer)
    except ValueError:
        return None
    if not any(number == option[0] for option in options):
        return None
    return number


def _print_saved_webhooks(saved, site_names=None):
    for position, entry in enumerate(saved, start=1):
        route = entry.get("route") or (ALL_SITES_TOKEN,)
        if site_names and ALL_SITES_TOKEN not in route:
            route = tuple(name for name in route if name in site_names) or route
        UI.note("info", "{}) {:<14} {:<44} -> {}".format(
            position, entry["label"], webhook_mask(entry["url"]), _route_label(route)))


def _choose_route(site_names, default=None):
    """Asks which sites a webhook should receive hits from."""
    print("   {}Which sites should this webhook receive?{}".format(LIGHT_PURPLE, RESET))
    for number, name in enumerate(site_names, start=1):
        marked = "*" if (default is None or ALL_SITES_TOKEN in default or name in default) else " "
        print("     {}[{}]{} {}{}".format(marked, number, RESET, name, RESET))
    answer = input("  {}[?]{} Sites (1,2 / A = all / Enter = all): ".format(PURPLE, RESET)).strip()
    try:
        picked = _parse_indices(answer, len(site_names), empty_means_all=True)
    except ValueError:
        return (ALL_SITES_TOKEN,)
    if len(picked) == len(site_names):
        return (ALL_SITES_TOKEN,)
    return tuple(site_names[index] for index in picked)


def _manage_webhooks(settings=None):
    """Add / route / remove / test saved webhooks. Lives in Settings."""
    sites, _problems = load_sites()
    site_names = [site.name for site in sites]
    while True:
        saved, invalid = load_saved_webhooks()
        if invalid:
            UI.note("warn", "webhooks.txt has {} invalid line(s); they are ignored.".format(invalid))
        if saved:
            _print_saved_webhooks(saved, site_names)
        else:
            UI.note("info", "No webhooks saved yet. Add one and it is offered on every run.")
        choice = get_input("Webhook manager: 1) add  2) change routing  3) remove  4) test  5) back: ",
                           type_=int, validation=lambda x: 1 <= x <= 5, error_msg="Choose 1, 2, 3, 4 or 5.")
        if choice == 1:
            url = get_input("New Discord webhook URL (or N to cancel): ",
                            validation=lambda x: x.lower() in ("n", "no") or is_valid_webhook_url(x),
                            error_msg="Enter a URL starting with https://discord.com/api/webhooks/")
            if url.lower() in ("n", "no"):
                continue
            label = get_input("Name for this webhook (optional): ")
            route = _choose_route(site_names)
            print_info("Verifying with Discord...")
            status, detail = verify_webhook(url)
            if status == "invalid":
                UI.note("warn", "Discord rejected that webhook ({}). Nothing saved.".format(detail))
                continue
            try:
                outcome = save_webhook(url, label, path=WEBHOOKS_FILE, route=route)
            except (OSError, ValueError) as exc:
                UI.note("warn", "Could not save the webhook: {}".format(exc))
                continue
            UI.note("ok", "Saved for {}. It is offered automatically from now on.".format(_route_label(route))
                    if outcome == "saved" else "That webhook is already saved.")
        elif choice == 2:
            if not saved:
                UI.note("info", "There are no saved webhooks to re-route.")
                continue
            picked = get_input("Re-route which? (numbers, A = all, N = cancel): ",
                               type_=lambda text: _parse_indices(text, len(saved)),
                               error_msg="Enter numbers like 1,3 or A.")
            if not picked:
                continue
            for index in picked:
                UI.note("info", "{} currently -> {}".format(saved[index]["label"],
                                                            _route_label(saved[index]["route"])))
                route = _choose_route(site_names, saved[index]["route"])
                set_webhook_route(saved[index]["url"], route)
                UI.note("ok", "{} now receives hits from {}.".format(saved[index]["label"],
                                                                     _route_label(route)))
        elif choice == 3:
            if not saved:
                UI.note("info", "There are no saved webhooks to remove.")
                continue
            picked = get_input("Remove which? (numbers, A = all, N = cancel): ",
                               type_=lambda text: _parse_indices(text, len(saved)),
                               error_msg="Enter numbers like 1,3 or A.")
            if not picked:
                continue
            removed = remove_saved_webhooks([saved[index]["url"] for index in picked])
            UI.note("ok", "Removed {} saved webhook(s).".format(removed))
        elif choice == 4:
            if not saved:
                UI.note("info", "There are no saved webhooks to test.")
                continue
            print_info("Testing {} saved webhook(s)...".format(len(saved)))
            for entry, status, detail in verify_webhooks(saved):
                UI.note("ok" if status == "ok" else "warn" if status == "unknown" else "error",
                        "{}: {} ({})".format(entry["label"], "working" if status == "ok" else status, detail))
        else:
            return


# ------------------------------------------------------------------ settings UI

def _settings_menu(settings):
    """The Settings screen: intro animation, theme, defaults and the webhook manager."""
    while True:
        sites, _problems = load_sites()
        options = [
            (1, "Intro animation ....... {}".format(INTRO_ANIMATIONS[settings["intro"]]["label"])),
            (2, "Colour theme .......... {}".format(THEMES[settings["theme"]]["label"])),
            (3, "Default sites ......... {}".format(", ".join(settings["sites"]) or "guns.lol")),
            (4, "Random name length .... {}".format(settings["letter_count"])),
            (5, "Delay ................. {}".format("{:g}s".format(settings["delay"]) if settings["delay"] > 0 else "Auto")),
            (6, "Premium filter ........ {}".format("On" if settings["filter_premium"] else "Off")),
            (7, "Save hits ............. {}".format("unclaimed.txt" if settings["save_to_file"] else "Off")),
            (8, "Use proxies ........... {}".format("On" if settings["use_proxies"] else "Off")),
            (9, "Webhook manager ....... {} saved".format(len(load_saved_webhooks()[0]))),
            (10, "Speed mode ............ {}".format(fastpath.MODE_LABELS.get(settings["speed"], settings["speed"]))),
            (11, "HTTP fast path ........ {}".format("On" if settings["use_http"] else "Off (browser only)")),
            (12, "Reset to defaults"),
            (0, "Back to the main menu"),
        ]
        choice = _menu("SETTINGS", options, settings)
        if choice is None:
            UI.note("warn", "Left the settings menu without saving.")
            return
        if choice == 0:
            return
        if choice == 1:
            settings["intro"] = _ask_animation(settings["intro"])
        elif choice == 2:
            settings["theme"] = _ask_theme(settings["theme"])
        elif choice == 3:
            settings["sites"] = _ask_default_sites(sites, settings["sites"])
        elif choice == 4:
            settings["letter_count"] = get_input(
                "Random username length (1-{}): ".format(MAX_USERNAME_LENGTH), type_=int,
                validation=lambda x: 1 <= x <= MAX_USERNAME_LENGTH,
                error_msg="Enter a whole number between 1 and {}.".format(MAX_USERNAME_LENGTH))
        elif choice == 5:
            settings["delay"] = get_input(
                "Delay in seconds (0 = automatic): ", type_=float,
                validation=lambda x: math.isfinite(x) and 0 <= x <= 3600,
                error_msg="Enter a number of seconds between 0 and 3600.")
        elif choice == 6:
            settings["filter_premium"] = get_input(
                "Filter guns.lol premium aliases? (Y/N): ", type_=bool, error_msg="Please enter Y or N.")
        elif choice == 7:
            settings["save_to_file"] = get_input(
                "Save unclaimed usernames to unclaimed.txt? (Y/N): ", type_=bool,
                error_msg="Please enter Y or N.")
        elif choice == 8:
            settings["use_proxies"] = get_input(
                "Rotate proxies from proxies.txt? (Y/N): ", type_=bool, error_msg="Please enter Y or N.")
            if settings["use_proxies"]:
                try:
                    count = len(load_proxies())
                    UI.note("ok", "{} proxies loaded from proxies.txt.".format(count))
                except FileNotFoundError:
                    settings["use_proxies"] = False
                    UI.note("warn", "proxies.txt not found, so proxies stay off.")
                except (OSError, ValueError) as exc:
                    settings["use_proxies"] = False
                    UI.note("warn", "proxies.txt could not be used: {}".format(exc))
        elif choice == 9:
            _manage_webhooks(settings)
        elif choice == 10:
            settings["speed"] = _ask_speed(settings["speed"])
        elif choice == 11:
            settings["use_http"] = get_input(
                "Use the plain-HTTP fast path where a site allows it? (Y/N): ", type_=bool,
                error_msg="Please enter Y or N.")
            UI.note("ok", "Fast path {}.".format("on" if settings["use_http"] else "off"))
        elif choice == 12:
            settings.update({key: value for key, value in DEFAULT_SETTINGS.items()})
            apply_theme(settings["theme"])
            UI.note("info", "Settings reset to defaults.")
        if save_settings(settings):
            UI.note("ok", "Saved to settings.json.")


def _ask_animation(current):
    """Pick and preview an intro animation."""
    names = list(INTRO_ANIMATIONS)
    while True:
        show_header()
        print("  {}{}{}".format(PURPLE, "=" * 12 + " INTRO ANIMATION " + "=" * 12, RESET))
        for number, name in enumerate(names, start=1):
            entry = INTRO_ANIMATIONS[name]
            marker = "{} <- current".format(RESET) if name == current else RESET
            print("   {}[{}]{} {:<24} {}".format(PURPLE, number, RESET, entry["label"], marker))
        print("   {}[P]{} Preview the current choice".format(PURPLE, RESET))
        print("  {}{}{}".format(PURPLE, "=" * 43, RESET))
        answer = input("  {}[?]{} Choose (number / P / Enter to keep): ".format(PURPLE, RESET)).strip()
        if not answer:
            return current
        if answer.lower() == "p":
            preview_animation(current)
            continue
        try:
            number = int(answer)
        except ValueError:
            continue
        if not 1 <= number <= len(names):
            continue
        chosen = names[number - 1]
        preview_animation(chosen)
        UI.note("ok", "Intro animation: {}".format(INTRO_ANIMATIONS[chosen]["label"]))
        return chosen


def _ask_theme(current):
    """Pick a CLI colour theme (also previews the header)."""
    names = list(THEMES)
    chosen = current
    while True:
        show_header()
        print("  {}{}{}".format(PURPLE, "=" * 12 + " COLOUR THEME " + "=" * 12, RESET))
        for number, name in enumerate(names, start=1):
            print("   {}[{}]{} {}{}".format(PURPLE, number, RESET, THEMES[name]["label"],
                                            "  <- current" if name == chosen else ""))
        print("  {}{}{}".format(PURPLE, "=" * 40, RESET))
        answer = input("  {}[?]{} Choose (number / Enter to keep): ".format(PURPLE, RESET)).strip()
        if not answer:
            return chosen
        try:
            number = int(answer)
        except ValueError:
            continue
        if not 1 <= number <= len(names):
            continue
        chosen = names[number - 1]
        apply_theme(chosen)
        UI.note("ok", "Colour theme: {}".format(THEMES[chosen]["label"]))
        return chosen


def _ask_default_sites(sites, current):
    """Pick the sites that are pre-selected on every run."""
    show_header()
    print("  {}Default sites (pre-selected on every run){}".format(LIGHT_PURPLE, RESET))
    for number, site in enumerate(sites, start=1):
        marked = "*" if site.name in current else " "
        print("   {}[{}]{} {:<11} {}".format(marked, number, RESET, site.label,
                                             site.url.replace("{username}", "<name>")))
    answer = input("  {}[?]{} Sites (1,2 / A = all / Enter to keep): ".format(PURPLE, RESET)).strip()
    if not answer:
        return list(current)
    try:
        picked = _parse_indices(answer, len(sites), empty_means_all=True)
    except ValueError:
        UI.note("warn", "Could not read that answer, default sites unchanged.")
        return list(current)
    chosen = [sites[index].name for index in picked]
    UI.note("ok", "Default sites: {}".format(", ".join(chosen)))
    return chosen


# -------------------------------------------------------------- list generator

def _print_pattern_table():
    groups = OrderedDict()
    for pattern in listgen.PATTERNS:
        groups.setdefault(pattern["group"], []).append(pattern)
    number = 0
    for group, patterns in groups.items():
        print("  {}{}{}".format(LIGHT_PURPLE, "-- " + group + " " + "-" * (46 - len(group)), RESET))
        for pattern in patterns:
            number += 1
            print("   {}[{}]{} {:<24} {}".format(PURPLE, number, RESET, pattern["label"], pattern["help"]))
    print("   {}[0]{} Cancel".format(PURPLE, RESET))
    return number


def _choose_pattern():
    """Returns the selected pattern code, or None when cancelled."""
    while True:
        show_header()
        print("  {}{}{}".format(PURPLE, "=" * 12 + " LIST GENERATOR " + "=" * 12, RESET))
        _print_pattern_table()
        print("  {}{}{}".format(PURPLE, "=" * 47, RESET))
        answer = input("  {}[?]{} Pick a pattern (number or name, e.g. 3L): ".format(PURPLE, RESET)).strip()
        if answer in ("0", "", "q", "back"):
            return None
        pattern = listgen.resolve_pattern(answer)
        if pattern is None and answer.isdigit() and 1 <= int(answer) <= len(listgen.PATTERNS):
            pattern = listgen.PATTERNS[int(answer) - 1]
        if pattern is None:
            UI.note("warn", "Unknown pattern. Try a number from the list or a name like 3L.")
            continue
        return pattern["code"]


def _preview(names, count=listgen.PREVIEW_COUNT):
    print("   {}Preview ({} of {}):{}".format(LIGHT_PURPLE, min(count, len(names)), len(names), RESET))
    for name in names[:count]:
        print("     {}{}".format(MUTED, name))


def list_generator_menu(settings):
    """Generate customlist.txt contents from patterns and bundled word lists."""
    target = os.path.join(BASE_DIR, "customlist.txt")
    while True:
        code = _choose_pattern()
        if code is None:
            return
        pattern = listgen.resolve_pattern(code)
        filter_premium = get_input(
            "Skip guns.lol premium aliases (start/end with . _ -)? (Y/N): ",
            type_=bool, error_msg="Please enter Y or N.")
        print_info("Generating {} ...".format(pattern["label"]))
        try:
            result = listgen.generate(code, limit=listgen.DEFAULT_LIMIT, filter_premium=filter_premium)
        except ValueError as exc:
            UI.note("error", str(exc))
            continue
        for note in result.notes:
            UI.note("warn", note)
        if not result.count:
            UI.note("error", "No names were produced.")
            continue
        show_header()
        print("  {}{}{}   {}{}{}".format(PURPLE, pattern["label"], RESET, LIGHT_PURPLE,
                                         "{} line(s)".format(result.count), RESET))
        _preview(result.names)
        if result.truncated:
            UI.note("warn", "Output was cut at {} lines; split it into several files if you need more.".format(
                listgen.DEFAULT_LIMIT))
        options = [(1, "Replace customlist.txt"), (2, "Append to customlist.txt"),
                   (3, "Write to a new file"), (4, "Preview more"), (0, "Back")]
        choice = _menu("WRITE LIST", options, settings)
        if choice == 0 or choice is None:
            continue
        if choice == 4:
            show_header()
            _preview(result.names, count=100)
            _enter_pressed(2.0)
            continue
        if choice == 3:
            filename = get_input("File name (e.g. list-3L.txt): ",
                                 validation=lambda x: bool(re.match(r"^[\w .-]{1,64}\.txt$", x.strip())),
                                 error_msg="Enter a .txt file name.")
            target = os.path.join(BASE_DIR, os.path.basename(filename.strip()))
        try:
            mode, lines = listgen.write_list(
                result.names, target, "append" if choice == 2 else "replace")
        except OSError as exc:
            UI.note("error", "Could not write {}: {}".format(target, exc))
            continue
        UI.note("ok", "{} {} with {} line(s) at {}.".format(
            "Appended to" if mode == "appended" else "Wrote", os.path.basename(target), lines,
            datetime.now().strftime("%H:%M:%S")))
        UI.note("info", "Answer 'Use customlist.txt? Y' on the next run to check them.")
        if not get_input("Generate another list? (Y/N): ", type_=bool, error_msg="Please enter Y or N."):
            return


# ----------------------------------------------------------------- webhook flow

def _ask_webhooks(site_names):
    """Select saved webhooks for this run (adding/routing happens in Settings)."""
    saved, invalid = load_saved_webhooks()
    if invalid:
        UI.note("warn", "Ignored {} invalid line(s) in webhooks.txt.".format(invalid))
    usable = [entry for entry in saved if any(
        site in entry["route"] or ALL_SITES_TOKEN in entry["route"] for site in site_names)]
    if not saved:
        UI.note("info", "No webhooks saved yet - open the menu's Settings > Webhook manager to add one.")
        UI.setting("Discord", "Off")
        return []
    if not usable:
        UI.note("warn", "None of the saved webhooks is routed to {}. Open Settings > Webhook manager.".format(
            ", ".join(site_names)))
        UI.setting("Discord", "Off")
        return []

    def choose(text):
        picked = _parse_indices(text, len(usable))
        if not picked:
            raise ValueError("no webhook selected")
        return [usable[index] for index in picked]

    _print_saved_webhooks(usable, site_names)
    selected = get_input("Use which webhooks? (1,3 / A = all / N = none): ", type_=choose,
                         error_msg="Enter numbers like 1,3, A for all, or N for none.")
    UI.note("ok", "{} webhook(s) selected.".format(len(selected)))
    UI.setting("Discord", "{} webhook(s) selected".format(len(selected)))
    return selected


# ------------------------------------------------------------------- prompt flow

def _ask_sites():
    sites, problems = load_sites()
    for problem in problems:
        UI.note("warn", problem)
    for position, site in enumerate(sites, start=1):
        tags = ["premium aliases" if site.premium else "standard"]
        if not site.builtin:
            tags.append("custom")
        UI.note("info", "{}) {:<11} {}  ({})".format(
            position, site.label, site.url.replace("{username}", "<name>"), ", ".join(tags)))

    def choose(text):
        picked = _parse_indices(text, len(sites), empty_means_all=True)
        if not picked:
            raise ValueError("no site selected")
        return [sites[index] for index in picked]

    selected = get_input("Check which sites? (1,2 / A = all / Enter = all): ", type_=choose,
                         error_msg="Choose one or more numbers, or A for all.")
    UI.setting("Sites", ", ".join(site.label for site in selected))
    return selected


def _apply_saved_settings(settings, sites):
    """Builds check_user_status() kwargs from the saved settings."""
    proxies = None
    if settings["use_proxies"]:
        try:
            proxies = load_proxies()
            UI.note("ok", "{} proxies loaded from proxies.txt.".format(len(proxies)))
        except FileNotFoundError:
            UI.note("warn", "proxies.txt not found - continuing without proxies.")
        except (OSError, ValueError) as exc:
            UI.note("warn", "proxies.txt ignored: {}".format(exc))
    customlist = None
    if settings["use_customlist"]:
        try:
            customlist = _ask_customlist()
        except Exception:
            customlist = None
    webhook_urls = _ask_webhooks([site.name for site in sites])
    letter_count = None if customlist else settings["letter_count"]
    return {
        "letter_count": letter_count,
        "interval": settings["delay"],
        "customlist": customlist,
        "filter_premium": settings["filter_premium"],
        "save_to_file": settings["save_to_file"],
        "webhook_urls": webhook_urls,
        "proxies": proxies,
        "sites": sites,
        "speed": settings["speed"],
        "use_http": settings["use_http"],
    }


def _ask_speed(current):
    """Pick the checking speed profile."""
    modes = list(fastpath.PROFILES)
    show_header()
    print("  {}{}{}".format(PURPLE, "=" * 12 + " SPEED MODE " + "=" * 12, RESET))
    for number, mode in enumerate(modes, start=1):
        marker = "  <- current" if mode == current else ""
        print("   {}[{}]{} {}{}".format(PURPLE, number, RESET, fastpath.MODE_LABELS[mode], marker))
    print("  {}{}{}".format(PURPLE, "=" * 38, RESET))
    print("   {}Gaps adapt automatically when a site pushes back (429 / challenge).{}".format(MUTED, RESET))
    answer = input("  {}[?]{} Choose (number / Enter to keep): ".format(PURPLE, RESET)).strip()
    if not answer:
        return current
    try:
        number = int(answer)
    except ValueError:
        return current
    if not 1 <= number <= len(modes):
        return current
    chosen = modes[number - 1]
    UI.note("ok", "Speed mode: {}".format(chosen))
    return chosen


def run_benchmark(settings, sites):
    """Polite, bounded pace measurement for the selected sites."""
    show_header()
    print("  {}{}{}".format(PURPLE, "=" * 10 + " RATE BENCHMARK " + "=" * 10, RESET))
    print("   {}A bounded probe (max 8 requests per site). It starts slowly and stops at{}".format(MUTED, RESET))
    print("   {}the first 429 / block, so a site is never flooded.{}".format(MUTED, RESET))
    answer = input("  {}[?]{} Which site? (numbers, A = all, Enter = back): ".format(PURPLE, RESET)).strip()
    if not answer:
        return
    try:
        picked = _parse_indices(answer, len(sites), empty_means_all=True)
    except ValueError:
        UI.note("warn", "Could not read that answer.")
        return
    pacer = fastpath.Pacer("steady")
    for index in picked:
        site = sites[index]
        print_info("Benchmarking {} ...".format(site.name))
        try:
            report = fastpath.benchmark(pacer, site, max_requests=8,
                                        callback=lambda line: print("   {}{}{}".format(MUTED, line, RESET)))
        except Exception as exc:
            UI.note("error", "{}: {}".format(site.name, exc))
            continue
        for line in report.lines():
            UI.note("ok" if report.clean else "warn", line)
        UI.note("info", fastpath.tune_advice(report))
        if not get_input("Apply this pace to the site for this session? (Y/N): ",
                         type_=bool, error_msg="Please enter Y or N."):
            continue
        settings.setdefault("benchmarks", {})[site.name] = report.safe_interval
        UI.note("ok", "{}: {}s minimum gap saved for this session.".format(
            site.name, round(report.safe_interval, 2)))


def collect_settings(settings=None):
    """Prompt flow for one run. `settings` carries the saved defaults."""
    settings = settings or load_settings()
    UI.reset()
    yes_no = "Please enter Y or N."
    apply_theme(settings["theme"])
    sites = _ask_sites()

    customlist = None
    if get_input("Use customlist.txt? (Y/N): ", type_=bool, error_msg=yes_no):
        customlist = _ask_customlist()
    letter_count = None
    if customlist:
        UI.setting("Mode", "Custom list ({} usernames)".format(len(customlist)))
    else:
        UI.setting("Mode", "Random usernames")
        letter_count = get_input(
            "Enter username length (e.g. 3, 4, 5): ", type_=int,
            validation=lambda x: 1 <= x <= MAX_USERNAME_LENGTH,
            error_msg="Please enter a whole number between 1 and {}.".format(MAX_USERNAME_LENGTH))
        UI.setting("Length", "{} characters".format(letter_count))

    interval = get_input(
        "Delay (in seconds *recommended 0*): ", type_=float,
        validation=lambda x: math.isfinite(x) and 0 <= x <= 3600,
        error_msg="Please enter a number of seconds between 0 and 3600.")
    UI.setting("Delay", "{:g}s".format(interval) if interval > 0 else "Auto")

    proxies, error = None, None
    while get_input("Use proxies.txt? (Y/N): ", type_=bool, error_msg=yes_no, error=error):
        try:
            proxies = load_proxies()
            break
        except FileNotFoundError:
            error = "proxies.txt not found. Create it next to gunslol.py, or answer N."
        except (OSError, ValueError) as exc:
            error = "Could not load proxies.txt: {}".format(exc)
    if proxies:
        UI.setting("Proxies", "{} loaded, rotating".format(len(proxies)))
        UI.note("ok", "{} proxies loaded. Requests will rotate across them.".format(len(proxies)))
    else:
        UI.setting("Proxies", "Off")
    if interval == 0:
        UI.setting("Delay", "Auto ({} jitter)".format("0.25-0.45s" if proxies else "0.8-1.2s"))

    if any(site.premium for site in sites):
        filter_premium = get_input(
            "Filter out usernames requiring Premium? (Starts/ends with '.', '-', '_') [Y/N]: ",
            type_=bool, error_msg=yes_no)
        UI.setting("Premium filter", "On" if filter_premium else "Off")
    else:
        filter_premium = False
        UI.setting("Premium filter", "n/a (no premium aliases)")

    save_to_file = get_input("Should unclaimed usernames be saved to unclaimed.txt? (Y/N): ",
                             type_=bool, error_msg=yes_no)
    UI.setting("Save hits", "unclaimed.txt" if save_to_file else "Off")

    webhook_urls = _ask_webhooks([site.name for site in sites])

    # Remember the answers so the main menu can offer "start with saved settings".
    settings.update({"sites": [site.name for site in sites], "delay": interval,
                     "letter_count": letter_count or settings["letter_count"],
                     "filter_premium": filter_premium, "save_to_file": save_to_file,
                     "use_proxies": bool(proxies), "use_customlist": bool(customlist)})
    save_settings(settings)

    return {
        "letter_count": letter_count,
        "interval": interval,
        "customlist": customlist,
        "filter_premium": filter_premium,
        "save_to_file": save_to_file,
        "webhook_urls": webhook_urls,
        "proxies": proxies,
        "sites": sites,
        "speed": settings["speed"],
        "use_http": settings["use_http"],
    }


def help_screen():
    """Short help / about text, reachable from the main menu."""
    show_header()
    print("  {}{}{}".format(PURPLE, "=" * 16 + " HELP " + "=" * 16, RESET))
    lines = [
        ("What it does", "Checks bio-link profile names on several sites and reports which are free."),
        ("Sites", "guns.lol, haunt.gg and jugs.lol are built in; add more in sites.json."),
        ("Lists", "Fill customlist.txt by hand or generate it in the menu (List generator)."),
        ("Webhooks", "Save one webhook per channel in webhooks.txt and route it per site."),
        ("Files", "All files live next to gunslol.py: settings.json, sites.json, webhooks.txt."),
        ("Stop", "Ctrl+C stops the run, closes Chrome and prints a summary."),
        ("Credits", "Original tool by efekrbas; filtering ideas by noivan0. MIT licensed."),
    ]
    for label, text in lines:
        print("   {}{}{} {}".format(MUTED, (label + " ").ljust(12, "."), RESET, text))
    print("  {}{}{}".format(PURPLE, "=" * 38, RESET))
    input("  {}[?]{} Press Enter to go back: ".format(PURPLE, RESET))


def main_menu(settings):
    """The selectable intro screen. Returns the chosen action name."""
    if not (_is_tty(sys.stdout) and _is_tty(sys.stdin)):
        return "run"  # piped input: behave like the classic script
    saved, _ = load_saved_webhooks()
    options = [
        (1, "Start checking"),
        (2, "Start with saved settings ({} sites, {} delay)".format(
            len(settings["sites"]), "{:g}s".format(settings["delay"]) if settings["delay"] > 0 else "auto")),
        (3, "Settings"),
        (4, "Webhook manager ({} saved)".format(len(saved))),
        (5, "List generator for customlist.txt"),
        (6, "Rate benchmark (polite, bounded)"),
        (7, "Help / About"),
        (0, "Exit"),
    ]
    choice = _menu("MAIN MENU", options, settings)
    return {1: "run", 2: "run-saved", 3: "settings", 4: "webhooks", 5: "lists",
            6: "benchmark", 7: "help", 0: "exit", None: "exit"}[choice]


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except Exception:
                pass
    if os.name == "nt":
        try:
            System.Init()  # enables ANSI colors in classic Windows consoles
        except Exception:
            pass
    colorama_init(autoreset=True)
    _install_signal_handlers()
    if os.name != "nt" and _is_tty(sys.stdout):
        sys.stdout.write("\033[22;0t")  # save the terminal title (restored on exit)


def _start_run(settings, use_saved=False):
    """Runs the checker, either from prompts or straight from saved settings."""
    show_header()
    if use_saved:
        sites, _problems = load_sites()
        wanted = [site for site in sites if site.name in settings["sites"]] or [sites[0]]
        UI.setting("Sites", ", ".join(site.label for site in wanted))
        UI.setting("Mode", "Saved settings")
        options = _apply_saved_settings(settings, wanted)
        check_user_status(**options)
        return
    check_user_status(**collect_settings(settings))


def main():
    configure_console()
    settings = load_settings()
    apply_theme(settings["theme"])
    update_title()
    exit_code = 0
    try:
        play_intro(settings["intro"])
        while True:
            action = main_menu(settings)
            if action == "exit":
                break
            if action == "run":
                _start_run(settings)
                break
            if action == "run-saved":
                _start_run(settings, use_saved=True)
                break
            if action == "settings":
                _settings_menu(settings)
                apply_theme(settings["theme"])
            elif action == "webhooks":
                _manage_webhooks(settings)
            elif action == "lists":
                list_generator_menu(settings)
            elif action == "benchmark":
                sites, _problems = load_sites()
                run_benchmark(settings, sites)
            elif action == "help":
                help_screen()
    except KeyboardInterrupt:
        exit_code = 130
        print("\n{}Program terminated.{}".format(Fore.YELLOW, Fore.RESET))
    except InputClosed:
        exit_code = 1
        print("\n{}Input closed. Program terminated.{}".format(Fore.YELLOW, Fore.RESET))
    except Exception as exc:
        exit_code = 1
        print("{}An unexpected error occurred: {}{}".format(Fore.RED, exc, Fore.RESET))
        print("{}{}{}".format(MUTED, traceback.format_exc().rstrip(), RESET))
    finally:
        save_settings(settings)
        _cleanup()
        try:
            if _is_tty(sys.stdout):
                Cursor.ShowCursor()
                if os.name != "nt":
                    sys.stdout.write("\033[23;0t")  # restore the terminal title
                    sys.stdout.flush()
        except Exception:
            pass
        if _launched_by_double_click:
            print("\n{}Press any key to exit...{}".format(Fore.CYAN, Fore.RESET))
            try:
                os.system("pause >nul")
            except BaseException:
                pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
