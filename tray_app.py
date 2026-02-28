import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

try:
    import webview
except Exception:
    webview = None

import app as backend

ERROR_ALREADY_EXISTS = 183


def resource_path(filename: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / filename
    return Path(__file__).resolve().parent / filename


class SingleInstanceGuard:
    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def acquire(self) -> bool:
        if not sys.platform.startswith("win"):
            return True
        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            return True
        return ctypes.GetLastError() != ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if not self.handle:
            return
        ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle = None


def desktop_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/?role=desktop"


def service_health_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/health"


def service_is_ready(port: int, timeout: float = 4.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(service_health_url(port), timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def show_popup(message: str, title: str = "LAN 文件传输") -> None:
    if not sys.platform.startswith("win"):
        print(f"{title}: {message}")
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)
    except Exception:
        print(f"{title}: {message}")


def state_file_path() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else (Path.home() / "AppData" / "Local")
    state_dir = base / "LANFileTransfer"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "tray_state.json"


def write_active_port(port: int) -> None:
    try:
        state_file_path().write_text(
            json.dumps({"active_port": int(port)}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def read_active_port(default_port: int) -> int:
    try:
        payload = json.loads(state_file_path().read_text(encoding="utf-8"))
        active_port = int(payload.get("active_port", default_port))
        if 1 <= active_port <= 65535:
            return active_port
    except Exception:
        pass
    return default_port


def clear_active_port() -> None:
    try:
        state_file_path().unlink(missing_ok=True)
    except Exception:
        pass


def build_tray_icon(size: int = 64) -> Image.Image:
    logo_file = resource_path("logos.png")
    if logo_file.exists():
        try:
            return Image.open(logo_file).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            pass
    image = Image.new("RGB", (size, size), (30, 58, 95))
    draw = ImageDraw.Draw(image)
    pad = 10
    draw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=10, fill=(22, 101, 52))
    draw.rectangle((22, 30, 42, 35), fill=(255, 255, 255))
    draw.polygon([(42, 26), (50, 32), (42, 39)], fill=(255, 255, 255))
    return image


class DesktopBridgeApi:
    def choose_download_directory(self, current_dir: str = "") -> str:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                title="选择下载目录",
                initialdir=current_dir or str(backend.default_download_dir()),
                mustexist=False,
            )
            root.destroy()
            return selected or ""
        except Exception:
            return ""


class TrayController:
    def __init__(self, port: int, save_dir: Path):
        self.requested_port = port
        self.port = port
        self.save_dir = save_dir
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.icon: pystray.Icon | None = None
        self.window = None
        self.exiting = False

    def desktop_url(self) -> str:
        return desktop_url(self.port)

    def health_url(self) -> str:
        return service_health_url(self.port)

    def backend_command(self) -> list[str]:
        args = [
            "--backend",
            "--port",
            str(self.port),
            "--save-dir",
            str(self.save_dir),
            "--no-browser",
            "--no-terminal-qr",
        ]
        if getattr(sys, "frozen", False):
            return [sys.executable, *args]
        return [sys.executable, str(Path(__file__).resolve()), *args]

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                proc = self.process
            if proc and proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.health_url(), timeout=1.0) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def start_backend(self) -> bool:
        with self.lock:
            if self.process and self.process.poll() is None:
                return True

            selected_port = backend.find_available_port(self.requested_port)
            self.port = selected_port

            popen_kwargs = {}
            if sys.platform.startswith("win"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(self.backend_command(), **popen_kwargs)

        write_active_port(self.port)
        return self.wait_ready()

    def stop_backend(self) -> None:
        with self.lock:
            proc = self.process
            self.process = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        clear_active_port()

    def call_local_post(self, path: str, body: bytes = b"{}") -> bool:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    def on_window_closing(self):
        if self.exiting:
            return True
        try:
            if self.window:
                self.window.hide()
        except Exception:
            pass
        return False

    def show_window(self) -> None:
        try:
            if self.window:
                self.window.show()
                self.window.restore()
        except Exception:
            pass

    def open_settings_panel(self) -> None:
        self.show_window()
        try:
            if self.window:
                self.window.evaluate_js("if (window.__lftOpenSettings) window.__lftOpenSettings();")
        except Exception:
            pass

    def open_download_dir(self) -> None:
        if not self.call_local_post("/settings/open-download-dir"):
            show_popup("无法打开下载目录，请确认服务已启动且目录有效。")

    def restart_backend(self) -> None:
        self.stop_backend()
        if not self.start_backend():
            show_popup("服务重启失败，请检查端口占用。")
            return
        if self.window:
            try:
                self.window.load_url(self.desktop_url())
            except Exception:
                pass
        show_popup("服务已重启。")

    def quit_app(self) -> None:
        self.exiting = True
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
        try:
            if self.window:
                self.window.destroy()
        except Exception:
            pass

    def on_open(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.show_window()

    def on_open_settings(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.open_settings_panel()

    def on_open_download_dir(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self.open_download_dir, daemon=True).start()

    def on_restart(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self.restart_backend, daemon=True).start()

    def on_quit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.quit_app()

    def run_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", self.on_open),
            pystray.MenuItem("下载目录设置", self.on_open_settings),
            pystray.MenuItem("打开下载目录", self.on_open_download_dir),
            pystray.MenuItem("重启服务", self.on_restart),
            pystray.MenuItem("退出", self.on_quit),
        )
        self.icon = pystray.Icon("lan_file_transfer", build_tray_icon(), "LAN 文件传输", menu)
        self.icon.run()

    def run(self) -> None:
        if webview is None:
            show_popup("缺少 pywebview 依赖，请先执行 pip install -r requirements.txt")
            return

        if not self.start_backend():
            show_popup("后端服务启动失败，请检查端口占用或防火墙策略。")
            return
        if self.port != self.requested_port:
            show_popup(f"端口 {self.requested_port} 被占用，已切换到 {self.port}")

        tray_thread = threading.Thread(target=self.run_tray, daemon=True)
        tray_thread.start()

        api = DesktopBridgeApi()
        self.window = webview.create_window(
            title="LAN File Transfer",
            url=self.desktop_url(),
            js_api=api,
            width=1080,
            height=760,
            min_size=(860, 600),
        )
        self.window.events.closing += self.on_window_closing

        try:
            webview.start(private_mode=False)
        finally:
            self.exiting = True
            if self.icon:
                try:
                    self.icon.stop()
                except Exception:
                    pass
            self.stop_backend()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LAN file transfer tray launcher")
    parser.add_argument("--backend", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--save-dir", default=None, help="保存目录（默认自动选择）")
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-terminal-qr", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    save_dir = backend.resolve_save_dir(args.save_dir)
    if args.backend:
        backend.start_server(
            port=args.port,
            save_dir=save_dir,
            auto_open_browser=not args.no_browser,
            print_terminal_qr=not args.no_terminal_qr,
            strict_port=True,
        )
        return

    guard = SingleInstanceGuard("LANFileTransfer.Tray.Singleton")
    if not guard.acquire():
        active_port = read_active_port(args.port)
        if service_is_ready(active_port, timeout=6.0):
            show_popup("程序已在运行，请切换到已有窗口。")
        else:
            show_popup("程序已在运行。")
        return

    try:
        TrayController(port=args.port, save_dir=save_dir).run()
    finally:
        guard.release()


if __name__ == "__main__":
    main()
