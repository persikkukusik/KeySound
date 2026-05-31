#!/usr/bin/env python3
"""
KeySound — global keyboard + mouse sound player with system tray GUI.
Requires:  pip install evdev pygame PyQt6
           sudo usermod -aG input $USER  (then re-login)
"""

import sys
import json
import asyncio
import threading
import math
from pathlib import Path

import pygame
from evdev import InputDevice, ecodes, list_devices
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit, QFileDialog, QSystemTrayIcon,
    QMenu, QFrame, QCheckBox, QSlider, QListWidget, QListWidgetItem,
    QGroupBox, QComboBox, QScrollArea,
)
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QAction



# ── Autostart helpers ─────────────────────────────────────────────────────────
import subprocess as _sp

AUTOSTART_DIR  = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "keysound.desktop"
SCRIPT_PATH    = Path(__file__).resolve()

DESKTOP_ENTRY = f"""[Desktop Entry]
Type=Application
Name=KeySound
Exec=python3 {SCRIPT_PATH}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=KeySound keyboard sound player
"""

def autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()

def set_autostart(enable: bool) -> None:
    if enable:
        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(DESKTOP_ENTRY)
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "keysound_config.json"

DEFAULT_CONFIG = {
    "press_folder":        str(SCRIPT_DIR / "sounds"),
    "release_folder":      "",
    "enabled":             True,
    "volume":              100,
    "blacklist":           [],
    "overrides":           {},
    # mouse
    "mouse_btn_enabled":      True,
    "mouse_move_sound":       "",
    "mouse_move_multiplier":  0.5,
    "mouse_move_vol_smooth":  0.15,   # 0.0 = instant, 1.0 = very slow fade
    # pitch
    "pitch_multiplier":       1.0,    # overall pitch scale (0.5 – 2.0)
    "pitch_random_offset":    0.0,    # ± random offset applied each play
    # pan
    "pan_strength":           0.0,    # 0 = off, 1 = full pan from mouse X position
    "pan_mouse_only":         False,  # True = pan only mouse buttons + move sound, not keyboard keys
}


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ── Audio helpers ─────────────────────────────────────────────────────────────
def load_sounds(folder: str) -> list[Path]:
    if not folder:
        return []
    p = Path(folder)
    if not p.is_dir():
        return []
    files = []
    for ext in ("*.mp3", "*.ogg", "*.wav"):
        files.extend(p.glob(ext))
    files.sort(key=lambda f: (int(f.stem) if f.stem.isdigit() else float("inf"), f.stem))
    return files


def key_to_sound(key_code: int, sounds: list[Path]) -> Path | None:
    if not sounds:
        return None
    return sounds[key_code % len(sounds)]


def play(path: Path | None, volume: float,
         pitch: float = 1.0, pan: float = 0.0) -> None:
    """Play a sound file at the given volume, pitch and pan.

    pan: -1.0 = full left, 0.0 = centre, +1.0 = full right.
    Implemented by setting per-channel left/right volumes on the mixer channel.
    Pitch != 1.0 is achieved by resampling the raw PCM array so pygame plays
    it at the mixer's fixed sample-rate, producing a higher/lower perceived
    pitch without changing playback duration noticeably for short clips.
    Falls back to plain playback when numpy is unavailable or pitch is 1.0.
    """
    if path is None or volume <= 0:
        return
    try:
        snd = pygame.mixer.Sound(str(path))
        if abs(pitch - 1.0) > 1e-4:
            try:
                import numpy as np
                arr = pygame.sndarray.array(snd)          # shape: (samples, channels) or (samples,)
                n_orig = arr.shape[0]
                n_new  = max(1, int(round(n_orig / pitch)))
                indices = np.linspace(0, n_orig - 1, n_new)
                left    = np.floor(indices).astype(int)
                right   = np.clip(left + 1, 0, n_orig - 1)
                frac    = (indices - left)[:, np.newaxis] if arr.ndim == 2 else (indices - left)
                resampled = (arr[left] * (1 - frac) + arr[right] * frac).astype(arr.dtype)
                snd = pygame.sndarray.make_sound(resampled)
            except Exception as e:
                print(f"  ⚠ pitch resample error: {e}")
        vol = min(volume, 1.0)
        if abs(pan) > 1e-4:
            # Equal-power pan: centre energy is preserved
            pan_c = max(-1.0, min(1.0, pan))
            left_vol  = vol * (1.0 - max(0.0,  pan_c))
            right_vol = vol * (1.0 + min(0.0, pan_c))
            ch = snd.play()
            if ch is not None:
                ch.set_volume(left_vol, right_vol)
        else:
            snd.set_volume(vol)
            snd.play()
    except Exception as e:
        print(f"  ⚠ play error: {e}")


