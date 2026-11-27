"""
폰 런처 (scrcpy GUI)
USB로 연결한 안드로이드 폰을 PC에서 에뮬레이터처럼 쓰기 위한 런처.

- 폰에 설치된 앱 목록을 아이콘 그리드로 보여주고, 클릭 한 번으로 실행
- 폰 화면은 꺼둔 채로 PC 창에서만 조작 (scrcpy --turn-screen-off)
- 오른쪽 사이드바: 뒤로/홈/최근앱, 볼륨, 회전, 스크린샷, 전원
- "앱마다 새 창" 모드: scrcpy 가상 디스플레이(--new-display)로 앱별 창 띄우기

필요: Python 3.9+, PySide6, scrcpy 3.x (adb 포함)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSpinBox, QStatusBar, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

APP_NAME = "폰 런처"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = EXE_DIR / "phone_launcher_config.json"
SHOT_DIR = Path.home() / "Pictures" / "PhoneLauncher"

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: 콘솔창 깜빡임 방지


# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "scrcpy_path": "",          # 비워두면 자동 탐색
    "turn_screen_off": True,    # 폰 화면 끄기
    "stay_awake": True,         # 연결 중 폰 잠들지 않게
    "audio": True,              # 소리를 PC로
    "max_size": 1600,           # 0 = 원본 해상도
    "max_fps": 60,
    "bitrate_mbps": 8,
    "always_on_top": False,
    "dock_window": True,        # scrcpy 창을 런처 옆에 붙이기
    "separate_windows": False,  # 앱마다 새 창(가상 디스플레이)
    "virtual_size": "1080x1920/420",
    "show_system_apps": False,
    "theme": "dark",            # dark | light | system
    "favorites": [],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# scrcpy / adb 래퍼
# ----------------------------------------------------------------------------
def find_scrcpy(custom: str = "") -> str | None:
    candidates = []
    if custom:
        candidates.append(Path(custom))
    exe = "scrcpy.exe" if IS_WIN else "scrcpy"
    for base in (EXE_DIR, APP_DIR):
        candidates.append(base / "scrcpy" / exe)
        candidates.append(base / exe)
        candidates.extend(base.glob(f"scrcpy-*/{exe}"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("scrcpy")


def find_adb(scrcpy_path: str | None) -> str | None:
    exe = "adb.exe" if IS_WIN else "adb"
    if scrcpy_path:
        p = Path(scrcpy_path).parent / exe
        if p.is_file():
            return str(p)
    return shutil.which("adb")


def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout,
        creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
    )


@dataclass
class Device:
    serial: str
    model: str = ""
    state: str = "device"

    @property
    def label(self) -> str:
        name = self.model.replace("_", " ") or self.serial
        extra = "" if self.state == "device" else f"  [{self.state}]"
        return f"{name}  ({self.serial}){extra}"


@dataclass
class AppInfo:
    package: str
    name: str
    system: bool = False


@dataclass
class Session:
    proc: subprocess.Popen
    serial: str
    title: str
    kind: str  # "main" | "app"
    started: float = field(default_factory=time.time)


class Phone:
    """adb/scrcpy 명령 모음."""

    def __init__(self, scrcpy: str | None, adb: str | None):
        self.scrcpy = scrcpy
        self.adb = adb

    # --- adb -----------------------------------------------------------------
    def devices(self) -> list[Device]:
        if not self.adb:
            return []
        out = run([self.adb, "devices", "-l"]).stdout.decode("utf-8", "replace")
        devs = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            model = ""
            for p in parts[2:]:
                if p.startswith("model:"):
                    model = p[6:]
            devs.append(Device(parts[0], model, parts[1]))
        return devs

    def shell(self, serial: str, *args: str, timeout: float = 10) -> str:
        r = run([self.adb, "-s", serial, "shell", *args], timeout=timeout)
        return r.stdout.decode("utf-8", "replace")

    def key(self, serial: str, keycode: int | str) -> None:
        self.shell(serial, "input", "keyevent", str(keycode))

    def launch_via_adb(self, serial: str, package: str) -> None:
        self.shell(serial, "monkey", "-p", package, "-c",
                   "android.intent.category.LAUNCHER", "1")

    def force_stop(self, serial: str, package: str) -> None:
        self.shell(serial, "am", "force-stop", package)

    def screenshot(self, serial: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        r = run([self.adb, "-s", serial, "exec-out", "screencap", "-p"], timeout=20)
        if not r.stdout.startswith(b"\x89PNG"):
            raise RuntimeError(r.stderr.decode("utf-8", "replace") or "스크린샷 실패")
        dest.write_bytes(r.stdout)
        return dest

    def rotate(self, serial: str) -> None:
        cur = self.shell(serial, "settings", "get", "system", "user_rotation").strip()
        nxt = "0" if cur in ("1", "3") else "1"
        self.shell(serial, "settings", "put", "system", "accelerometer_rotation", "0")
        self.shell(serial, "settings", "put", "system", "user_rotation", nxt)

    # --- 앱 목록 ---------------------------------------------------------------
    LIST_RE = re.compile(r"^\s*([*-])\s+(.+?)\s{2,}([\w.]+)\s*$")

    def list_apps(self, serial: str) -> list[AppInfo]:
        apps: list[AppInfo] = []
        # 1) scrcpy --list-apps : 앱 이름까지 나옴 (scrcpy 2.1+)
        if self.scrcpy:
            try:
                r = run([self.scrcpy, "-s", serial, "--list-apps"], timeout=40)
                text = (r.stdout + b"\n" + r.stderr).decode("utf-8", "replace")
                for line in text.splitlines():
                    m = self.LIST_RE.match(line)
                    if m:
                        apps.append(AppInfo(m.group(3), m.group(2).strip(), m.group(1) == "*"))
            except Exception:
                apps = []
        # 2) 실패 시 pm list packages (이름 대신 패키지명)
        if not apps and self.adb:
            user = set(_parse_pm(self.shell(serial, "pm", "list", "packages", "-3")))
            launchable = _parse_pm(self.shell(serial, "pm", "list", "packages"))
            for pkg in launchable:
                nice = pkg.split(".")[-1].capitalize()
                apps.append(AppInfo(pkg, nice, pkg not in user))
        apps.sort(key=lambda a: a.name.lower())
        return apps

    # --- scrcpy --------------------------------------------------------------
    def base_args(self, serial: str, cfg: dict) -> list[str]:
        a = [self.scrcpy, "-s", serial, "--shortcut-mod=lalt,ralt"]
        if cfg["stay_awake"]:
            a.append("--stay-awake")
        if not cfg["audio"]:
            a.append("--no-audio")
        if cfg["max_size"]:
            a.append(f"--max-size={cfg['max_size']}")
        if cfg["max_fps"]:
            a.append(f"--max-fps={cfg['max_fps']}")
        a.append(f"--video-bit-rate={cfg['bitrate_mbps']}M")
        if cfg["always_on_top"]:
            a.append("--always-on-top")
        return a

    def start(self, args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            args, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )


def _parse_pm(text: str) -> list[str]:
    return [l.split(":", 1)[1].strip() for l in text.splitlines() if l.startswith("package:")]


# ----------------------------------------------------------------------------
# 아이콘 (앱 아이콘 대신 이니셜 원형 아이콘 생성)
# ----------------------------------------------------------------------------
PALETTE = ["#4F7CFF", "#20B486", "#F2994A", "#EB5757", "#9B51E0",
           "#2D9CDB", "#27AE60", "#F2C94C", "#BB6BD9", "#56CCF2"]


def letter_icon(text: str, key: str, size: int = 64) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(PALETTE[sum(map(ord, key)) % len(PALETTE)]))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, size * 0.26, size * 0.26)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(int(size * 0.42))
    p.setFont(f)
    ch = (text.strip() or "?")[0].upper()
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    return QIcon(pm)


# ----------------------------------------------------------------------------
# 백그라운드 작업 → UI 신호
# ----------------------------------------------------------------------------
class Bus(QObject):
    devices = Signal(list)
    apps = Signal(str, list)
    status = Signal(str)
    error = Signal(str)


def bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


# ----------------------------------------------------------------------------
# 메인 창
# ----------------------------------------------------------------------------
THEMES = {
    "dark": dict(bg="#1E1F24", fg="#E6E6E6", field="#2A2C33", border="#3A3D46", panel="#24262C",
                 hover="#2F323B", sel="#33405E", flat="#2A2C33", flathover="#353843",
                 side="#17181C", tool="#2A2C33", toolhover="#3A3E4A", muted="#9AA0AA",
                 check="#5A5F6B", accent="#2F6FED", accenthover="#4A82F0"),
    "light": dict(bg="#F4F5F7", fg="#1F2329", field="#FFFFFF", border="#D3D7DE", panel="#FFFFFF",
                  hover="#EEF1F6", sel="#DCE7FF", flat="#E6E9EF", flathover="#DADFE7",
                  side="#E9ECF1", tool="#FFFFFF", toolhover="#DCE3EE", muted="#6B7280",
                  check="#9AA3B2", accent="#2F6FED", accenthover="#4A82F0"),
}

STYLE_TMPL = """
QWidget { background:$bg; color:$fg; font-size:13px; }
QLineEdit, QComboBox, QSpinBox { background:$field; border:1px solid $border;
    border-radius:6px; padding:5px 8px; }
