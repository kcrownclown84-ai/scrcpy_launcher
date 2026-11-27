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
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSpinBox, QStatusBar, QTabWidget,
    QToolButton, QVBoxLayout, QWidget,
)

APP_NAME = "폰 런처"
try:  # CI가 빌드할 때 만들어 넣는 파일 (빌드 날짜·커밋)
    from _build_info import BUILD  # type: ignore
except Exception:
    BUILD = "개발 버전"
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
EXE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
# 설정 파일은 exe 옆이 아니라 %APPDATA%\\PhoneLauncher 에 저장 (설정을 바꿀 때만 생성됨)
CONFIG_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / "PhoneLauncher"
CONFIG_PATH = CONFIG_DIR / "config.json"
ICON_DIR = CONFIG_DIR / "icons"   # 폰에서 가져온 앱 아이콘 캐시
ICON_HELPER = APP_DIR / "assets" / "iconhelper.jar"  # 폰에서 아이콘을 꺼내는 도우미 (CI에서 빌드)
LEGACY_CONFIG = EXE_DIR / "phone_launcher_config.json"
NO_SAVE = "--no-save" in sys.argv  # 바로가기에 --no-save 를 붙이면 아무 파일도 안 남김
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
    "aspect": "phone",          # 폰 화면 창 비율: phone(그대로 미러링) | 16:9 | 16:10 | 4:3 | 21:9 | custom
    "aspect_landscape": True,   # 가로 방향
    "aspect_custom": "1920x1080",
    "show_system_apps": False,
    "theme": "dark",            # dark | light | system
    "favorites": [],
    "wifi_addr": "",            # 마지막으로 무선 연결한 IP:포트
    "wifi_auto": True,          # 시작할 때 무선 자동 재연결
    "power_save": False,        # 절전 모드: 해상도·FPS·화질을 낮춰 폰 발열/배터리 절약
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["favorites"] = []
    if LEGACY_CONFIG.is_file():  # 예전 버전이 exe 옆에 만든 파일은 옮기고 지움
        try:
            if not NO_SAVE and not CONFIG_PATH.exists():
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)
                CONFIG_PATH.write_bytes(LEGACY_CONFIG.read_bytes())
            LEGACY_CONFIG.unlink()
        except Exception:
            pass
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    if NO_SAVE:
        return
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# scrcpy / adb 래퍼
# ----------------------------------------------------------------------------
def delete_saved_config() -> None:
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    try:
        LEGACY_CONFIG.unlink()
    except Exception:
        pass


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
        extra = {"device": "", "unauthorized": "  [폰 잠금 풀고 'USB 디버깅 허용' 눌러주세요]",
                 "offline": "  [연결 끊김 - 다시 연결 중]",
                 "authorizing": "  [인증 중…]"}.get(self.state, f"  [{self.state}]")
        icon = "📶 " if self.wireless else ""
        return f"{icon}{name}  ({self.serial}){extra}"

    @property
    def wireless(self) -> bool:
        return ":" in self.serial or "_adb-tls" in self.serial


@dataclass
class AppInfo:
    package: str
    name: str
    system: bool = False
    launchable: bool = True


@dataclass
class Session:
    proc: subprocess.Popen
    serial: str
    title: str
    kind: str  # "main" | "app"
    started: float = field(default_factory=time.time)
    display_id: int | None = None  # 가상 디스플레이 번호 (비율 모드)