# ── Listener ──────────────────────────────────────────────────────────────────
class KeyListener(QObject):
    status_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._loop    = None
        self._thread  = None
        self._running = False
        # keyboard
        self.press_sounds:   list[Path] = []
        self.release_sounds: list[Path] = []
        self.enabled   = True
        self.volume    = 1.0
        self.blacklist: set[int] = set()
        self.overrides: dict[int, dict] = {}
        # mouse
        self.mouse_btn_enabled     = True
        self.mouse_move_sound: Path | None = None
        self.mouse_move_mult       = 0.5   # normalised multiplier
        self.mouse_move_vol_smooth = 0.15  # interpolation factor (0=instant, 1=never)
        # pitch
        self.pitch_multiplier      = 1.0   # overall pitch scale
        self.pitch_random_offset   = 0.0   # per-play ± random offset
        # pan
        self.pan_strength          = 0.0   # 0=off, 1=full
        self.pan_mouse_only        = False  # if True, keyboard keys play at centre
        self.mouse_x_norm          = 0.0   # live −1.0…+1.0 (updated by mouse handler)

    def start(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def _pitch(self) -> float:
        """Return a pitch value = multiplier ± random offset."""
        import random
        offset = 0.0
        if self.pitch_random_offset > 0:
            offset = random.uniform(-self.pitch_random_offset,
                                     self.pitch_random_offset)
        return max(0.1, self.pitch_multiplier + offset)

    def _pan(self) -> float:
        """Return the current pan value scaled by pan_strength.
        mouse_x_norm is −1.0 (left edge) … 0.0 (centre) … +1.0 (right edge).
        """
        return max(-1.0, min(1.0, self.mouse_x_norm * self.pan_strength))

    # ── device discovery ──────────────────────────────────────────────────────
    def _find_keyboards(self) -> list[InputDevice]:
        out = []
        for path in list_devices():
            try:
                dev  = InputDevice(path)
                caps = dev.capabilities()
                if ecodes.EV_KEY in caps and ecodes.KEY_A in caps[ecodes.EV_KEY]:
                    if ecodes.EV_REL not in caps:
                        out.append(dev)
            except Exception:
                continue
        return out

    def _find_mice(self) -> list[InputDevice]:
        out = []
        for path in list_devices():
            try:
                dev  = InputDevice(path)
                caps = dev.capabilities()
                # mice have EV_REL with REL_X
                if ecodes.EV_REL in caps and ecodes.REL_X in caps[ecodes.EV_REL]:
                    out.append(dev)
            except Exception:
                continue
        return out

    # ── main loop ─────────────────────────────────────────────────────────────
    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        keyboards = self._find_keyboards()
        mice      = self._find_mice()

        if not keyboards and not mice:
            self.status_changed.emit("⚠ No input devices found — add yourself to the 'input' group")
            return

        parts = []
        if keyboards:
            parts.append(", ".join(f"'{k.name}'" for k in keyboards))
        if mice:
            parts.append("mouse: " + ", ".join(f"'{m.name}'" for m in mice))
        self.status_changed.emit("Listening on " + " | ".join(parts))

        held: set[int] = set()
        tasks = (
            [self._loop.create_task(self._handle_kb(kb, held))  for kb in keyboards] +
            [self._loop.create_task(self._handle_mouse(m))      for m  in mice]
        )
        try:
            self._loop.run_forever()
        finally:
            for t in tasks:
                t.cancel()

    # ── keyboard handler ──────────────────────────────────────────────────────
    async def _handle_kb(self, dev: InputDevice, held: set):
        async for event in dev.async_read_loop():
            if not self._running:
                break
            if event.type != ecodes.EV_KEY:
                continue
            code, state = event.code, event.value

            kb_pan = 0.0 if self.pan_mouse_only else self._pan()

            if state == 1 and code not in held:
                held.add(code)
                if self.enabled and code not in self.blacklist:
                    if code in self.overrides:
                        play(self.overrides[code].get("press"), self.volume, self._pitch(), kb_pan)
                    else:
                        play(key_to_sound(code, self.press_sounds), self.volume, self._pitch(), kb_pan)

            elif state == 0:
                held.discard(code)
                if self.enabled and code not in self.blacklist:
                    if code in self.overrides:
                        play(self.overrides[code].get("release"), self.volume, self._pitch(), kb_pan)
                    else:
                        play(key_to_sound(code, self.release_sounds), self.volume, self._pitch(), kb_pan)

    # ── mouse handler ─────────────────────────────────────────────────────────
    async def _handle_mouse(self, dev: InputDevice):
        import time as _time
        dx = dy = 0
        move_channel: pygame.mixer.Channel | None = None
        move_snd_path: Path | None = None
        move_snd_obj:  pygame.mixer.Sound | None = None

        # Absolute mouse X position (pixels), clamped to screen width.
        # Initialise to screen centre so pan starts neutral.
        try:
            from PyQt6.QtWidgets import QApplication
            screen_w = QApplication.primaryScreen().size().width()
        except Exception:
            screen_w = 1920
        abs_x: float = screen_w / 2.0

        # Rolling window: store (timestamp, distance) samples from the last
        # WINDOW_S seconds. Volume = total distance in window * mult / SCALE.
        WINDOW_S   = 0.05  # 50 ms window
        SCALE      = 200.0 # px/s → vol=1.0 at mult=1.0 (mult can exceed 1.0)
        samples: list[tuple[float, float]] = []  # [(t, dist), ...]
        last_move_t: float = 0.0  # time of last non-zero movement packet

        # Track the logical (unpanned) volume as a plain float so we never
        # read it back from the channel — get_volume() only returns the left
        # channel after set_volume(l, r), which breaks the lerp when panned.
        move_vol: list[float] = [0.0]   # mutable cell so watchdog can share it

        def _apply_move_vol(vol: float) -> None:
            """Write vol to the channel, splitting for pan if needed."""
            move_vol[0] = vol
            if move_channel is None:
                return
            pan_v = self._pan()
            if abs(pan_v) > 1e-4:
                move_channel.set_volume(
                    vol * (1.0 - max(0.0,  pan_v)),
                    vol * (1.0 + min(0.0, pan_v)),
                )
            else:
                move_channel.set_volume(vol)

        async def _silence_watchdog():
            # Runs concurrently. Every WINDOW_S seconds it checks whether the
            # mouse has been idle for a full window; if so it lerps toward zero.
            while self._running:
                await asyncio.sleep(WINDOW_S)
                if move_channel is not None:
                    now = _time.monotonic()
                    if now - last_move_t >= WINDOW_S:
                        smooth  = max(0.0, min(0.99, self.mouse_move_vol_smooth))
                        faded   = move_vol[0] * smooth   # lerp toward 0
                        _apply_move_vol(faded if faded > 0.001 else 0.0)

        asyncio.ensure_future(_silence_watchdog())

        async for event in dev.async_read_loop():
            if not self._running:
                break

            # ── buttons ───────────────────────────────────────────────────────
            if event.type == ecodes.EV_KEY:
                code, state = event.code, event.value
                if state == 1 and self.enabled and self.mouse_btn_enabled:
                    if code not in self.blacklist:
                        if code in self.overrides:
                            play(self.overrides[code].get("press"), self.volume, self._pitch(), self._pan())
                        else:
                            play(key_to_sound(code, self.press_sounds), self.volume, self._pitch(), self._pan())
                elif state == 0 and self.enabled and self.mouse_btn_enabled:
                    if code not in self.blacklist:
                        if code in self.overrides:
                            play(self.overrides[code].get("release"), self.volume, self._pitch(), self._pan())
                        else:
                            play(key_to_sound(code, self.release_sounds), self.volume, self._pitch(), self._pan())

            # ── movement: accumulate into dx/dy ───────────────────────────────
            elif event.type == ecodes.EV_REL:
                if event.code == ecodes.REL_X:
                    dx    += event.value
                    abs_x  = max(0.0, min(screen_w, abs_x + event.value))
                elif event.code == ecodes.REL_Y:
                    dy += event.value

            # ── sync: one report packet is complete ───────────────────────────
            elif event.type == ecodes.EV_SYN:
                # Update normalised mouse X: 0=left → -1.0, centre → 0.0, right → +1.0
                if screen_w > 0:
                    self.mouse_x_norm = (abs_x / screen_w) * 2.0 - 1.0
                # Reload sound only when the file path changes
                if self.mouse_move_sound != move_snd_path:
                    move_snd_path = self.mouse_move_sound
                    if move_channel is not None:
                        move_channel.stop()
                        move_channel = None
                    move_snd_obj = None
                    samples.clear()
                    last_move_t = 0.0
                    move_vol[0] = 0.0
                    if move_snd_path:
                        try:
                            move_snd_obj = pygame.mixer.Sound(str(move_snd_path))
                            move_channel = move_snd_obj.play(loops=-1)
                            if move_channel is not None:
                                move_channel.set_volume(0.0)
                        except Exception as e:
                            print(f"  \u26a0 move sound load error: {e}")

                if move_channel is not None:
                    now  = _time.monotonic()
                    dist = math.sqrt(dx * dx + dy * dy)

                    if dist > 0:
                        samples.append((now, dist))
                        last_move_t = now

                    cutoff  = now - WINDOW_S
                    samples = [(t, d) for t, d in samples if t >= cutoff]

                    if self.enabled and self.mouse_move_mult > 0 and samples:
                        total_dist = sum(d for _, d in samples)
                        speed = total_dist / WINDOW_S      # pixels / sec
                        target_vol = speed * self.mouse_move_mult / SCALE
                    else:
                        target_vol = 0.0

                    # Lerp the logical volume, then write to channel with pan
                    smooth  = max(0.0, min(0.99, self.mouse_move_vol_smooth))
                    new_vol = max(0.0, min(1.0, move_vol[0] + (1.0 - smooth) * (target_vol - move_vol[0])))
                    _apply_move_vol(new_vol)

                dx = dy = 0



# ── Tray icon ─────────────────────────────────────────────────────────────────
def make_tray_icon(active: bool) -> QIcon:
    name = "Icon.png" if active else "Icon-gray.png"
    icon_path = SCRIPT_DIR / name
    if icon_path.is_file():
        return QIcon(str(icon_path))
    px = QPixmap(64, 64)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#5865F2") if active else QColor("#888888"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    p.setFont(QFont("Sans", 26, QFont.Weight.Bold))
    p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "K")
    p.end()
    return QIcon(px)


