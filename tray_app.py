import argparse
import ctypes
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import pystray
from PIL import Image, ImageDraw

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
    return f"http://{backend.get_lan_ip()}:{port}/?role=desktop"


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


class TrayController:
    def __init__(self, port: int, save_dir: Path):
        self.port = port
        self.save_dir = save_dir
        self.process: subprocess.Popen | None = None
        self.icon: pystray.Icon | None = None
        self.lock = threading.Lock()

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

            popen_kwargs = {}
            if sys.platform.startswith("win"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(self.backend_command(), **popen_kwargs)
        return self.wait_ready()

    def stop_backend(self) -> None:
        with self.lock:
            proc = self.process
            self.process = None
        if not proc:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)

    def ensure_running(self) -> bool:
        with self.lock:
            running = bool(self.process and self.process.poll() is None)
        if running:
            return True
        return self.start_backend()

    def open_page(self) -> None:
        if not self.ensure_running():
            self.notify("LAN 文件传输", "后端服务启动失败")
            return
        webbrowser.open(self.desktop_url(), new=1)

    def restart_backend(self) -> None:
        self.stop_backend()
        if self.start_backend():
            self.notify("LAN 文件传输", "服务已重启")
            webbrowser.open(self.desktop_url(), new=1)
        else:
            self.notify("LAN 文件传输", "服务重启失败，请检查端口占用")

    def notify(self, title: str, message: str) -> None:
        if not self.icon:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def on_open(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self.open_page, daemon=True).start()

    def on_restart(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        threading.Thread(target=self.restart_backend, daemon=True).start()

    def on_quit(self, icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.stop_backend()
        icon.stop()

    def run(self) -> None:
        if self.start_backend():
            webbrowser.open(self.desktop_url(), new=1)
        image = build_tray_icon()
        menu = pystray.Menu(
            pystray.MenuItem("打开传输页面", self.on_open),
            pystray.MenuItem("重启服务", self.on_restart),
            pystray.MenuItem("退出", self.on_quit),
        )
        self.icon = pystray.Icon("lan_file_transfer", image, "LAN 文件传输", menu)
        self.icon.run()


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
        )
        return

    guard = SingleInstanceGuard(f"LANFileTransfer.Tray.{args.port}")
    if not guard.acquire():
        if service_is_ready(args.port, timeout=6.0):
            webbrowser.open(desktop_url(args.port), new=1)
            show_popup("程序已在运行，已为你打开传输页面。")
        else:
            show_popup("程序已在运行，请在系统托盘中操作。")
        return

    try:
        TrayController(port=args.port, save_dir=save_dir).run()
    finally:
        guard.release()


if __name__ == "__main__":
    main()