QPushButton { background:$accent; border:none; border-radius:6px; padding:7px 12px; color:white; }
QPushButton:hover { background:$accenthover; }
QPushButton#flat { background:$flat; color:$fg; }
QPushButton#flat:hover { background:$flathover; }
QListWidget { background:$panel; border:none; border-radius:10px; padding:8px; }
QListWidget::item { border-radius:10px; padding:6px; color:$fg; }
QListWidget::item:hover { background:$hover; }
QListWidget::item:selected { background:$sel; color:$fg; }
QTabWidget::pane { border:none; }
QTabBar::tab { background:$panel; padding:7px 16px; border-top-left-radius:6px;
    border-top-right-radius:6px; margin-right:2px; }
QTabBar::tab:selected { background:$accent; color:white; }
QToolButton { background:$tool; border:none; border-radius:10px; font-size:20px; color:$fg; }
QToolButton:hover { background:$toolhover; }
QToolButton:pressed { background:$accent; }
QFrame#sidebar { background:$side; border-left:1px solid $border; }
QFrame#sidebar QFrame#sep { background:$border; }
QFrame#sidebar QWidget { background:transparent; }
QFrame#sidebar QToolButton { background:$tool; }
QFrame#sidebar QToolButton:hover { background:$toolhover; }
QFrame#sidebar QToolButton:pressed { background:$accent; }
QLabel#muted { color:$muted; }
QStatusBar { background:$side; color:$muted; }
QMenu { background:$field; border:1px solid $border; }
QMenu::item:selected { background:$sel; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid $check; border-radius:4px; background:$field; }
QCheckBox::indicator:checked { background:$accent; border-color:$accent; }
"""


def system_is_dark() -> bool:
    try:
        return QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except Exception:
        return False


def build_style(theme: str) -> str:
    if theme not in THEMES:
        theme = "dark" if system_is_dark() else "light"
    from string import Template
    return Template(STYLE_TMPL).substitute(THEMES[theme])


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.bus = Bus()
        self.phone = Phone(None, None)
        self.devices: list[Device] = []
        self.apps: list[AppInfo] = []
        self.sessions: list[Session] = []

        self.setWindowTitle(APP_NAME)
        self.resize(560, 760)
        self._build_ui()
        self._wire()
        try:  # Windows 다크/라이트 설정이 바뀌면 바로 반영 (테마 = system 일 때)
            QApplication.styleHints().colorSchemeChanged.connect(
                lambda *_: self.cfg.get("theme") == "system" and self.apply_theme())
        except Exception:
            pass
        self._locate_tools()

        self.poll = QTimer(self, interval=3000, timeout=self._tick)
        self.poll.start()
        self.refresh_devices()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QWidget()
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        main = QWidget()
        v = QVBoxLayout(main)
        v.setContentsMargins(14, 14, 14, 8)

        # 기기 선택줄
        top = QHBoxLayout()
        self.dev_combo = QComboBox()
        self.dev_combo.setMinimumWidth(220)
        btn_refresh = QPushButton("⟳")
        btn_refresh.setObjectName("flat")
        btn_refresh.setFixedWidth(38)
        btn_refresh.setToolTip("기기 다시 찾기")
        btn_refresh.clicked.connect(self.refresh_devices)
        self.btn_mirror = QPushButton("▶  폰 화면 열기")
        self.btn_mirror.clicked.connect(self.toggle_mirror)
        top.addWidget(QLabel("📱"))
        top.addWidget(self.dev_combo, 1)
        top.addWidget(btn_refresh)
        top.addWidget(self.btn_mirror)
        v.addLayout(top)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        # 탭1: 앱
        apps_tab = QWidget()
        av = QVBoxLayout(apps_tab)
        av.setContentsMargins(0, 8, 0, 0)
        srow = QHBoxLayout()
        self.search = QLineEdit(placeholderText="🔍  앱 검색 (이름 또는 패키지)")
        self.chk_system = QCheckBox("시스템 앱")
        self.chk_system.setChecked(self.cfg["show_system_apps"])
        btn_reload = QPushButton("앱 새로고침")
        btn_reload.setObjectName("flat")
        btn_reload.clicked.connect(self.refresh_apps)
        srow.addWidget(self.search, 1)
        srow.addWidget(self.chk_system)
        srow.addWidget(btn_reload)
        av.addLayout(srow)

        av.addWidget(QLabel("⭐ 즐겨찾기"))
        self.fav_list = self._grid(height=118)
        av.addWidget(self.fav_list)
        av.addWidget(QLabel("전체 앱  (클릭하면 실행, 우클릭은 메뉴)"))
        self.app_list = self._grid()
        av.addWidget(self.app_list, 1)
        self.tabs.addTab(apps_tab, "앱")

        # 탭2: 실행 중
        run_tab = QWidget()
        rv = QVBoxLayout(run_tab)
        rv.setContentsMargins(0, 8, 0, 0)
        self.run_list = QListWidget()
        rv.addWidget(self.run_list, 1)
        btn_stop = QPushButton("선택한 창 닫기")
        btn_stop.setObjectName("flat")
        btn_stop.clicked.connect(self.stop_selected)
        rv.addWidget(btn_stop)
        self.tabs.addTab(run_tab, "실행 중")

        # 탭3: 설정
        self.tabs.addTab(self._settings_tab(), "설정")

        h.addWidget(main, 1)
        h.addWidget(self._sidebar())

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.apply_theme()

    def _grid(self, height: int | None = None) -> QListWidget:
        w = QListWidget()
        w.setViewMode(QListWidget.IconMode)
        w.setResizeMode(QListWidget.Adjust)
        w.setMovement(QListWidget.Static)
        w.setIconSize(QSize(52, 52))
        w.setGridSize(QSize(92, 100))
        w.setWordWrap(True)
        w.setUniformItemSizes(True)
        w.setSpacing(4)
        w.setContextMenuPolicy(Qt.CustomContextMenu)
        if height:
            w.setFixedHeight(height)
            w.setFlow(QListWidget.LeftToRight)
            w.setWrapping(False)
            w.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        return w

    def _sidebar(self) -> QFrame:
        bar = QFrame(objectName="sidebar")
        bar.setFixedWidth(62)
        v = QVBoxLayout(bar)
        v.setContentsMargins(8, 14, 8, 14)
        v.setSpacing(8)
        buttons = [
            ("◁", "뒤로", lambda s: self.phone.key(s, 4)),
            ("○", "홈", lambda s: self.phone.key(s, 3)),
            ("▢", "최근 앱", lambda s: self.phone.key(s, 187)),
            None,
            ("🔊", "볼륨 +", lambda s: self.phone.key(s, 24)),
            ("🔉", "볼륨 -", lambda s: self.phone.key(s, 25)),
            ("🔇", "음소거", lambda s: self.phone.key(s, 164)),
            None,
            ("⟳", "화면 회전 (세로/가로)", lambda s: self.phone.rotate(s)),
            ("📷", "스크린샷 (사진 폴더에 저장)", self._screenshot),
            ("🔔", "알림창 내리기", lambda s: self.phone.shell(s, "cmd", "statusbar", "expand-notifications")),
            None,
            ("⏻", "전원 버튼 (폰 화면 켜기/끄기)", lambda s: self.phone.key(s, 26)),
        ]
        for b in buttons:
            if b is None:
                line = QFrame()
                line.setFixedHeight(1)
                line.setObjectName("sep")
                v.addWidget(line)
                continue
            icon, tip, fn = b
            tb = QToolButton(text=icon, toolTip=tip)
            tb.setFixedSize(46, 46)
            tb.clicked.connect(lambda _=False, f=fn, t=tip: self._device_action(f, t))
            v.addWidget(tb)
        v.addStretch(1)
        return bar

    def _settings_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setContentsMargins(4, 12, 4, 4)
        f.setVerticalSpacing(10)
        c = self.cfg

        self.s_theme = QComboBox()
        for key, label in (("dark", "다크"), ("light", "라이트"), ("system", "Windows 설정 따라가기")):
            self.s_theme.addItem(label, key)
        self.s_theme.setCurrentIndex(max(0, self.s_theme.findData(c.get("theme", "dark"))))
        self.s_theme.currentIndexChanged.connect(
            lambda _: (self._set("theme", self.s_theme.currentData()), self.apply_theme()))
        f.addRow("화면 테마", self.s_theme)

        self.s_path = QLineEdit(c["scrcpy_path"], placeholderText="자동으로 찾음")
        pb = QPushButton("찾아보기")
        pb.setObjectName("flat")
        pb.clicked.connect(self._pick_scrcpy)
        row = QHBoxLayout()
        row.addWidget(self.s_path, 1)
        row.addWidget(pb)
        f.addRow("scrcpy.exe 위치", row)

        def chk(key, text):
            cb = QCheckBox(text)
            cb.setChecked(bool(c[key]))
            cb.toggled.connect(lambda v: self._set(key, v))
            return cb

        f.addRow(chk("turn_screen_off", "폰 화면 끄고 사용 (PC에만 표시)"))
        f.addRow(chk("stay_awake", "연결 중에는 폰이 잠들지 않게"))
        f.addRow(chk("audio", "폰 소리를 PC 스피커로 (Android 11+)"))
        f.addRow(chk("dock_window", "폰 화면 창을 런처 옆에 붙이기"))
        f.addRow(chk("always_on_top", "폰 화면 창 항상 위"))
        f.addRow(chk("separate_windows", "앱마다 새 창으로 열기 (가상 디스플레이, Android 10+)"))

        def spin(key, lo, hi, step, suffix):
            sb = QSpinBox(minimum=lo, maximum=hi, singleStep=step, suffix=suffix)
            sb.setValue(int(c[key]))
            sb.valueChanged.connect(lambda v: self._set(key, v))
            return sb

        f.addRow("최대 해상도", spin("max_size", 0, 4096, 160, " px (0=원본)"))
        f.addRow("최대 FPS", spin("max_fps", 15, 144, 15, " fps"))
        f.addRow("화질(비트레이트)", spin("bitrate_mbps", 1, 64, 1, " Mbps"))

        self.s_vsize = QLineEdit(c["virtual_size"])
        self.s_vsize.editingFinished.connect(lambda: self._set("virtual_size", self.s_vsize.text().strip()))
        f.addRow("새 창 크기/DPI", self.s_vsize)

        tips = QLabel(
            "단축키 (폰 화면 창에서): Alt+O 화면 끄기 · Alt+Shift+O 켜기 · "
            "Alt+F 전체화면 · Alt+R 회전 · Alt+B 뒤로 · Alt+H 홈 · 우클릭 = 뒤로"
        )
        tips.setWordWrap(True)
        tips.setObjectName("muted")
        f.addRow(tips)
        return w

    # ---------------- 이벤트 연결 ----------------
    def _wire(self):
        self.bus.devices.connect(self._on_devices)
        self.bus.apps.connect(self._on_apps)
        self.bus.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.bus.error.connect(lambda m: QMessageBox.warning(self, APP_NAME, m))
        self.search.textChanged.connect(self._render_apps)
        self.chk_system.toggled.connect(lambda v: (self._set("show_system_apps", v), self._render_apps()))
        self.dev_combo.currentIndexChanged.connect(lambda _: (self.refresh_apps(), self._update_mirror_btn()))
        for lst in (self.app_list, self.fav_list):
            lst.itemClicked.connect(self._on_app_clicked)
            lst.customContextMenuRequested.connect(lambda pos, l=lst: self._app_menu(l, pos))

    def apply_theme(self):
        self.setStyleSheet(build_style(self.cfg.get("theme", "dark")))

    def _set(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def _pick_scrcpy(self):
        path, _ = QFileDialog.getOpenFileName(self, "scrcpy 실행 파일 선택", "",
                                              "scrcpy (scrcpy.exe scrcpy);;모든 파일 (*)")
        if path:
            self.s_path.setText(path)
            self._set("scrcpy_path", path)
            self._locate_tools()
            self.refresh_devices()

    def _locate_tools(self):
        sc = find_scrcpy(self.cfg.get("scrcpy_path", ""))
        self.phone = Phone(sc, find_adb(sc))
        if not sc:
            self.statusBar().showMessage("scrcpy를 찾지 못했어요. 설정 탭에서 scrcpy.exe 위치를 지정하세요.")
        elif not self.phone.adb:
            self.statusBar().showMessage("adb를 찾지 못했어요. scrcpy 폴더에 adb.exe가 있는지 확인하세요.")

    # ---------------- 기기 ----------------
    def current_serial(self) -> str | None:
        i = self.dev_combo.currentIndex()
        if 0 <= i < len(self.devices) and self.devices[i].state == "device":
            return self.devices[i].serial
        return None

    def refresh_devices(self):
        bg(lambda: self.bus.devices.emit(self._safe(self.phone.devices, default=[])))

    def _on_devices(self, devs: list[Device]):
        old = [d.serial + d.state for d in self.devices]
        if old == [d.serial + d.state for d in devs] and self.devices:
            return
        cur = self.current_serial()
        self.devices = devs
        self.dev_combo.blockSignals(True)
        self.dev_combo.clear()
        for d in devs:
            self.dev_combo.addItem(d.label)
        if not devs:
            self.dev_combo.addItem("연결된 폰 없음 — USB 디버깅 켜고 연결하세요")
        idx = next((i for i, d in enumerate(devs) if d.serial == cur), 0)
        self.dev_combo.setCurrentIndex(idx)
        self.dev_combo.blockSignals(False)
        unauthorized = [d for d in devs if d.state == "unauthorized"]
        if unauthorized:
            self.statusBar().showMessage("폰에서 'USB 디버깅 허용' 팝업을 눌러주세요.")
        self._update_mirror_btn()
        self.refresh_apps()

    # ---------------- 앱 목록 ----------------
    def refresh_apps(self):
        serial = self.current_serial()
        if not serial:
            self.apps = []
            self._render_apps()
            return
        self.statusBar().showMessage("앱 목록 불러오는 중…")

        def work():
            apps = self._safe(self.phone.list_apps, serial, default=[])
            self.bus.apps.emit(serial, apps)
        bg(work)

    def _on_apps(self, serial: str, apps: list[AppInfo]):
        if serial != self.current_serial():
            return
        self.apps = apps
        self._render_apps()
        self.statusBar().showMessage(f"앱 {len(apps)}개", 4000)

    def _render_apps(self):
        q = self.search.text().strip().lower()
        show_sys = self.chk_system.isChecked()
        favs = self.cfg["favorites"]
        self.app_list.clear()
        self.fav_list.clear()
        by_pkg = {a.package: a for a in self.apps}
        for pkg in favs:
            a = by_pkg.get(pkg)
            if a:
                self.fav_list.addItem(self._item(a))
        for a in self.apps:
            if a.system and not show_sys:
                continue
            if q and q not in a.name.lower() and q not in a.package.lower():
                continue
            self.app_list.addItem(self._item(a))

    def _item(self, a: AppInfo) -> QListWidgetItem:
        it = QListWidgetItem(letter_icon(a.name, a.package), a.name)
        it.setData(Qt.UserRole, a.package)
        it.setToolTip(f"{a.name}\n{a.package}")
        it.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
        return it

    def _on_app_clicked(self, item: QListWidgetItem):
        self.launch(item.data(Qt.UserRole), item.text())

    def _app_menu(self, lst: QListWidget, pos):
        item = lst.itemAt(pos)
        if not item:
            return
        pkg, name = item.data(Qt.UserRole), item.text()
        m = QMenu(self)
        m.addAction("실행", lambda: self.launch(pkg, name))
        m.addAction("새 창으로 실행", lambda: self.launch(pkg, name, separate=True))
        m.addAction("처음부터 다시 실행", lambda: self.launch(pkg, name, restart=True))
        m.addSeparator()
        if pkg in self.cfg["favorites"]:
            m.addAction("즐겨찾기 해제", lambda: self._toggle_fav(pkg))
        else:
            m.addAction("즐겨찾기 추가", lambda: self._toggle_fav(pkg))
        m.addAction("강제 종료", lambda: self._device_action(lambda s: self.phone.force_stop(s, pkg), f"{name} 종료"))
        m.addAction("패키지명 복사", lambda: QApplication.clipboard().setText(pkg))
        m.exec(lst.mapToGlobal(pos))

    def _toggle_fav(self, pkg: str):
        favs = self.cfg["favorites"]
        favs.remove(pkg) if pkg in favs else favs.append(pkg)
        self._set("favorites", favs)
        self._render_apps()

    # ---------------- 실행 ----------------
    def _require(self) -> str | None:
        if not self.phone.scrcpy:
            QMessageBox.warning(self, APP_NAME, "scrcpy를 찾지 못했어요.\n설정 탭에서 scrcpy.exe 위치를 지정해주세요.")
            self.tabs.setCurrentIndex(2)
            return None
        s = self.current_serial()
        if not s:
            QMessageBox.information(self, APP_NAME, "연결된 폰이 없어요.\nUSB 디버깅을 켜고 케이블을 연결해주세요.")
        return s

    def _main_session(self, serial: str) -> Session | None:
        return next((x for x in self.sessions if x.serial == serial and x.kind == "main"
                     and x.proc.poll() is None), None)

    def _dock_args(self) -> list[str]:
        if not self.cfg["dock_window"]:
            return []
        g = self.frameGeometry()
        return [f"--window-x={g.right() + 4}", f"--window-y={max(g.top(), 0)}"]

    def toggle_mirror(self):
        serial = self._require()
        if not serial:
            return
        s = self._main_session(serial)
        if s:
            s.proc.terminate()
        else:
            self.open_main(serial)
        QTimer.singleShot(400, self._update_mirror_btn)

    def open_main(self, serial: str, start_app: str | None = None) -> Session:
        args = self.phone.base_args(serial, self.cfg)
        if self.cfg["turn_screen_off"]:
            args.append("--turn-screen-off")
        args += self._dock_args()
        title = f"{APP_NAME} - {self._device_name(serial)}"
        args.append(f"--window-title={title}")
        if start_app:
            args.append(f"--start-app={start_app}")
        return self._spawn(args, serial, title, "main")

    def launch(self, pkg: str, name: str, separate: bool | None = None, restart: bool = False):
        serial = self._require()
        if not serial:
            return
        separate = self.cfg["separate_windows"] if separate is None else separate
        target = ("+" if restart else "") + pkg

        if separate:
            args = self.phone.base_args(serial, self.cfg)
            args += [f"--new-display={self.cfg['virtual_size']}",
                     f"--start-app={target}", f"--window-title={name}"]
            # 가상 디스플레이에서는 폰 본체 화면이 잠들지 않게만 유지
            self._spawn(args, serial, name, "app")
            self.bus.status.emit(f"'{name}' 새 창으로 실행")
            return

        main = self._main_session(serial)
        if main:  # 이미 폰 화면 창이 열려 있으면 그 안에서 실행
            def work():
                if restart:
                    self.phone.force_stop(serial, pkg)
                self.phone.launch_via_adb(serial, pkg)
            bg(self._safe, work)
            self.bus.status.emit(f"'{name}' 실행")
        else:
            self.open_main(serial, start_app=target)
            self.bus.status.emit(f"폰 화면 열고 '{name}' 실행")
        self._update_mirror_btn()

    def _spawn(self, args, serial, title, kind) -> Session:
        try:
            proc = self.phone.start(args)
        except Exception as e:
            self.bus.error.emit(f"scrcpy 실행 실패: {e}")
            raise
        s = Session(proc, serial, title, kind)
        self.sessions.append(s)
        bg(self._watch, s)
        self._render_sessions()
        return s

    def _watch(self, s: Session):
        """scrcpy가 바로 죽으면 에러 메시지를 보여준다."""
        _, err = s.proc.communicate()
        if s.proc.returncode not in (0, None) and time.time() - s.started < 8:
            msg = err.decode("utf-8", "replace").strip().splitlines()
            tail = "\n".join(l for l in msg if "ERROR" in l or "WARN" in l)[-800:] or "\n".join(msg[-6:])
            self.bus.error.emit(f"'{s.title}' 창을 열지 못했어요.\n\n{tail}")

    def _render_sessions(self):
        self.sessions = [s for s in self.sessions if s.proc.poll() is None]
        self.run_list.clear()
        for s in self.sessions:
            kind = "폰 화면" if s.kind == "main" else "앱 창"
            it = QListWidgetItem(f"{'🖥' if s.kind == 'main' else '🪟'}  {s.title}   ·  {kind}  ·  {s.serial}")
            it.setData(Qt.UserRole, id(s))
            self.run_list.addItem(it)
        self.tabs.setTabText(1, f"실행 중 ({len(self.sessions)})" if self.sessions else "실행 중")

    def stop_selected(self):
        for it in self.run_list.selectedItems():
            for s in self.sessions:
                if id(s) == it.data(Qt.UserRole):
                    s.proc.terminate()
        QTimer.singleShot(400, self._render_sessions)

    def _update_mirror_btn(self):
        s = self.current_serial()
        on = bool(s and self._main_session(s))
        self.btn_mirror.setText("■  폰 화면 닫기" if on else "▶  폰 화면 열기")

    # ---------------- 사이드바 동작 ----------------
    def _device_action(self, fn, label):
        serial = self.current_serial()
        if not serial or not self.phone.adb:
            self.statusBar().showMessage("연결된 폰이 없어요.", 3000)
            return
        bg(lambda: (self._safe(fn, serial), self.bus.status.emit(label)))

    def _screenshot(self, serial):
        dest = SHOT_DIR / time.strftime("screen_%Y%m%d_%H%M%S.png")
        self.phone.screenshot(serial, dest)
        self.bus.status.emit(f"스크린샷 저장: {dest}")

    # ---------------- 기타 ----------------
    def _device_name(self, serial):
        d = next((d for d in self.devices if d.serial == serial), None)
        return (d.model.replace("_", " ") if d and d.model else serial)

    def _safe(self, fn, *args, default=None):
        try:
            return fn(*args)
        except Exception as e:
            self.bus.status.emit(f"오류: {e}")
            return default

    def _tick(self):
        self.refresh_devices()
        self._render_sessions()
        self._update_mirror_btn()

    def closeEvent(self, e):
        for s in self.sessions:
            if s.proc.poll() is None:
                s.proc.terminate()
        # exe에 내장된 adb는 임시 폴더에서 돌기 때문에, 종료할 때 같이 꺼줘야 임시 파일이 정리됨
        adb = self.phone.adb
        if adb and getattr(sys, "frozen", False) and str(APP_DIR) in adb:
            try:
                run([adb, "kill-server"], timeout=5)
            except Exception:
                pass
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(letter_icon("📱", "phone", 128))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