# ── Folder row ────────────────────────────────────────────────────────────────
class FolderRow(QWidget):
    def __init__(self, label: str, placeholder: str, clearable: bool = False):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(115)
        layout.addWidget(lbl)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        layout.addWidget(self.edit)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)
        if clearable:
            clr = QPushButton("✕")
            clr.setFixedWidth(30)
            clr.setToolTip("Discard")
            clr.clicked.connect(lambda: self.edit.clear())
            layout.addWidget(clr)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self.edit.setText(folder)

    def path(self) -> str:   return self.edit.text().strip()
    def set_path(self, p):   self.edit.setText(p)


# ── File row (single file picker) ─────────────────────────────────────────────
class FileRow(QWidget):
    def __init__(self, label: str, placeholder: str, clearable: bool = True):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(115)
        layout.addWidget(lbl)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(placeholder)
        layout.addWidget(self.edit)
        browse = QPushButton("Browse…")
        browse.setFixedWidth(80)
        browse.clicked.connect(self._browse)
        layout.addWidget(browse)
        if clearable:
            clr = QPushButton("✕")
            clr.setFixedWidth(30)
            clr.setToolTip("Clear")
            clr.clicked.connect(lambda: self.edit.clear())
            layout.addWidget(clr)

    def _browse(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select sound file", "",
                                           "Audio files (*.mp3 *.ogg *.wav)")
        if f:
            self.edit.setText(f)

    def path(self) -> str:   return self.edit.text().strip()
    def set_path(self, p):   self.edit.setText(p)