class Phone:
    """adb/scrcpy 명령 모음."""

    def __init__(self, scrcpy: str | None, adb: str | None):
        self.scrcpy = scrcpy
        self.adb = adb
        self._server_ok = False
        self._lock = threading.Lock()

    # --- adb -----------------------------------------------------------------
    def ensure_server(self) -> None:
        """adb 서버를 출력 캡처 없이 먼저 띄운다.
        Windows에서 출력을 캡처하는 adb 명령이 서버를 처음 띄우면, 서버가 파이프를 물고 있어서
        명령이 끝나지 않고 타임아웃 나는 문제가 있음 (페어링 타임아웃의 주원인)."""
        if self._server_ok or not self.adb:
            return
        with self._lock:
            if self._server_ok:
                return
            try:
                subprocess.run([self.adb, "start-server"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=30, creationflags=NO_WINDOW)
            except Exception:
                pass
            self._server_ok = True

    def devices(self) -> list[Device]:
        if not self.adb:
            return []
        self.ensure_server()
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

    # --- 무선 (Android 11+ 무선 디버깅) ----------------------------------------
    def adb_out(self, *args: str, timeout: float = 15) -> str:
        self.ensure_server()
        r = run([self.adb, *args], timeout=timeout)
        return (r.stdout + r.stderr).decode("utf-8", "replace").strip()

    @staticmethod
    def reachable(addr: str, timeout: float = 3) -> bool:
        """PC에서 폰의 IP:포트로 TCP 연결이 되는지 미리 확인."""
        import socket
        try:
            host, port = addr.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except Exception:
            return False

    UNREACHABLE = ("PC에서 폰의 그 주소로 접속이 안 돼요.\n"
                   "• 폰의 페어링 창이 닫히면 포트가 사라져요. 창을 띄운 채로, 새 IP:포트와 코드로 다시 하세요.\n"
                   "• 공유기의 게스트 Wi-Fi나 'AP 격리/무선 격리'가 켜져 있으면 유선 PC와 통신이 막혀요.")

    @staticmethod
    def network_hint(addr: str) -> str:
        """PC와 폰이 다른 네트워크(대역)에 있으면 그 사실을 알려주는 문구."""
        import socket
        try:
            host = addr.rsplit(":", 1)[0]
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.connect((host, 9))  # 패킷은 안 보냄. PC가 그 주소로 갈 때 쓰는 내 IP만 확인
            mine = u.getsockname()[0]
            u.close()
        except Exception:
            return ""
        if mine.split(".")[:3] != host.split(".")[:3]:
            return (f"⚠ PC의 IP는 {mine}, 폰은 {host} 예요. 서로 다른 네트워크라서 연결할 수 없어요.\n"
                    "PC가 연결된 공유기의 Wi-Fi에 폰을 연결하거나, PC를 폰과 같은 Wi-Fi에 연결하세요.\n"
                    "(회사망·게스트 Wi-Fi처럼 기기끼리 막힌 네트워크에서는 무선 연결이 안 돼요.)")
        return ""

    def unreachable_msg(self, addr: str) -> str:
        return self.network_hint(addr) or self.UNREACHABLE

    def pair(self, addr: str, code: str) -> tuple[bool, str]:
        # 페어링 포트에 미리 접속해보면 폰이 창을 닫을 수 있어서, 확인은 실패한 뒤에만 함
        try:
            out = self.adb_out("pair", addr, code, timeout=40)
        except subprocess.TimeoutExpired:
            if not self.reachable(addr):
                return False, self.unreachable_msg(addr)
            return False, ("응답이 없어요 (시간 초과).\n"
                           "• 폰에서 페어링 창을 닫았다가 새 코드로 다시 해보세요.\n"
                           "• 그래도 안 되면 런처를 껐다 켠 뒤 다시 시도하세요.")
        if "Successfully paired" in out:
            return True, out
        if "Wrong password" in out or "protocol fault" in out:
            return False, "코드가 틀렸거나 만료됐어요. 폰에서 새 코드를 띄워 다시 입력하세요."
        if not self.reachable(addr):
            return False, self.unreachable_msg(addr)
        return False, out

    def connect(self, addr: str, tries: int = 3) -> tuple[bool, str]:
        # 폰 화면이 꺼져 있으면 Wi-Fi가 절전 상태라 첫 응답이 늦어요 → 몇 번 두드리며 재시도
        out = ""
        for i in range(tries):
            self.reachable(addr, timeout=4)  # 깨우기용 접속 (실패해도 계속)
            try:
                out = self.adb_out("connect", addr, timeout=20)
            except subprocess.TimeoutExpired:
                out = "timeout"
            if ("connected to" in out) and "cannot" not in out and "failed" not in out:
                return True, out
            if "offline" in out or "already" in out:
                self.adb_out("disconnect", addr, timeout=8)
            time.sleep(1.5)
        if not self.reachable(addr, timeout=5):
            return False, (self.network_hint(addr) or
                           "PC에서 폰의 그 주소로 접속이 안 돼요.\n"
                           "• 무선 디버깅 화면 맨 위의 'IP 주소 및 포트'가 맞는지 확인하세요 (페어링 포트와 달라요).\n"
                           "• 폰 화면이 꺼져 있으면 Wi-Fi가 잠들 수 있어요. 폰 설정에서 Wi-Fi 절전을 꺼주세요.")
        return False, out

    MDNS_RE = re.compile(r"_adb-tls-(connect|pairing)\._tcp\.?\s+(\d+\.\d+\.\d+\.\d+:\d+)")

    def mdns(self) -> dict:
        """같은 네트워크의 무선 디버깅 폰 찾기 → {"connect": [...], "pairing": [...]}"""
        found = {"connect": [], "pairing": []}
        try:
            out = self.adb_out("mdns", "services", timeout=8)
        except Exception:
            return found
        for kind, addr in self.MDNS_RE.findall(out):
            if addr not in found[kind]:
                found[kind].append(addr)
        return found

    def shell(self, serial: str, *args: str, timeout: float = 10) -> str:
        self.ensure_server()
        r = run([self.adb, "-s", serial, "shell", *args], timeout=timeout)
        return r.stdout.decode("utf-8", "replace")

    def key(self, serial: str, keycode: int | str) -> None:
        self.shell(serial, "input", "keyevent", str(keycode))

    def is_awake(self, serial: str) -> bool:
        out = self.shell(serial, "dumpsys", "power", timeout=8)
        m = re.search(r"mWakefulness=(\w+)", out)
        return not m or m.group(1) == "Awake"

    def launch_via_adb(self, serial: str, package: str) -> None:
        self.shell(serial, "monkey", "-p", package, "-c",
                   "android.intent.category.LAUNCHER", "1")

    def launch_on_display(self, serial: str, package: str, display_id: int) -> bool:
        out = self.shell(serial, "cmd", "package", "resolve-activity", "--brief",
                         "-c", "android.intent.category.LAUNCHER", package)
        comp = next((l.strip() for l in reversed(out.splitlines()) if "/" in l), "")
        if not comp:
            return False
        r = self.shell(serial, "am", "start", "--display", str(display_id),
                       "-f", "0x10000000", "-n", comp)
        return "Error" not in r

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

    # --- 앱 아이콘 (폰에서 직접 가져오기) ---------------------------------------
    REMOTE_HELPER = "/data/local/tmp/pl_icons.jar"

    def fetch_icons(self, serial: str, pkgs: list[str], size: int = 96, chunk: int = 25):
        """폰에서 아이콘 PNG를 꺼내 (pkg, bytes) 묶음을 chunk 단위로 yield."""
        if not (self.adb and ICON_HELPER.is_file() and pkgs):
            return
        import base64
        self.ensure_server()
        r = run([self.adb, "-s", serial, "push", str(ICON_HELPER), self.REMOTE_HELPER], timeout=30)
        if r.returncode != 0:
            return
        try:
            for i in range(0, len(pkgs), chunk):
                part = pkgs[i:i + chunk]
                r = run([self.adb, "-s", serial, "shell", f"CLASSPATH={self.REMOTE_HELPER}",
                         "app_process", "/", "IconDump", str(size), *part], timeout=90)
                got = {}
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    bits = line.strip().split(" ", 1)
                    if len(bits) == 2 and not bits[1].startswith("-") and bits[0] in part:
                        try:
                            got[bits[0]] = base64.b64decode(bits[1])
                        except Exception:
                            pass
                yield got
        finally:
            try:
                self.shell(serial, "rm", "-f", self.REMOTE_HELPER)
            except Exception:
                pass

    def icon_diag(self, serial: str, pkgs: list[str]) -> str:
        """아이콘이 안 나올 때 원인 파악용 보고서."""
        lines = [f"helper: {ICON_HELPER} exists={ICON_HELPER.is_file()}"]
        try:
            lines.append("device: " + " / ".join(
                self.shell(serial, "getprop", k).strip() for k in
                ("ro.product.manufacturer", "ro.product.model", "ro.build.version.release", "ro.build.version.sdk")))
            r = run([self.adb, "-s", serial, "push", str(ICON_HELPER), self.REMOTE_HELPER], timeout=30)
            lines.append(f"push rc={r.returncode}: " + (r.stdout + r.stderr).decode("utf-8", "replace").strip()[-300:])
            r = run([self.adb, "-s", serial, "shell", "ICONDUMP_DEBUG=1", f"CLASSPATH={self.REMOTE_HELPER}",
                     "app_process", "/", "IconDump", "48", *pkgs], timeout=90)
            lines.append(f"run rc={r.returncode}")
            for l in (r.stdout + r.stderr).decode("utf-8", "replace").splitlines():
                bits = l.split(" ", 1)
                if len(bits) == 2 and len(bits[1]) > 200 and bits[1].startswith("iVBOR"):
                    l = f"{bits[0]} OK (png {len(bits[1])} chars)"
                lines.append(l[:400])
            self.shell(serial, "rm", "-f", self.REMOTE_HELPER)
        except Exception as e:
            lines.append(f"error: {e!r}")
        return "\n".join(lines[:150])

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
        # 홈 화면에 아이콘이 있는 앱은 시스템 앱(삼성 기본 앱 등)이어도 보여주기
        try:
            out = self.shell(serial, "cmd", "package", "query-activities", "--brief",
                             "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
                             timeout=20)
            launch = {l.strip().split("/")[0] for l in out.splitlines() if "/" in l}
            if launch:
                known = {a.package for a in apps}
                for a in apps:
                    a.launchable = a.package in launch
                for pkg in launch - known:
                    apps.append(AppInfo(pkg, pkg.split(".")[-1].capitalize(), False, True))
        except Exception:
            pass
        apps.sort(key=lambda a: a.name.lower())
        return apps

    # --- scrcpy --------------------------------------------------------------
    def base_args(self, serial: str, cfg: dict) -> list[str]:
        a = [self.scrcpy, "-s", serial, "--shortcut-mod=lalt,ralt"]
        if cfg["stay_awake"]:
            a.append("--stay-awake")
        if not cfg["audio"]:
            a.append("--no-audio")
        size, fps, rate = cfg["max_size"], cfg["max_fps"], cfg["bitrate_mbps"]
        if cfg.get("power_save"):  # 절전 모드: 인코딩 부담을 줄여 폰 발열/배터리 절약
            size = min(size or 9999, 1024)
            fps = min(fps or 999, 30)
            rate = min(rate, 4)
        if size:
            a.append(f"--max-size={size}")
        if fps:
            a.append(f"--max-fps={fps}")
        a.append(f"--video-bit-rate={rate}M")
        if cfg["always_on_top"]:
            a.append("--always-on-top")
        return a

    def battery(self, serial: str) -> dict:
        """dumpsys battery → {level, temp(°C), charging, source}"""
        out = self.shell(serial, "dumpsys", "battery", timeout=8)
        info = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        src = [n for k, n in (("AC powered", "충전기"), ("USB powered", "USB"),
                              ("Wireless powered", "무선충전")) if info.get(k) == "true"]
        try:
            temp = int(info.get("temperature", "0")) / 10
        except ValueError:
            temp = 0
        return {
            "level": info.get("level", "?"),
            "temp": temp,
            "charging": info.get("status") == "2",
            "full": info.get("status") == "5",
            "source": src[0] if src else "",
        }

    def start(self, args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            args, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
    icons = Signal(dict)
    battery = Signal(str, dict)
    call = Signal(object)
    call = Signal(object)  # 백그라운드 스레드에서 UI 스레드로 함수 실행 요청
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



ASPECTS = [  # (key, 표시 이름, 가로 기준 해상도)
    ("phone", "폰 화면 그대로 (미러링)", None),
    ("16:9", "16:9 와이드 (모니터·게임)", (1920, 1080)),
    ("16:10", "16:10 (노트북)", (1920, 1200)),
    ("4:3", "4:3 (태블릿)", (1600, 1200)),
    ("21:9", "21:9 울트라와이드", (2520, 1080)),
    ("custom", "직접 입력", None),
]


def aspect_display(cfg: dict) -> str | None:
    """설정된 비율 → scrcpy --new-display 값 (WxH/dpi). phone이면 None."""
    key = cfg.get("aspect", "phone")
    if key == "phone":
        return None
    if key == "custom":
        m = re.match(r"^\s*(\d+)\s*[x×*]\s*(\d+)", cfg.get("aspect_custom", ""))
        if not m:
            return None
        w, h = int(m.group(1)), int(m.group(2))
        w, h = max(w, h), min(w, h)
    else:
        w, h = next(r for k, _, r in ASPECTS if k == key)
    if not cfg.get("aspect_landscape", True):
        w, h = h, w
    dpi = max(160, min(480, int(min(w, h) / 1080 * 280)))  # 글자가 너무 작지 않게
    return f"{w}x{h}/{dpi}"


class WifiDialog(QDialog):
    """무선 디버깅으로 폰 연결 (페어링 + 연결)."""
    result = Signal(str, bool, str)  # 동작, 성공, 메시지
    found = Signal(dict)

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.phone = win.phone
        self.setWindowTitle("무선으로 폰 연결")
        self.setMinimumWidth(460)
        v = QVBoxLayout(self)
        v.setSpacing(10)

        guide = QLabel(
            "<b>폰 준비</b> (Android 11 이상, PC와 같은 공유기)<br>"
            "설정 → 개발자 옵션 → <b>무선 디버깅</b> 켜기<br>"
            "처음 한 번은 <b>'페어링 코드로 기기 페어링'</b>을 눌러<br>"
            "나오는 IP:포트와 6자리 코드를 아래 ①에 입력하세요."
        )
        guide.setWordWrap(True)
        v.addWidget(guide)

        btn_scan = QPushButton("🔍  네트워크에서 폰 자동 찾기")
        btn_scan.setObjectName("flat")
        btn_scan.clicked.connect(self.scan)
        v.addWidget(btn_scan)

        v.addWidget(QLabel("① 처음 한 번만: 페어링"))
        prow = QHBoxLayout()
        self.pair_addr = QLineEdit(placeholderText="예) 192.168.0.12:41234")
        self.pair_code = QLineEdit(placeholderText="6자리 코드")
        self.pair_code.setMaximumWidth(110)
        self.btn_pair = QPushButton("페어링")
        self.btn_pair.clicked.connect(self.do_pair)
        prow.addWidget(self.pair_addr, 1)
        prow.addWidget(self.pair_code)
        prow.addWidget(self.btn_pair)
        v.addLayout(prow)

        v.addWidget(QLabel("② 연결 (무선 디버깅 화면 위쪽의 'IP 주소 및 포트')"))
        crow = QHBoxLayout()
        self.conn_addr = QComboBox()
        self.conn_addr.setEditable(True)
        self.conn_addr.lineEdit().setPlaceholderText("예) 192.168.0.12:37555")
        if win.cfg.get("wifi_addr"):
            self.conn_addr.addItem(win.cfg["wifi_addr"])
        self.btn_conn = QPushButton("연결")
        self.btn_conn.clicked.connect(self.do_connect)
        crow.addWidget(self.conn_addr, 1)
        crow.addWidget(self.btn_conn)
        v.addLayout(crow)

        self.auto = QCheckBox("런처를 켤 때 자동으로 다시 연결")
        self.auto.setChecked(bool(win.cfg.get("wifi_auto", True)))
        self.auto.toggled.connect(lambda on: win._set("wifi_auto", on))
        v.addWidget(self.auto)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setObjectName("muted")
        v.addWidget(self.msg)

        tip = QLabel("무선은 USB보다 조금 느릴 수 있어요. 끊기면 설정 탭에서 화질(비트레이트)을 4~6 Mbps로 낮춰보세요.")
        tip.setWordWrap(True)
        tip.setObjectName("muted")
        v.addWidget(tip)

        self.result.connect(self._on_result)
        self.found.connect(self._on_found)
        self.scan()

    def _busy(self, text):
        self.msg.setText(text)

    def scan(self):
        if not self.phone.adb:
            self._busy("adb를 찾지 못했어요.")
            return
        self._busy("같은 네트워크에서 무선 디버깅이 켜진 폰을 찾는 중…")
        bg(lambda: self.found.emit(self.phone.mdns()))

    def _on_found(self, f: dict):
        for a in f["connect"]:
            if self.conn_addr.findText(a) < 0:
                self.conn_addr.insertItem(0, a)
        if f["connect"]:
            self.conn_addr.setCurrentIndex(0)
        if f["pairing"] and not self.pair_addr.text():
            self.pair_addr.setText(f["pairing"][0])
        if f["connect"] or f["pairing"]:
            self._busy(f"찾았어요: 연결 {len(f['connect'])}개, 페어링 대기 {len(f['pairing'])}개")
        else:
            self._busy("자동으로 못 찾았어요. 폰 화면의 IP:포트를 직접 입력하세요.")

    def do_pair(self):
        addr, code = self.pair_addr.text().strip(), self.pair_code.text().strip()
        if not addr or not code:
            self._busy("페어링 IP:포트와 6자리 코드를 입력하세요.")
            return
        self._busy("페어링 중…")

        def work():
            try:
                ok, out = self.phone.pair(addr, code)
            except Exception as e:
                ok, out = False, str(e)
            self.result.emit("pair", ok, out)
            if ok:  # 페어링되면 연결 포트를 찾아 바로 연결 시도
                time.sleep(1.5)
                self.found.emit(self.phone.mdns())
        bg(work)

    def do_connect(self):
        addr = self.conn_addr.currentText().strip()
        if not addr:
            self._busy("연결할 IP:포트를 입력하세요.")
            return
        if ":" not in addr:
            addr += ":5555"
        self._busy(f"{addr} 연결 중…")

        def work():
            try:
                ok, out = self.phone.connect(addr)
            except Exception as e:
                ok, out = False, str(e)
            self.result.emit("connect:" + addr, ok, out)
        bg(work)

    def _on_result(self, action: str, ok: bool, out: str):
        if action == "pair":
            self._busy("✅ 페어링 완료! 이제 ② 연결을 누르세요." if ok else
                       f"❌ 페어링 실패\n{out}")
            if ok and self.conn_addr.currentText():
                self.do_connect()
            return
        addr = action.split(":", 1)[1]
        if ok:
            self.win._set("wifi_addr", addr)
            self._busy(f"✅ {addr} 연결됐어요.")
            self.win.refresh_devices()
            QTimer.singleShot(800, self.accept)
        else:
            self._busy(f"❌ 연결 실패\n{out}\n처음이면 ① 페어링을 먼저 하세요.")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.bus = Bus()
        self.phone = Phone(None, None)
        self.devices: list[Device] = []
        self.apps: list[AppInfo] = []
        self.sessions: list[Session] = []
        self.icons: dict = {}

        self.setWindowTitle(f"{APP_NAME}  ·  {BUILD}")
        self.resize(560, 760)
        self._build_ui()
        self._wire()
        try:  # Windows 다크/라이트 설정이 바뀌면 바로 반영 (테마 = system 일 때)
            QApplication.styleHints().colorSchemeChanged.connect(
                lambda *_: self.cfg.get("theme") == "system" and self.apply_theme())
        except Exception:
            pass
        self._locate_tools()

        self.batt_timer = QTimer(self, interval=30000, timeout=self.refresh_battery)
        self.batt_timer.start()
        QTimer.singleShot(1500, self.refresh_battery)
        self.poll = QTimer(self, interval=3000, timeout=self._tick)
        self.poll.start()
        self.refresh_devices()
        self.auto_wifi()

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
        btn_wifi = QPushButton("📶")
        btn_wifi.setObjectName("flat")
        btn_wifi.setFixedWidth(38)
        btn_wifi.setToolTip("무선(Wi-Fi)으로 연결")
        btn_wifi.clicked.connect(self.open_wifi)
        top.addWidget(btn_wifi)
        top.addWidget(self.btn_mirror)
        v.addLayout(top)

        self.batt = QLabel("")
        self.batt.setObjectName("muted")
        self.batt.setToolTip("폰 배터리·충전·온도 (30초마다 갱신)")
        v.addWidget(self.batt)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        # 탭1: 앱
        apps_tab = QWidget()
        av = QVBoxLayout(apps_tab)
        av.setContentsMargins(0, 8, 0, 0)
        srow = QHBoxLayout()
        self.search = QLineEdit(placeholderText="🔍  앱 검색 (이름 또는 패키지)")
        self.chk_system = QCheckBox("숨은 앱까지")
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
            ("⟳", "가로/세로 전환", lambda s: self.rotate_view(s)),
            ("📷", "스크린샷 (사진 폴더에 저장)", self._screenshot),
            ("🔔", "알림창 내리기", lambda s: self.phone.shell(s, "cmd", "statusbar", "expand-notifications")),
            None,
            ("⏻", "폰 화면 켜기/끄기 (PC 창은 계속 보여요)", lambda s: self.bus_call(self.toggle_panel)),
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
        f.addRow(chk("power_save", "절전 모드 (1024px · 30fps · 4Mbps로 낮춰 폰 발열·배터리 절약)"))
        f.addRow(chk("separate_windows", "앱마다 새 창으로 열기 (가상 디스플레이, Android 10+)"))

        def spin(key, lo, hi, step, suffix):
            sb = QSpinBox(minimum=lo, maximum=hi, singleStep=step, suffix=suffix)
            sb.setValue(int(c[key]))
            sb.valueChanged.connect(lambda v: self._set(key, v))
            return sb

        f.addRow("최대 해상도", spin("max_size", 0, 4096, 160, " px (0=원본)"))
        f.addRow("최대 FPS", spin("max_fps", 15, 144, 15, " fps"))
        f.addRow("화질(비트레이트)", spin("bitrate_mbps", 1, 64, 1, " Mbps"))

        arow = QHBoxLayout()
        self.s_aspect = QComboBox()
        for key, label, _ in ASPECTS:
            self.s_aspect.addItem(label, key)
        self.s_aspect.setCurrentIndex(max(0, self.s_aspect.findData(c.get("aspect", "phone"))))
        self.s_custom = QLineEdit(c.get("aspect_custom", "1920x1080"), placeholderText="예) 2560x1080")
        self.s_custom.setMaximumWidth(120)
        self.s_land = QCheckBox("가로")
        self.s_land.setChecked(bool(c.get("aspect_landscape", True)))
        arow.addWidget(self.s_aspect, 1)
        arow.addWidget(self.s_custom)
        arow.addWidget(self.s_land)
        f.addRow("화면 비율", arow)

        def aspect_changed():
            self.cfg["aspect"] = self.s_aspect.currentData()
            self.cfg["aspect_custom"] = self.s_custom.text().strip()
            self.cfg["aspect_landscape"] = self.s_land.isChecked()
            save_config(self.cfg)
            self.s_custom.setVisible(self.cfg["aspect"] == "custom")
            self.s_land.setEnabled(self.cfg["aspect"] != "phone")
            self._reopen_main()
        self.s_aspect.currentIndexChanged.connect(lambda _: aspect_changed())
        self.s_custom.editingFinished.connect(aspect_changed)
        self.s_land.toggled.connect(lambda _: aspect_changed())
        self.s_custom.setVisible(c.get("aspect") == "custom")
        self.s_land.setEnabled(c.get("aspect", "phone") != "phone")
        note = QLabel("'폰 화면 그대로'가 아니면 원하는 비율의 가상 화면을 새로 만들어 앱을 그 모양에 맞게 띄워요 "
                      "(Android 10+). 일부 앱(은행·DRM 영상 등)은 가상 화면에서 안 열리거나 검게 나올 수 있어요.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        f.addRow(note)

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

        btn_diag = QPushButton("앱 아이콘 다시 받기 + 진단")
        btn_diag.setObjectName("flat")
        btn_diag.clicked.connect(self.icon_diagnose)
        f.addRow(btn_diag)

        self.s_nosave = QCheckBox("설정 저장 안 함 (PC에 설정 파일을 남기지 않음)")
        self.s_nosave.setChecked(NO_SAVE)
        self.s_nosave.toggled.connect(self._toggle_nosave)
        f.addRow(self.s_nosave)
        where = QLabel(f"설정 파일 위치: {CONFIG_PATH}")
        where.setObjectName("muted")
        where.setWordWrap(True)
        where.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow(where)
        return w

    # ---------------- 이벤트 연결 ----------------
    def _wire(self):
        self.bus.devices.connect(self._on_devices)
        self.bus.apps.connect(self._on_apps)
        self.bus.icons.connect(self._on_icons)
        self.bus.battery.connect(self._on_battery)
        self.bus.call.connect(lambda fn: fn())
        self.bus.call.connect(lambda fn: fn())
        self.bus.status.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.bus.error.connect(lambda m: QMessageBox.warning(self, APP_NAME, m))
        self.search.textChanged.connect(self._render_apps)
        self.chk_system.toggled.connect(lambda v: (self._set("show_system_apps", v), self._render_apps()))
        self.dev_combo.currentIndexChanged.connect(lambda _: (self.refresh_apps(), self._update_mirror_btn(), self.refresh_battery()))
        for lst in (self.app_list, self.fav_list):
            lst.itemClicked.connect(self._on_app_clicked)
            lst.customContextMenuRequested.connect(lambda pos, l=lst: self._app_menu(l, pos))

    def _toggle_nosave(self, on: bool):
        global NO_SAVE
        NO_SAVE = on
        if on:
            delete_saved_config()
            self.statusBar().showMessage("저장된 설정 파일을 지웠어요. 지금부터 바꾸는 설정은 끄면 사라져요.", 8000)
        else:
            save_config(self.cfg)
            self.statusBar().showMessage(f"설정을 저장했어요: {CONFIG_PATH}", 6000)

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

    def open_wifi(self):
        if not self.phone.adb:
            QMessageBox.warning(self, APP_NAME, "adb를 찾지 못했어요. 설정 탭에서 scrcpy 위치를 확인하세요.")
            return
        WifiDialog(self).exec()

    def auto_wifi(self):
        """시작할 때 마지막 무선 주소(또는 자동 검색된 폰)로 재연결."""
        if not self.phone.adb or not self.cfg.get("wifi_auto", True):
            return

        def work():
            try:
                addrs = self.phone.mdns()["connect"]
                last = self.cfg.get("wifi_addr")
                if last and last not in addrs:
                    addrs.append(last)
                for a in addrs:
                    ok, _ = self.phone.connect(a)
                    if ok:
                        self.cfg["wifi_addr"] = a
                        save_config(self.cfg)
                        self.bus.status.emit(f"📶 무선 연결됨: {a}")
                        break
            except Exception:
                pass
            self.bus.devices.emit(self._safe(self.phone.devices, default=[]))
        bg(work)

    def _maybe_reconnect(self):
        """무선 폰이 사라지거나 offline이면 15초마다 조용히 다시 연결 시도."""
        addr = self.cfg.get("wifi_addr")
        if not (addr and self.cfg.get("wifi_auto", True) and self.phone.adb):
            return
        if any(d.state == "device" for d in self.devices):
            return
        now = time.time()
        if getattr(self, "_reconn_busy", False) or now - getattr(self, "_reconn_at", 0) < 15:
            return
        self._reconn_at, self._reconn_busy = now, True

        def work():
            try:
                addrs = self.phone.mdns()["connect"]
                if addr not in addrs:
                    addrs.append(addr)
                for a in addrs:
                    ok, _ = self.phone.connect(a, tries=1)
                    if ok:
                        if a != addr:
                            self.cfg["wifi_addr"] = a
                            save_config(self.cfg)
                        self.bus.status.emit(f"📶 다시 연결됨: {a}")
                        break
            except Exception:
                pass
            finally:
                self._reconn_busy = False
        bg(work)

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
            self.dev_combo.addItem("연결된 폰 없음 — USB 연결 또는 📶 무선 연결")
        idx = next((i for i, d in enumerate(devs) if d.serial == cur), 0)
        self.dev_combo.setCurrentIndex(idx)
        self.dev_combo.blockSignals(False)
        unauthorized = [d for d in devs if d.state == "unauthorized"]
        if unauthorized:
            self.statusBar().showMessage("폰에서 'USB 디버깅 허용' 팝업을 눌러주세요.")
        self._update_mirror_btn()
        self.refresh_apps()
        self.refresh_battery()

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
        self.load_icons(serial, apps)

    # ---------------- 앱 아이콘 ----------------
    def load_icons(self, serial: str, apps: list):
        """캐시 → 없으면 폰에서 가져오기. 백그라운드에서 돌고, 받는 대로 화면에 반영."""
        if getattr(self, "_icon_job", None) == serial:
            return
        self._icon_job = serial

        def work():
            try:
                missing, cached = [], {}
                # 사용자 앱 먼저, 시스템 앱은 나중에
                for a in sorted(apps, key=lambda a: a.system):
                    if a.package in self.icons:
                        continue
                    f = ICON_DIR / f"{a.package}.png"
                    if not NO_SAVE and f.is_file():
                        cached[a.package] = f.read_bytes()
                    else:
                        missing.append(a.package)
                if cached:
                    self.bus.icons.emit(cached)
                if missing and ICON_HELPER.is_file():
                    self.bus.status.emit(f"폰에서 앱 아이콘 가져오는 중… ({len(missing)}개)")
                    for got in self.phone.fetch_icons(serial, missing):
                        if not got:
                            continue
                        if not NO_SAVE:
                            try:
                                ICON_DIR.mkdir(parents=True, exist_ok=True)
                                for pkg, data in got.items():
                                    (ICON_DIR / f"{pkg}.png").write_bytes(data)
                            except Exception:
                                pass
                        self.bus.icons.emit(got)
                    self.bus.status.emit("앱 아이콘을 다 가져왔어요.")
            except Exception as e:
                self.bus.status.emit(f"아이콘 가져오기 실패: {e}")
            finally:
                self._icon_job = None
        bg(work)

    def icon_diagnose(self):
        serial = self.current_serial()
        if not serial:
            QMessageBox.information(self, APP_NAME, "연결된 폰이 없어요.")
            return
        # 캐시를 비우고 다시 받기
        self.icons.clear()
        shutil.rmtree(ICON_DIR, ignore_errors=True)
        sample = [a.package for a in self.apps if not a.system][:3] or [a.package for a in self.apps][:3]
        self.statusBar().showMessage("아이콘 진단 중…")

        def work():
            report = self.phone.icon_diag(serial, sample)
            self.bus.call.emit(lambda: self._show_report(report))
            self.load_icons(serial, self.apps)
        bg(work)

    def _show_report(self, text: str):
        from PySide6.QtWidgets import QPlainTextEdit
        d = QDialog(self)
        d.setWindowTitle("아이콘 진단 결과")
        d.resize(640, 420)
        v = QVBoxLayout(d)
        v.addWidget(QLabel("아이콘이 계속 안 나오면 이 내용을 복사해서 보내주세요."))
        box = QPlainTextEdit(text)
        box.setReadOnly(True)
        v.addWidget(box, 1)
        b = QPushButton("복사하고 닫기")
        b.clicked.connect(lambda: (QApplication.clipboard().setText(text), d.accept()))
        v.addWidget(b)
        d.exec()

    def _on_icons(self, got: dict):
        for pkg, data in got.items():
            pm = QPixmap()
            if pm.loadFromData(data):
                self.icons[pkg] = QIcon(pm)
        for lst in (self.app_list, self.fav_list):
            for i in range(lst.count()):
                it = lst.item(i)
                ic = self.icons.get(it.data(Qt.UserRole))
                if ic:
                    it.setIcon(ic)

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
            if not a.launchable and not show_sys:
                continue
            if q and q not in a.name.lower() and q not in a.package.lower():
                continue
            self.app_list.addItem(self._item(a))

    def _item(self, a: AppInfo) -> QListWidgetItem:
        it = QListWidgetItem(self.icons.get(a.package) or letter_icon(a.name, a.package), a.name)
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

    def _reopen_main(self):
        """비율이 바뀌면 열려 있는 폰 화면 창을 새 설정으로 다시 연다."""
        serial = self.current_serial()
        s = serial and self._main_session(serial)
        if not s:
            return
        s.proc.terminate()

        def again():
            try:
                s.proc.wait(timeout=3)
            except Exception:
                pass
            self.bus.call.emit(lambda: (self.open_main(serial), self._update_mirror_btn()))
        bg(again)

    def rotate_view(self, serial: str):
        if self.cfg.get("aspect", "phone") == "phone":
            self.phone.rotate(serial)
        else:  # 비율 모드: 가로/세로를 바꿔서 창을 다시 연다
            self.cfg["aspect_landscape"] = not self.cfg.get("aspect_landscape", True)
            save_config(self.cfg)
            self.bus.call.emit(lambda: (self.s_land.blockSignals(True),
                                          self.s_land.setChecked(self.cfg["aspect_landscape"]),
                                          self.s_land.blockSignals(False), self._reopen_main()))

    def open_main(self, serial: str, start_app: str | None = None) -> Session:
        args = self.phone.base_args(serial, self.cfg)
        vd = aspect_display(self.cfg)
        if vd:  # 원하는 비율의 가상 화면을 만들어서 그 안에서 앱 실행 (폰 본체 화면과 별개)
            args.append(f"--new-display={vd}")
        elif self.cfg["turn_screen_off"]:
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
                if main.display_id is not None:
                    if self.phone.launch_on_display(serial, pkg, main.display_id):
                        return
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
        lines = []
        for raw in iter(s.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            lines = (lines + [line])[-60:]
            m = re.search(r"New display:.*\(id=(\d+)\)", line)
            if m:
                s.display_id = int(m.group(1))
        s.proc.wait()
        if s.proc.returncode not in (0, None) and time.time() - s.started < 8:
            msg = lines
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

    # ---------------- 배터리/발열 ----------------
    def refresh_battery(self):
        serial = self.current_serial()
        if not serial or not self.phone.adb:
            self.batt.setText("")
            return

        def work():
            try:
                self.bus.battery.emit(serial, self.phone.battery(serial))
            except Exception:
                pass
        bg(work)

    def _on_battery(self, serial: str, b: dict):
        if serial != self.current_serial():
            return
        if b["full"]:
            charge = "충전 완료"
        elif b["charging"]:
            charge = f"⚡ 충전 중 ({b['source']})" if b["source"] else "⚡ 충전 중"
        else:
            charge = "배터리 사용 중"
        t = b["temp"]
        if t >= 45:
            color, warn = "#EB5757", "  ⚠ 너무 뜨거워요! 폰 화면 창을 닫고 식혀주세요"
        elif t >= 40:
            color, warn = "#F2994A", "  ⚠ 조금 뜨거워요 (설정 → 절전 모드 추천)"
        else:
            color, warn = "", ""
        temp = f"<span style='color:{color}'>🌡 {t:.1f}°C{warn}</span>" if color else f"🌡 {t:.1f}°C"
        self.batt.setText(f"🔋 {b['level']}%  ·  {charge}  ·  {temp}")

    # ---------------- 폰 본체 화면 끄기/켜기 (scrcpy 방식: 폰은 깨어 있고 화면 패널만 끔) ----------------
    def bus_call(self, fn):
        self.bus.call.emit(fn)  # 어느 스레드에서 불러도 UI 스레드에서 실행

    _ui = bus_call

    def _send_scrcpy_keys(self, s: Session, shift: bool) -> bool:
        """폰 화면 창에 Alt+O(끄기) / Alt+Shift+O(켜기)를 보낸다. 창을 앞으로 못 가져오면 False."""
        if not IS_WIN:
            return False
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, s.title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if user32.GetForegroundWindow() != hwnd:
            return False  # 엉뚱한 창에 단축키가 가지 않게
        VK_MENU, VK_SHIFT, VK_O, UP = 0x12, 0x10, 0x4F, 0x0002
        keys = [VK_MENU] + ([VK_SHIFT] if shift else []) + [VK_O]
        for k in keys:
            user32.keybd_event(k, 0, 0, 0)
        for k in reversed(keys):
            user32.keybd_event(k, 0, UP, 0)
        return True

    def toggle_panel(self):
        serial = self.current_serial()
        s = serial and self._main_session(serial)
        if not s:
            self.statusBar().showMessage("먼저 '폰 화면 열기'를 눌러주세요.", 4000)
            return
        turn_on = getattr(s, "panel_off", bool(self.cfg["turn_screen_off"]))
        if self._send_scrcpy_keys(s, shift=turn_on):
            s.panel_off = not turn_on
            self.statusBar().showMessage("폰 화면을 켰어요." if turn_on else "폰 화면을 껐어요. PC에서는 계속 보여요.", 4000)
        else:
            self.statusBar().showMessage("폰 화면 창을 클릭한 뒤 Alt+O(끄기) / Alt+Shift+O(켜기)를 눌러주세요.", 6000)

    def _keep_awake_watch(self):
        """폰 전원 버튼을 눌러 폰이 잠들면 PC 화면이 까매짐 → 깨우고 폰 화면 창을 다시 열어 화면 끄기를 다시 적용."""
        serial = self.current_serial()
        s = serial and self._main_session(serial)
        if not s or getattr(self, "_watch_busy", False) or time.time() - s.started < 6:
            return
        self._watch_busy = True

        def work():
            try:
                if not self.phone.is_awake(serial):
                    self.phone.key(serial, 224)  # WAKEUP
                    self.bus.status.emit("폰이 잠들어서 다시 깨웠어요 (폰 전원 버튼 대신 런처의 ⏻ 버튼을 쓰세요).")
                    s.proc.terminate()
                    try:
                        s.proc.wait(timeout=4)
                    except Exception:
                        pass
                    time.sleep(0.8)
                    self._ui(lambda: (self.open_main(serial), self._update_mirror_btn()))
            except Exception:
                pass
            finally:
                self._watch_busy = False
        bg(work)

    def _tick(self):
        self._keep_awake_watch()
        self._maybe_reconnect()
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
    if IS_WIN:  # 작업표시줄에 파이썬 아이콘 대신 런처 아이콘이 보이게
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PhoneLauncher.App")
        except Exception:
            pass
    icon_file = APP_DIR / "assets" / "icon.png"
    app.setWindowIcon(QIcon(str(icon_file)) if icon_file.is_file() else letter_icon("P", "phone", 128))
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