# ── All known key/button names ────────────────────────────────────────────────
ALL_KEY_NAMES: list[str] = sorted(
    name for name in dir(ecodes) if name.startswith("KEY_") or name.startswith("BTN_")
)


def key_name_to_code(name: str) -> int | None:
    return getattr(ecodes, name, None)


# ── Blacklist panel ───────────────────────────────────────────────────────────
class BlacklistPanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__("Blacklisted Keys / Buttons  (no sound played)")
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # Add row
        add_row = QHBoxLayout()
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.addItems(ALL_KEY_NAMES)
        self.combo.setCurrentText("")
        self.combo.lineEdit().setPlaceholderText("Search key / button name…")
        add_row.addWidget(self.combo)
        add_btn = QPushButton("Add")
        add_btn.setFixedWidth(60)
        add_btn.clicked.connect(self._add)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)

        # Scroll area for items with inline delete buttons
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(130)
        self._inner = QWidget()
        self._items_layout = QVBoxLayout(self._inner)
        self._items_layout.setSpacing(2)
        self._items_layout.addStretch()
        scroll.setWidget(self._inner)
        layout.addWidget(scroll)

        self._item_widgets: dict[str, QWidget] = {}   # name -> row widget

    def _add(self):
        raw  = self.combo.currentText().strip().upper()
        name = raw if raw.startswith("KEY_") or raw.startswith("BTN_") else "KEY_" + raw
        if key_name_to_code(name) is None:
            return
        if name in self._item_widgets:
            return
        self._add_item_widget(name)
        self.changed.emit()

    def _add_item_widget(self, name: str):
        row = QWidget()
        h   = QHBoxLayout(row)
        h.setContentsMargins(2, 0, 2, 0)
        lbl = QLabel(name)
        h.addWidget(lbl)
        h.addStretch()
        del_btn = QPushButton("✕")
        del_btn.setFixedSize(24, 24)
        del_btn.setStyleSheet("color: #c0392b; font-weight: bold; border: none;")
        del_btn.clicked.connect(lambda: self._remove(name))
        h.addWidget(del_btn)
        idx = self._items_layout.count() - 1   # before stretch
        self._items_layout.insertWidget(idx, row)
        self._item_widgets[name] = row

    def _remove(self, name: str):
        w = self._item_widgets.pop(name, None)
        if w:
            self._items_layout.removeWidget(w)
            w.deleteLater()
        self.changed.emit()

    def get_keys(self) -> list[str]:
        return list(self._item_widgets.keys())

    def set_keys(self, keys: list[str]):
        for w in list(self._item_widgets.values()):
            self._items_layout.removeWidget(w)
            w.deleteLater()
        self._item_widgets.clear()
        for k in keys:
            self._add_item_widget(k)


# ── Override row ──────────────────────────────────────────────────────────────
class OverrideRow(QWidget):
    removed = pyqtSignal(object)

    def __init__(self, key_name="", press_path="", release_path=""):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)

        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(ALL_KEY_NAMES)
        self.key_combo.setCurrentText(key_name)
        self.key_combo.setFixedWidth(145)
        row.addWidget(self.key_combo)

        self.press_edit = QLineEdit(press_path)
        self.press_edit.setPlaceholderText("Press sound…")
        row.addWidget(self.press_edit)
        pb = QPushButton("…")
        pb.setFixedWidth(26)
        pb.clicked.connect(lambda: self._pick(self.press_edit))
        row.addWidget(pb)

        self.release_edit = QLineEdit(release_path)
        self.release_edit.setPlaceholderText("Release sound… (optional)")
        row.addWidget(self.release_edit)
        rb = QPushButton("…")
        rb.setFixedWidth(26)
        rb.clicked.connect(lambda: self._pick(self.release_edit))
        row.addWidget(rb)

        rem = QPushButton("✕")
        rem.setFixedWidth(26)
        rem.setStyleSheet("color: #c0392b; font-weight: bold;")
        rem.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(rem)

    def _pick(self, edit):
        f, _ = QFileDialog.getOpenFileName(self, "Select sound", "",
                                           "Audio (*.mp3 *.ogg *.wav)")
        if f:
            edit.setText(f)

    def data(self):
        return (self.key_combo.currentText().strip(),
                self.press_edit.text().strip(),
                self.release_edit.text().strip())


# ── Overrides panel ───────────────────────────────────────────────────────────
class OverridesPanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__("Key / Button Overrides  (specific sound per key, bypasses folder index)")
        outer = QVBoxLayout(self)

        add_btn = QPushButton("+ Add override")
        add_btn.clicked.connect(lambda: self._add_row())
        outer.addWidget(add_btn)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(110)
        scroll.setMaximumHeight(200)
        self._inner = QWidget()
        self._rows_layout = QVBoxLayout(self._inner)
        self._rows_layout.setSpacing(2)
        self._rows_layout.addStretch()
        scroll.setWidget(self._inner)
        outer.addWidget(scroll)

        self._rows: list[OverrideRow] = []

    def _add_row(self, key_name="", press_path="", release_path=""):
        row = OverrideRow(key_name, press_path, release_path)
        row.removed.connect(self._remove_row)
        row.key_combo.currentTextChanged.connect(self.changed)
        row.press_edit.textChanged.connect(self.changed)
        row.release_edit.textChanged.connect(self.changed)
        self._rows.append(row)
        self._rows_layout.insertWidget(self._rows_layout.count() - 1, row)
        self.changed.emit()

    def _remove_row(self, row):
        self._rows.remove(row)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self.changed.emit()

    def get_overrides(self) -> dict:
        result = {}
        for row in self._rows:
            key, press, release = row.data()
            if key and (key.startswith("KEY_") or key.startswith("BTN_")):
                result[key] = {"press": press, "release": release}
        return result

    def set_overrides(self, overrides: dict):
        for row in list(self._rows):
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()
        for key_name, paths in overrides.items():
            self._add_row(key_name, paths.get("press", ""), paths.get("release", ""))


# ── Keyboard settings panel ───────────────────────────────────────────────────
class KeyboardPanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__("Keyboard")
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Pitch multiplier (0.5 – 2.0)
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(QLabel("Pitch multiplier:"))
        self.pitch_mult_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_mult_slider.setRange(50, 200)   # maps to 0.50×–2.00×
        self.pitch_mult_slider.setValue(100)        # default 1.00×
        self.pitch_mult_slider.setFixedWidth(160)
        self.pitch_mult_slider.valueChanged.connect(self._on_pitch_mult)
        pitch_row.addWidget(self.pitch_mult_slider)
        self.pitch_mult_label = QLabel()
        self.pitch_mult_label.setFixedWidth(50)
        pitch_row.addWidget(self.pitch_mult_label)
        pitch_mult_hint = QLabel("  (overall pitch of all key/mouse sounds)")
        pitch_mult_hint.setStyleSheet("color: #888; font-size: 11px;")
        pitch_row.addWidget(pitch_mult_hint)
        pitch_row.addStretch()
        layout.addLayout(pitch_row)
        self._on_pitch_mult(self.pitch_mult_slider.value())

        # Pitch random offset (0 – 0.50)
        offset_row = QHBoxLayout()
        offset_row.addWidget(QLabel("Pitch offset ±:"))
        self.pitch_offset_slider = QSlider(Qt.Orientation.Horizontal)
        self.pitch_offset_slider.setRange(0, 50)   # maps to 0.00–0.50
        self.pitch_offset_slider.setValue(0)
        self.pitch_offset_slider.setFixedWidth(160)
        self.pitch_offset_slider.valueChanged.connect(self._on_pitch_offset)
        offset_row.addWidget(self.pitch_offset_slider)
        self.pitch_offset_label = QLabel()
        self.pitch_offset_label.setFixedWidth(50)
        offset_row.addWidget(self.pitch_offset_label)
        offset_hint = QLabel("  (random pitch variation per keypress)")
        offset_hint.setStyleSheet("color: #888; font-size: 11px;")
        offset_row.addWidget(offset_hint)
        offset_row.addStretch()
        layout.addLayout(offset_row)
        self._on_pitch_offset(self.pitch_offset_slider.value())

        # Pan strength (0 = off, 100 = full)
        pan_row = QHBoxLayout()
        pan_row.addWidget(QLabel("Pan strength:"))
        self.pan_slider = QSlider(Qt.Orientation.Horizontal)
        self.pan_slider.setRange(0, 100)
        self.pan_slider.setValue(0)
        self.pan_slider.setFixedWidth(160)
        self.pan_slider.valueChanged.connect(self._on_pan)
        pan_row.addWidget(self.pan_slider)
        self.pan_label = QLabel()
        self.pan_label.setFixedWidth(40)
        pan_row.addWidget(self.pan_label)
        pan_hint = QLabel("  (0 = off  →  sounds pan left/right with mouse X position)")
        pan_hint.setStyleSheet("color: #888; font-size: 11px;")
        pan_row.addWidget(pan_hint)
        pan_row.addStretch()
        layout.addLayout(pan_row)
        self._on_pan(self.pan_slider.value())

        # Pan scope checkbox
        self.pan_mouse_only_cb = QCheckBox(
            "Mouse only  (pan applies to mouse buttons + move sound; keyboard keys always centred)"
        )
        self.pan_mouse_only_cb.setChecked(False)
        self.pan_mouse_only_cb.stateChanged.connect(self.changed)
        layout.addWidget(self.pan_mouse_only_cb)

    def _on_pitch_mult(self, val):
        self.pitch_mult_label.setText(f"{val / 100.0:.2f}\u00d7")
        self.changed.emit()

    def _on_pitch_offset(self, val):
        self.pitch_offset_label.setText(f"\u00b1{val / 100.0:.2f}")
        self.changed.emit()

    def _on_pan(self, val):
        self.pan_label.setText("off" if val == 0 else f"{val}%")
        self.changed.emit()

    def get_values(self) -> dict:
        return {
            "pitch_multiplier":    self.pitch_mult_slider.value() / 100.0,
            "pitch_random_offset": self.pitch_offset_slider.value() / 100.0,
            "pan_strength":        self.pan_slider.value() / 100.0,
            "pan_mouse_only":      self.pan_mouse_only_cb.isChecked(),
        }

    def set_values(self, cfg: dict):
        pitch_mult_val = int(round(cfg.get("pitch_multiplier", 1.0) * 100))
        self.pitch_mult_slider.setValue(max(50, min(200, pitch_mult_val)))
        self._on_pitch_mult(self.pitch_mult_slider.value())

        pitch_offset_val = int(round(cfg.get("pitch_random_offset", 0.0) * 100))
        self.pitch_offset_slider.setValue(max(0, min(50, pitch_offset_val)))
        self._on_pitch_offset(self.pitch_offset_slider.value())

        pan_val = int(round(cfg.get("pan_strength", 0.0) * 100))
        self.pan_slider.setValue(max(0, min(100, pan_val)))
        self._on_pan(self.pan_slider.value())

        self.pan_mouse_only_cb.setChecked(cfg.get("pan_mouse_only", False))


# ── Mouse settings panel ──────────────────────────────────────────────────────
class MousePanel(QGroupBox):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__("Mouse")
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # Button sounds toggle
        self.btn_cb = QCheckBox("Play sounds for mouse buttons  (uses same press/release folders as keyboard)")
        self.btn_cb.setChecked(True)
        self.btn_cb.stateChanged.connect(self.changed)
        layout.addWidget(self.btn_cb)

        # Move sound file
        self.move_row = FileRow("Move sound:", "Sound file played on mouse movement… (optional)")
        self.move_row.edit.textChanged.connect(self.changed)
        layout.addWidget(self.move_row)

        # Delta multiplier (geometric: slider 0-100 → multiplier 0.001–2.0)
        mult_row = QHBoxLayout()
        mult_row.addWidget(QLabel("Delta multiplier:"))
        self.mult_slider = QSlider(Qt.Orientation.Horizontal)
        self.mult_slider.setRange(0, 100)
        self.mult_slider.setValue(self._mult_to_slider(0.5))
        self.mult_slider.setFixedWidth(160)
        self.mult_slider.valueChanged.connect(self._on_mult)
        mult_row.addWidget(self.mult_slider)
        self.mult_label = QLabel()
        self.mult_label.setFixedWidth(70)
        mult_row.addWidget(self.mult_label)
        hint = QLabel("  (higher = louder at same speed)")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        mult_row.addWidget(hint)
        mult_row.addStretch()
        layout.addLayout(mult_row)
        self._on_mult(self.mult_slider.value())

        # Volume smoothing slider (0 = instant, 99 = very slow)
        smooth_row = QHBoxLayout()
        smooth_row.addWidget(QLabel("Vol smoothing:"))
        self.smooth_slider = QSlider(Qt.Orientation.Horizontal)
        self.smooth_slider.setRange(0, 99)
        self.smooth_slider.setValue(15)   # default 0.15
        self.smooth_slider.setFixedWidth(160)
        self.smooth_slider.valueChanged.connect(self._on_smooth)
        smooth_row.addWidget(self.smooth_slider)
        self.smooth_label = QLabel()
        self.smooth_label.setFixedWidth(70)
        smooth_row.addWidget(self.smooth_label)
        smooth_hint = QLabel("  (how quickly move-sound volume reacts)")
        smooth_hint.setStyleSheet("color: #888; font-size: 11px;")
        smooth_row.addWidget(smooth_hint)
        smooth_row.addStretch()
        layout.addLayout(smooth_row)
        self._on_smooth(self.smooth_slider.value())

    @staticmethod
    def _slider_to_mult(val: int) -> float:
        if val <= 0:
            return 0.0
        return 0.001 * (2000.0 ** (val / 100.0))

    @staticmethod
    def _mult_to_slider(mult: float) -> int:
        if mult <= 0.001:
            return 0
        return int(round(100.0 * math.log(mult / 0.001) / math.log(2000.0)))

    def _on_mult(self, val):
        mult = self._slider_to_mult(val)
        self.mult_label.setText("off" if val == 0 else f"{mult:.3f}\u00d7")
        self.changed.emit()

    def _on_smooth(self, val):
        self.smooth_label.setText(f"{val / 100.0:.2f}")
        self.changed.emit()

    def get_values(self) -> dict:
        return {
            "mouse_btn_enabled":     self.btn_cb.isChecked(),
            "mouse_move_sound":      self.move_row.path(),
            "mouse_move_multiplier": self._slider_to_mult(self.mult_slider.value()),
            "mouse_move_vol_smooth": self.smooth_slider.value() / 100.0,
        }

    def set_values(self, cfg: dict):
        self.btn_cb.setChecked(cfg.get("mouse_btn_enabled", True))
        self.move_row.set_path(cfg.get("mouse_move_sound", ""))
        raw = cfg.get("mouse_move_multiplier", 0.5)
        if raw > 2.0:  # old config format: slider position 0-200
            mult = raw / 100.0
        else:
            mult = raw
        val = self._mult_to_slider(mult)
        self.mult_slider.setValue(val)
        self._on_mult(val)

        smooth_val = int(round(cfg.get("mouse_move_vol_smooth", 0.15) * 100))
        self.smooth_slider.setValue(max(0, min(99, smooth_val)))
        self._on_smooth(self.smooth_slider.value())


# ── Main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, listener: KeyListener):
        super().__init__()
        self.listener = listener
        self.cfg      = load_config()
        self._init_audio()
        self._build_ui()
        self._build_tray()
        self._suppress_save = True
        self._populate_from_config()
        self._suppress_save = False
        self._apply_config()
        self._start_listener()

    def _init_audio(self):
        pygame.mixer.pre_init(frequency=44100, size=-16, channels=2, buffer=256)
        pygame.mixer.init()
        pygame.mixer.set_num_channels(64)

    def _build_ui(self):
        self.setWindowTitle("KeySound")
        self.setMinimumWidth(660)
        self.setWindowIcon(make_tray_icon(True))

        root = QWidget()
        # Make root scrollable so the window doesn't get too tall to use
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(root)
        self.setCentralWidget(scroll_area)

        vbox = QVBoxLayout(root)
        vbox.setSpacing(10)
        vbox.setContentsMargins(18, 18, 18, 18)

        # Title
        title = QLabel("🎹 KeySound")
        title.setStyleSheet("font-size: 20px; font-weight: bold;")
        vbox.addWidget(title)

        vbox.addWidget(_hline())

        # Folders
        self.press_row   = FolderRow("Press sounds:", "Folder with press sounds")
        self.release_row = FolderRow("Release sounds:", "Optional — leave blank to disable", clearable=True)
        vbox.addWidget(self.press_row)
        vbox.addWidget(self.release_row)

        # Volume + enabled
        ctrl_row = QHBoxLayout()
        self.enabled_cb = QCheckBox("Enabled")
        self.enabled_cb.setChecked(True)
        self.enabled_cb.stateChanged.connect(self._apply_config)
        ctrl_row.addWidget(self.enabled_cb)
        ctrl_row.addSpacing(20)
        ctrl_row.addWidget(QLabel("Volume:"))
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(140)
        self.vol_slider.valueChanged.connect(self._on_volume)
        ctrl_row.addWidget(self.vol_slider)
        self.vol_label = QLabel("100%")
        self.vol_label.setFixedWidth(38)
        ctrl_row.addWidget(self.vol_label)
        ctrl_row.addStretch()
        vbox.addLayout(ctrl_row)

        # Status + counts
        self.status_lbl    = QLabel("Starting…")
        self.status_lbl.setStyleSheet("color: #666; font-size: 12px;")
        self.press_count   = QLabel("")
        self.release_count = QLabel("")
        for lbl in (self.press_count, self.release_count):
            lbl.setStyleSheet("color: #888; font-size: 11px;")
        vbox.addWidget(self.status_lbl)
        vbox.addWidget(self.press_count)
        vbox.addWidget(self.release_count)

        vbox.addWidget(_hline())

        # Keyboard panel
        self.keyboard_panel = KeyboardPanel()
        self.keyboard_panel.changed.connect(self._apply_config)
        vbox.addWidget(self.keyboard_panel)

        vbox.addWidget(_hline())

        # Mouse panel
        self.mouse_panel = MousePanel()
        self.mouse_panel.changed.connect(self._apply_config)
        vbox.addWidget(self.mouse_panel)

        vbox.addWidget(_hline())

        # Blacklist
        self.blacklist_panel = BlacklistPanel()
        self.blacklist_panel.changed.connect(self._apply_config)
        vbox.addWidget(self.blacklist_panel)

        # Overrides
        self.overrides_panel = OverridesPanel()
        self.overrides_panel.changed.connect(self._apply_config)
        vbox.addWidget(self.overrides_panel)

        # Apply button
        btn_row = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.setStyleSheet("font-weight: bold; padding: 6px 18px;")
        apply_btn.clicked.connect(self._apply_config)
        btn_row.addStretch()
        btn_row.addWidget(apply_btn)
        vbox.addLayout(btn_row)

        vbox.addWidget(_hline())

        # Startup options
        startup_box = QGroupBox("Startup")
        startup_layout = QVBoxLayout(startup_box)
        self.autostart_cb = QCheckBox("Launch KeySound automatically on login  (~/.config/autostart/keysound.desktop)")
        self.autostart_cb.setChecked(autostart_enabled())
        self.autostart_cb.stateChanged.connect(self._on_autostart)
        startup_layout.addWidget(self.autostart_cb)
        vbox.addWidget(startup_box)

    def _build_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(True), self)
        self.tray.setToolTip("KeySound")
        menu = QMenu()
        show_act = QAction("Show / Hide", self)
        show_act.triggered.connect(self._toggle_window)
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(QApplication.quit)
        menu.addAction(show_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._toggle_window()
            if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def _toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self.show(); self.raise_(); self.activateWindow()

    def _on_volume(self, val):
        self.vol_label.setText(f"{val}%")
        self._apply_config()

    def _populate_from_config(self):
        self.press_row.set_path(self.cfg.get("press_folder", ""))
        self.release_row.set_path(self.cfg.get("release_folder", ""))
        self.enabled_cb.setChecked(self.cfg.get("enabled", True))
        self.vol_slider.setValue(self.cfg.get("volume", 100))
        self.blacklist_panel.set_keys(self.cfg.get("blacklist", []))
        self.overrides_panel.set_overrides(self.cfg.get("overrides", {}))
        self.keyboard_panel.set_values(self.cfg)
        self.mouse_panel.set_values(self.cfg)

    def _start_listener(self):
        self.listener.status_changed.connect(self._on_status)
        self.listener.start(**self._build_listener_kwargs())

    def _build_listener_kwargs(self) -> dict:
        press   = load_sounds(self.cfg["press_folder"])
        release = load_sounds(self.cfg["release_folder"])
        volume  = self.cfg["volume"] / 100.0

        blacklist: set[int] = set()
        for name in self.cfg.get("blacklist", []):
            code = key_name_to_code(name)
            if code is not None:
                blacklist.add(code)

        overrides: dict[int, dict] = {}
        for name, paths in self.cfg.get("overrides", {}).items():
            code = key_name_to_code(name)
            if code is None:
                continue
            overrides[code] = {
                "press":   Path(paths["press"])   if paths.get("press")   else None,
                "release": Path(paths["release"]) if paths.get("release") else None,
            }

        move_path = self.cfg.get("mouse_move_sound", "")
        move_sound = Path(move_path) if move_path and Path(move_path).is_file() else None
        raw = self.cfg.get("mouse_move_multiplier", 0.5)
        if raw > 2.0:  # old config format: slider position 0-200
            move_mult = raw / 100.0
        else:
            move_mult = raw

        return dict(
            press_sounds          = press,
            release_sounds        = release,
            enabled               = self.cfg["enabled"],
            volume                = volume,
            blacklist             = blacklist,
            overrides             = overrides,
            mouse_btn_enabled     = self.cfg.get("mouse_btn_enabled", True),
            mouse_move_sound      = move_sound,
            mouse_move_mult       = move_mult,
            mouse_move_vol_smooth = self.cfg.get("mouse_move_vol_smooth", 0.15),
            pitch_multiplier      = self.cfg.get("pitch_multiplier", 1.0),
            pitch_random_offset   = self.cfg.get("pitch_random_offset", 0.0),
            pan_strength          = self.cfg.get("pan_strength", 0.0),
            pan_mouse_only        = self.cfg.get("pan_mouse_only", False),
        )

    def _apply_config(self):
        if getattr(self, '_suppress_save', False):
            return
        self.cfg["press_folder"]   = self.press_row.path()
        self.cfg["release_folder"] = self.release_row.path()
        self.cfg["enabled"]        = self.enabled_cb.isChecked()
        self.cfg["volume"]         = self.vol_slider.value()
        self.cfg["blacklist"]      = self.blacklist_panel.get_keys()
        self.cfg["overrides"]      = self.overrides_panel.get_overrides()
        self.cfg.update(self.keyboard_panel.get_values())
        self.cfg.update(self.mouse_panel.get_values())
        save_config(self.cfg)

        self.listener.update(**self._build_listener_kwargs())

        press   = load_sounds(self.cfg["press_folder"])
        release = load_sounds(self.cfg["release_folder"])
        self.press_count.setText(
            f"  ✓ {len(press)} press sounds loaded" if press else "  ⚠ No press sounds found"
        )
        self.release_count.setText(
            f"  ✓ {len(release)} release sounds loaded" if release else "  (no release sounds)"
        )
        self.tray.setIcon(make_tray_icon(self.cfg["enabled"]))

    def _on_autostart(self, state):
        set_autostart(bool(state))

    def _on_status(self, msg):
        self.status_lbl.setText(msg)


# ── helpers ───────────────────────────────────────────────────────────────────
def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    return f


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    listener = KeyListener()
    window   = MainWindow(listener)
    window.show()
    try:
        sys.exit(app.exec())
    finally:
        listener.stop()
        pygame.mixer.quit()


if __name__ == "__main__":
    main()
