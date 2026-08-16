import argparse
import base64
import errno
import io
import ipaddress
import ctypes
from ctypes import wintypes
import json
import logging
import os
import random
import secrets
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_logger = logging.getLogger("LANFileTransfer")

from flask import Flask, jsonify, make_response, render_template, request, send_file
from flask_sock import Sock
from qrcode import QRCode
import requests
from werkzeug.wsgi import ClosingIterator

APP_NAME = "LANFileTransfer"
DESKTOP_DEVICE_ID = "desktop"


def is_usable_ipv4(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(str(ip_text or "").strip())
    except ValueError:
        return False
    return (
        ip.version == 4
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_unspecified
        and not ip.is_multicast
    )


def ipv4_priority_key(ip_text: str) -> tuple[int, str]:
    ip = ipaddress.ip_address(ip_text)
    # 优先私网地址，其次其他可用 IPv4。
    return (0 if ip.is_private else 1, str(ip))


def get_lan_ipv4_candidates() -> list[str]:
    primary: list[str] = []  # 默认路由出口（UDP connect 成功）——真实对外网卡，优先
    secondary: list[str] = []
    seen: set[str] = set()

    def push(value: str, is_primary: bool = False) -> None:
        ip_text = str(value or "").strip()
        if not ip_text or ip_text in seen or not is_usable_ipv4(ip_text):
            return
        seen.add(ip_text)
        (primary if is_primary else secondary).append(ip_text)

    # UDP connect 成功意味着该 IP 是默认路由出口（真实上网网卡）。
    # 注意：不能把所有来源混在一起按字符串排序——Hyper-V/WSL 虚拟网卡
    # （如 172.21.x、172.30.x）会排在真实局域网 IP（如 192.168.1.x）前面，
    # 导致二维码指向手机不可达的虚拟网卡 IP。
    for endpoint in (("8.8.8.8", 80), ("1.1.1.1", 80), ("223.5.5.5", 80)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(endpoint)
            push(sock.getsockname()[0], is_primary=True)
        except OSError:
            continue
        finally:
            sock.close()

    # 兜底：无外网（UDP connect 全部失败）时，用"存在真实网关"识别真实局域网网卡。
    # Hyper-V/WSL 虚拟网卡的网关通常为 0.0.0.0，不会进入 primary。
    if sys.platform.startswith("win"):
        for ip_str, _mask, _bcast, gateway in _windows_adapters():
            if gateway:
                push(ip_str, is_primary=True)

    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        host_ips = []
    for ip_text in host_ips:
        push(ip_text)

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        infos = []
    for info in infos:
        sockaddr = info[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            push(str(sockaddr[0]))

    primary.sort(key=ipv4_priority_key)
    secondary.sort(key=ipv4_priority_key)
    return primary + secondary


def get_lan_ip() -> str:
    candidates = get_lan_ipv4_candidates()
    selected = get_selected_lan_ip(candidates)
    if selected:
        return selected
    if candidates:
        return candidates[0]
    return "127.0.0.1"


def get_selected_lan_ip(candidates: list[str]) -> Optional[str]:
    settings = load_runtime_settings()
    preferred = settings.get("selected_lan_ip")
    if not preferred or not isinstance(preferred, str):
        return None
    preferred = preferred.strip()
    if preferred in candidates:
        return preferred
    return None


def _windows_adapters() -> list[tuple[str, str, str, str]]:
    """Windows: 通过 iphlpapi.GetAdaptersInfo 解析网卡 (ip, mask, broadcast, gateway)。"""
    results: list[tuple[str, str, str, str]] = []
    if not sys.platform.startswith("win"):
        return results
    try:
        MAX_ADAPTER_NAME_LENGTH = 256
        MAX_ADAPTER_DESCRIPTION_LENGTH = 128
        MAX_ADAPTER_ADDRESS_LENGTH = 8

        class _IP_ADDR_STRING(ctypes.Structure):
            pass

        _IP_ADDR_STRING._fields_ = [
            ("Next", ctypes.POINTER(_IP_ADDR_STRING)),
            ("IpAddress", ctypes.c_char * 16),
            ("IpMask", ctypes.c_char * 16),
            ("Context", wintypes.DWORD),
        ]

        class _IP_ADAPTER_INFO(ctypes.Structure):
            pass

        _IP_ADAPTER_INFO._fields_ = [
            ("Next", ctypes.POINTER(_IP_ADAPTER_INFO)),
            ("ComboIndex", wintypes.DWORD),
            ("AdapterName", ctypes.c_char * (MAX_ADAPTER_NAME_LENGTH + 4)),
            ("Description", ctypes.c_char * (MAX_ADAPTER_DESCRIPTION_LENGTH + 4)),
            ("AddressLength", wintypes.UINT),
            ("Address", ctypes.c_ubyte * MAX_ADAPTER_ADDRESS_LENGTH),
            ("Index", wintypes.DWORD),
            ("Type", wintypes.UINT),
            ("DhcpEnabled", wintypes.UINT),
            ("CurrentIpAddress", ctypes.POINTER(_IP_ADDR_STRING)),
            ("IpAddressList", _IP_ADDR_STRING),
            ("GatewayList", _IP_ADDR_STRING),
            ("DhcpServer", _IP_ADDR_STRING),
            ("HaveWins", wintypes.BOOL),
            ("PrimaryWinsServer", _IP_ADDR_STRING),
            ("SecondaryWinsServer", _IP_ADDR_STRING),
            ("LeaseObtained", wintypes.DWORD),
            ("LeaseExpires", wintypes.DWORD),
        ]

        iphlpapi = ctypes.windll.iphlpapi
        size = wintypes.ULONG(0)
        ret = iphlpapi.GetAdaptersInfo(None, ctypes.byref(size))
        if ret == 111:  # ERROR_BUFFER_OVERFLOW
            buffer = ctypes.create_string_buffer(size.value)
            adapter_ptr = ctypes.cast(buffer, ctypes.POINTER(_IP_ADAPTER_INFO))
            ret = iphlpapi.GetAdaptersInfo(adapter_ptr, ctypes.byref(size))
            if ret == 0:  # NO_ERROR
                current = adapter_ptr
                while current:
                    info = current.contents
                    gateway = ""
                    gw = info.GatewayList
                    gw_ip = gw.IpAddress.decode("ascii", errors="ignore").strip("\x00")
                    if gw_ip and gw_ip != "0.0.0.0":
                        gateway = gw_ip
                    addr = info.IpAddressList
                    while True:
                        ip_str = addr.IpAddress.decode("ascii", errors="ignore").strip("\x00")
                        mask_str = addr.IpMask.decode("ascii", errors="ignore").strip("\x00")
                        if ip_str and mask_str and ip_str != "0.0.0.0":
                            try:
                                ip_obj = ipaddress.ip_address(ip_str)
                                mask_obj = ipaddress.ip_address(mask_str)
                                if ip_obj.version == 4 and mask_obj.version == 4:
                                    # Calculate prefix length from mask
                                    mask_int = int(mask_obj)
                                    prefix_len = bin(mask_int).count("1")
                                    network = ipaddress.ip_network(f"{ip_str}/{prefix_len}", strict=False)
                                    results.append((ip_str, mask_str, str(network.broadcast_address), gateway))
                            except ValueError:
                                pass
                        if addr.Next:
                            addr = addr.Next.contents
                        else:
                            break
                    current = info.Next
    except Exception:
        pass
    return results


def get_interface_subnet_info() -> list[tuple[str, str, str]]:
    """Returns list of (ip_address, subnet_mask, broadcast_address) tuples from actual OS network config."""
    if sys.platform.startswith("win"):
        return [(ip, mask, bcast) for ip, mask, bcast, _gateway in _windows_adapters()]

    # Linux/Mac: try netifaces if available, else fall back
    results: list[tuple[str, str, str]] = []
    try:
        import fcntl
        import struct
        SIOCGIFNETMASK = 0x891B
        SIOCGIFBRDADDR = 0x8919
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for ifname in socket.if_nameindex():
                name = ifname[1]
                try:
                    ifreq = struct.pack("256s", name[:15].encode("utf-8"))
                    mask_raw = fcntl.ioctl(sock.fileno(), SIOCGIFNETMASK, ifreq)
                    mask = socket.inet_ntoa(mask_raw[20:24])
                    brd_raw = fcntl.ioctl(sock.fileno(), SIOCGIFBRDADDR, ifreq)
                    brd = socket.inet_ntoa(brd_raw[20:24])
                    # Get IP via getsockname-like approach or just use the interface
                    try:
                        ip_raw = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
                        ip = socket.inet_ntoa(ip_raw[20:24])
                    except OSError:
                        ip = ""
                    if ip and ip != "0.0.0.0" and mask != "0.0.0.0" and not ip.startswith("127."):
                        results.append((ip, mask, brd))
                except OSError:
                    continue
        finally:
            sock.close()
    except (ImportError, OSError):
        pass

    return results


def infer_directed_broadcast_targets(ipv4_addresses: list[str]) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()

    def push(ip_text: str) -> None:
        value = str(ip_text or "").strip()
        if not value or value in seen:
            return
        seen.add(value)
        targets.append(value)

    push("255.255.255.255")

    # First priority: actual subnet broadcast addresses from OS
    subnet_info = get_interface_subnet_info()
    for ip_str, _mask_str, broadcast_addr in subnet_info:
        if ip_str in ipv4_addresses and broadcast_addr and broadcast_addr != "255.255.255.255":
            push(broadcast_addr)

    # Fallback: /24 heuristic for any IP not covered by actual subnet info
    covered_ips = {item[0] for item in subnet_info}
    for ip_text in ipv4_addresses:
        if not is_usable_ipv4(ip_text) or ip_text in covered_ips:
            continue
        try:
            iface_ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        network = ipaddress.ip_network(f"{iface_ip}/24", strict=False)
        broadcast = str(network.broadcast_address)
        if broadcast != "255.255.255.255":
            push(broadcast)

    return targets


def find_available_port(start_port: int, host: str = "0.0.0.0", max_tries: int = 100) -> int:
    port = start_port
    for _ in range(max_tries):
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            test_sock.bind((host, port))
            return port
        except OSError:
            port += 1
        finally:
            test_sock.close()
    raise RuntimeError(f"未找到可用端口，起始端口: {start_port}，尝试次数: {max_tries}")


def print_qr(url: str) -> None:
    qr = QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    print("\nScan QR in phone browser:")
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("QR rendering skipped: terminal encoding does not support block characters.")
        print(f"Open URL manually: {url}")


def build_qr_data_url(url: str) -> str:
    qr = QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def runtime_template_dir() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass) / "templates"
    return Path(__file__).resolve().parent / "templates"


def persistent_app_data_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    meipass_path = Path(meipass).resolve() if meipass else None
    appdata = os.getenv("APPDATA")
    candidates = []
    if appdata:
        candidates.append((Path(appdata) / APP_NAME).resolve())
    candidates.append(Path(os.path.dirname(sys.executable)).resolve())

    for candidate in candidates:
        if meipass_path is not None and (candidate == meipass_path or meipass_path in candidate.parents):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except Exception:
            continue
        return candidate

    raise RuntimeError("无法创建持久化数据目录")


def history_db_path() -> Path:
    return persistent_app_data_dir() / "history.db"


def default_save_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = os.getenv("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "LANFileTransfer" / "received_files"
    return Path(__file__).resolve().parent / "received_files"


def default_transient_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = os.getenv("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "LANFileTransfer" / "transient_uploads"
    return Path(__file__).resolve().parent / "transient_uploads"


def default_download_dir() -> Path:
    if sys.platform.startswith("win"):
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            downloads_key = "{374DE290-123F-4565-9164-39C4925E467B}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw_value, _ = winreg.QueryValueEx(key, downloads_key)
            if isinstance(raw_value, bytes):
                raw_text = raw_value.decode("utf-16-le", errors="ignore").rstrip("\x00")
            else:
                raw_text = str(raw_value)
            expanded = os.path.expandvars(raw_text.strip())
            if expanded:
                return Path(expanded).expanduser().resolve()
        except Exception:
            pass

        user_profile = os.getenv("USERPROFILE")
        if user_profile:
            return (Path(user_profile) / "Downloads").resolve()
    return (Path.home() / "Downloads").resolve()


_settings_lock = threading.Lock()


def settings_file_path() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    settings_dir = (base / "LANFileTransfer").resolve()
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.json"


def _read_settings_unlocked() -> dict:
    path = settings_file_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_settings_unlocked(settings: dict) -> None:
    path = settings_file_path()
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_runtime_settings() -> dict:
    with _settings_lock:
        return _read_settings_unlocked()


def save_runtime_settings(settings: dict) -> None:
    with _settings_lock:
        _write_settings_unlocked(settings)


def normalize_download_dir(raw_dir: str) -> Optional[Path]:
    if not raw_dir:
        return None
    normalized_text = os.path.expandvars(raw_dir.strip().strip("'\""))
    candidate = Path(normalized_text).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate.resolve()


def normalize_uploaded_filename(raw_name: str) -> str:
    raw_text = str(raw_name or "").strip().strip("'\"")
    if not raw_text:
        return "downloaded_file"
    flattened = raw_text.replace("\\", "/")
    base_name = flattened.rsplit("/", 1)[-1].strip()
    return base_name or "downloaded_file"


def sanitize_filename_for_windows(name: str) -> str:
    invalid = '<>:"/\\|?*'
    result = "".join("_" if ch in invalid else ch for ch in (name or ""))
    result = result.strip(" .")
    return result or "downloaded_file"


def allocate_unique_file_path(directory: Path, desired_name: str, reserve: bool = False) -> Path:
    clean_name = sanitize_filename_for_windows(desired_name)
    stem = Path(clean_name).stem or "downloaded_file"
    suffix = Path(clean_name).suffix
    candidate = directory / clean_name
    index = 1
    while True:
        if not reserve:
            if not candidate.exists():
                return candidate
        else:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            try:
                fd = os.open(str(candidate), flags)
            except FileExistsError:
                pass
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
            else:
                os.close(fd)
                return candidate
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1


def resolve_save_dir(raw_save_dir: Optional[str]) -> Path:
    if not raw_save_dir:
        return default_save_dir().resolve()

    save_dir = Path(raw_save_dir)
    if save_dir.is_absolute():
        return save_dir.resolve()

    if getattr(sys, "frozen", False):
        return (default_save_dir().parent / save_dir).resolve()

    base_dir = Path(__file__).resolve().parent
    return (base_dir / save_dir).resolve()


def attach_response_close_hooks(response):
    """send_file 返回的响应是 direct_passthrough 模式，WSGI 服务器不会调用
    Response.close()，导致 call_on_close 回调（状态标记/瞬态清理）永不执行。
    手动将响应体包装为 ClosingIterator，保证响应结束（含客户端中断）时触发回调。

    注意：ClosingIterator 会把内层迭代器的 close 加入回调首位且无幂等保护，
    因此关闭前必须先把 response.response 摘除（置空），避免递归 close。
    """
    if not getattr(response, "direct_passthrough", False):
        return response
    inner = response.response

    def _close():
        try:
            if hasattr(inner, "close"):
                inner.close()
        finally:
            response.response = ()  # 摘除 ClosingIterator，防止 close 递归
            response.close()

    response.response = ClosingIterator(inner, callbacks=[_close])
    return response


def normalize_device_identifier(raw: Optional[str], max_len: int = 120) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    safe = "".join(ch for ch in value[:max_len] if ch.isalnum() or ch in ("-", "_"))
    return safe


def normalize_device_name(raw: Optional[str], fallback: str = "电脑端", max_len: int = 80) -> str:
    name = str(raw or "").strip()
    if not name:
        return fallback
    return name[:max_len]


def load_or_create_local_device_identity() -> tuple[str, str]:
    settings = load_runtime_settings()
    device_id = normalize_device_identifier(settings.get("desktop_device_id"))
    if not device_id:
        device_id = uuid.uuid4().hex
        settings["desktop_device_id"] = device_id

    fallback_name = socket.gethostname() or "电脑端"
    device_name = normalize_device_name(settings.get("desktop_device_name"), fallback=fallback_name)
    if settings.get("desktop_device_name") != device_name:
        settings["desktop_device_name"] = device_name

    save_runtime_settings(settings)
    return device_id, device_name


def create_app(
    upload_dir: Path,
    transient_upload_dir: Path,
    base_url: str,
    lan_ip: str,
    lan_ip_candidates: Optional[list[str]],
    http_port: int,
    local_device_id: str,
    local_device_name: str,
    initial_mobile_token: str,
    token_ttl_seconds: int = 120,
    session_ttl_seconds: int = 8 * 60 * 60,
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024,
    download_dir: Optional[Path] = None,
    template_dir: Optional[Path] = None,
    history_db: Optional[Path] = None,
) -> Flask:
    app = Flask(__name__, template_folder=str(template_dir or runtime_template_dir()))
    app.config["UPLOAD_DIR"] = upload_dir
    app.config["TRANSIENT_UPLOAD_DIR"] = transient_upload_dir
    app.config["JSON_AS_ASCII"] = False
    app.config["BASE_URL"] = base_url
    app.config["TOKEN_TTL_SECONDS"] = token_ttl_seconds
    app.config["SESSION_TTL_SECONDS"] = session_ttl_seconds
    app.config["MAX_UPLOAD_BYTES"] = max_upload_bytes
    app.config["DOWNLOAD_DIR"] = (download_dir or default_download_dir()).resolve()
    app.config["HISTORY_DB_PATH"] = (history_db or history_db_path()).resolve()
    app.config["HISTORY_DB_PATH"].parent.mkdir(parents=True, exist_ok=True)

    sock = Sock(app)
    records = []
    record_map = {}
    clients = {}
    lock = threading.Lock()
    file_lock = threading.Lock()
    trusted_desktop_ips = {"127.0.0.1", "::1"}
    if is_usable_ipv4(lan_ip):
        trusted_desktop_ips.add(lan_ip)
    for candidate in (lan_ip_candidates or []):
        if is_usable_ipv4(candidate):
            trusted_desktop_ips.add(str(candidate))

    # 允许的浏览器 Origin 主机：本机/本机局域网地址（含 localhost 与主机名）。
    # 判定规则（见 origin_allowed）：
    #   1. 无 Origin（非浏览器客户端，如服务端间调用）→ 放行
    #   2. Origin 与请求 Host 一致（页面从本服务加载，同源）→ 放行，覆盖任意 IP/端口/转发场景
    #   3. Origin 主机是本机 IP/主机名且端口为本服务端口 → 放行
    #   4. 其余（跨站恶意页面）→ 拒绝
    http_port_int = int(http_port)
    allowed_origin_hosts = {"127.0.0.1", "localhost", "::1"}
    for ip_text in [lan_ip, *list(lan_ip_candidates or [])]:
        if is_usable_ipv4(ip_text):
            allowed_origin_hosts.add(ip_text)
    try:
        hostname = socket.gethostname()
        if hostname:
            allowed_origin_hosts.add(hostname.lower())
    except OSError:
        pass

    def origin_allowed(origin: str) -> bool:
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlsplit(origin)
        except ValueError:
            return False
        host_header = request.headers.get("Host", "")
        if host_header and parsed.netloc == host_header:
            return True
        if parsed.scheme != "http" or parsed.port != http_port_int:
            return False
        return (parsed.hostname or "").lower() in allowed_origin_hosts

    @app.before_request
    def guard_state_changing_origin():
        # 服务端到服务端（urllib/requests）调用不携带 Origin，不受影响；
        # 浏览器发起的写请求必须来自受信页面，否则拒绝。
        is_ws_upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
        if request.method in ("POST", "PUT", "PATCH", "DELETE") or is_ws_upgrade:
            origin = request.headers.get("Origin") or request.headers.get("Sec-WebSocket-Origin")
            if not origin_allowed(origin):
                _logger.warning(
                    "Rejected %s request from untrusted origin %s (%s %s)",
                    "WebSocket" if is_ws_upgrade else "state-changing",
                    origin,
                    request.method,
                    request.path,
                )
                return jsonify({"error": "跨站请求被拒绝"}), 403

    peer_discovery_port = 54546
    runtime_settings = load_runtime_settings()
    configured_port = runtime_settings.get("peer_discovery_port")
    if isinstance(configured_port, int) and 1024 <= configured_port <= 65535:
        peer_discovery_port = configured_port
    peer_announce_interval = 3.0
    peer_stale_seconds = 15
    pair_request_ttl_seconds = 120
    self_device_id = normalize_device_identifier(local_device_id) or uuid.uuid4().hex
    self_device_name = normalize_device_name(local_device_name, fallback=(socket.gethostname() or "电脑端"))
    app.config["SELF_DEVICE_ID"] = self_device_id
    app.config["SELF_DEVICE_NAME"] = self_device_name
    app.config["HTTP_PORT"] = int(http_port)
    mobile_device_names = {}
    latest_mobile_device_id = {"id": ""}
    discovered_desktops = {}
    paired_desktops = {}
    pending_pair_requests = {}
    outgoing_pair_requests = {}
    token_state = {
        "token": initial_mobile_token,
        "expires_at": time.time() + token_ttl_seconds,
        "consumed": False,
    }
    sessions = {}

    def cleanup_expired_sessions_locked(now: int) -> None:
        ttl = app.config["SESSION_TTL_SECONDS"]
        expired_ids = [sid for sid, s in sessions.items() if now - s["last_seen_at"] > ttl]
        for sid in expired_ids:
            sessions.pop(sid, None)

    def random_token(length: int = 12) -> str:
        alphabet = string.ascii_letters + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def issue_token(force_new: bool = False) -> tuple[str, float]:
        with lock:
            now = time.time()
            should_reuse = (
                not force_new
                and token_state["token"]
                and not token_state["consumed"]
                and token_state["expires_at"] > now
            )
            if should_reuse:
                return token_state["token"], token_state["expires_at"]

            token_state["token"] = random_token()
            token_state["expires_at"] = now + token_ttl_seconds
            token_state["consumed"] = False
            return token_state["token"], token_state["expires_at"]

    def mobile_url_from_token(token: str) -> str:
        return f"{app.config['BASE_URL']}/?token={token}"

    def get_mobile_qr_payload(force_new: bool = False) -> dict:
        token, expires_at = issue_token(force_new=force_new)
        url = mobile_url_from_token(token)
        return {
            "mobile_url": url,
            "mobile_qr_data_url": build_qr_data_url(url),
            "token_expires_at": int(expires_at),
        }

    def history_connection() -> sqlite3.Connection:
        conn = sqlite3.connect(str(app.config["HISTORY_DB_PATH"]), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            # WAL 模式显著降低并发读写时的 "database is locked" 概率
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
        except sqlite3.Error:
            pass
        return conn

    def ensure_history_schema() -> None:
        with history_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transfer_history (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'mobile',
                    desktop_side TEXT NOT NULL DEFAULT 'unknown'
                )
                """
            )
            cursor = conn.execute("PRAGMA table_info(transfer_history)")
            columns = {str(row["name"]) for row in cursor.fetchall()}
            if "desktop_side" not in columns:
                conn.execute(
                    "ALTER TABLE transfer_history ADD COLUMN desktop_side TEXT NOT NULL DEFAULT 'unknown'"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfer_history_device_ts ON transfer_history(device_id, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfer_history_ts ON transfer_history(timestamp)"
            )

    def normalize_device_id(raw: Optional[str]) -> str:
        return normalize_device_identifier(raw)

    def normalize_desktop_side(raw: Optional[str]) -> str:
        side = str(raw or "").strip().lower()
        if side in ("incoming", "outgoing"):
            return side
        return "unknown"

    def resolve_request_device(allow_query: bool = False) -> tuple[str, str, bool]:
        ip = request.remote_addr
        if is_trusted_desktop(ip):
            return DESKTOP_DEVICE_ID, "电脑端", True

        raw_device_id = request.headers.get("X-Device-Id")
        if allow_query and not raw_device_id:
            raw_device_id = request.args.get("device_id")
        device_id = normalize_device_id(raw_device_id)
        if not device_id:
            raise ValueError("缺少设备标识")

        raw_name = str(request.headers.get("X-Device-Name") or "").strip()
        device_name = raw_name[:80] if raw_name else f"手机-{device_id[:8]}"
        with lock:
            mobile_device_names[device_id] = device_name
            latest_mobile_device_id["id"] = device_id
        return device_id, device_name, False

    def preferred_mobile_device_for_desktop() -> tuple[str, str]:
        with lock:
            device_id = latest_mobile_device_id["id"]
            if device_id:
                return device_id, mobile_device_names.get(device_id, f"手机-{device_id[:8]}")
        return DESKTOP_DEVICE_ID, "电脑端"

    def normalize_peer_name(raw: Optional[str], fallback: str) -> str:
        return normalize_device_name(raw, fallback=fallback)

    def encode_header_text(value: Optional[str], fallback: str) -> str:
        normalized = normalize_device_name(value, fallback=fallback)
        try:
            normalized.encode("latin-1")
            return normalized
        except UnicodeEncodeError:
            return urllib.parse.quote(normalized, safe="")

    def decode_header_text(value: Optional[str]) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        return urllib.parse.unquote(raw)

    def parse_peer_port(raw) -> Optional[int]:
        try:
            port = int(raw)
        except (TypeError, ValueError):
            return None
        if 1 <= port <= 65535:
            return port
        return None

    def serialize_paired_desktops_locked() -> list[dict]:
        rows = []
        for device_id, peer in paired_desktops.items():
            host = str(peer.get("host") or "").strip()
            port = parse_peer_port(peer.get("port"))
            if not host or port is None:
                continue
            rows.append(
                {
                    "device_id": device_id,
                    "device_name": normalize_peer_name(peer.get("device_name"), fallback=f"电脑-{device_id[:8]}"),
                    "host": host,
                    "port": port,
                    "paired_at": int(peer.get("paired_at") or int(time.time())),
                }
            )
        rows.sort(key=lambda item: item["device_name"])
        return rows

    def persist_paired_desktops() -> None:
        with lock:
            payload = serialize_paired_desktops_locked()
        persist_runtime_setting("paired_desktops", payload)

    def refresh_discovered_from_peer_locked(
        device_id: str, device_name: str, host: str, port: int, seen_at: Optional[float] = None
    ) -> None:
        now = float(seen_at if seen_at is not None else time.time())
        discovered_desktops[device_id] = {
            "device_id": device_id,
            "device_name": device_name,
            "host": host,
            "port": int(port),
            "last_seen_at": int(now),
        }
        paired = paired_desktops.get(device_id)
        if paired is not None:
            paired["device_name"] = device_name
            paired["host"] = host
            paired["port"] = int(port)
            paired["last_seen_at"] = int(now)

    def cleanup_discovered_desktops_locked(now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        expired = [
            peer_id
            for peer_id, peer in discovered_desktops.items()
            if ts - float(peer.get("last_seen_at", 0)) > peer_stale_seconds
        ]
        for peer_id in expired:
            discovered_desktops.pop(peer_id, None)

    def cleanup_pair_requests_locked(now: Optional[float] = None) -> None:
        ts = float(now if now is not None else time.time())
        expired_inbound = [
            rid
            for rid, req in pending_pair_requests.items()
            if ts - float(req.get("created_at", 0)) > pair_request_ttl_seconds
        ]
        for rid in expired_inbound:
            pending_pair_requests.pop(rid, None)

        expired_outbound = [
            rid
            for rid, req in outgoing_pair_requests.items()
            if ts - float(req.get("created_at", 0)) > pair_request_ttl_seconds
        ]
        for rid in expired_outbound:
            outgoing_pair_requests.pop(rid, None)

    def list_discovered_desktops() -> list[dict]:
        with lock:
            cleanup_discovered_desktops_locked()
            rows = []
            for device_id, peer in discovered_desktops.items():
                rows.append(
                    {
                        "device_id": device_id,
                        "device_name": peer["device_name"],
                        "host": peer["host"],
                        "port": int(peer["port"]),
                        "last_seen_at": int(peer["last_seen_at"]),
                        "paired": device_id in paired_desktops,
                    }
                )
        rows.sort(key=lambda item: item["device_name"])
        return rows

    def list_paired_desktops() -> list[dict]:
        with lock:
            cleanup_discovered_desktops_locked()
            rows = []
            for device_id, peer in paired_desktops.items():
                discovered = discovered_desktops.get(device_id)
                host = str(peer.get("host") or "").strip()
                port = parse_peer_port(peer.get("port"))
                if discovered is not None:
                    discovered_host = str(discovered.get("host") or "").strip()
                    discovered_port = parse_peer_port(discovered.get("port"))
                    if discovered_host:
                        host = discovered_host
                    if discovered_port is not None:
                        port = discovered_port
                if not host or port is None:
                    continue
                rows.append(
                    {
                        "device_id": device_id,
                        "device_name": normalize_peer_name(peer.get("device_name"), fallback=f"电脑-{device_id[:8]}"),
                        "host": host,
                        "port": port,
                        "paired_at": int(peer.get("paired_at", int(time.time()))),
                        "online": discovered is not None,
                        "last_seen_at": int(discovered["last_seen_at"]) if discovered is not None else 0,
                    }
                )
        rows.sort(key=lambda item: item["device_name"])
        return rows

    def list_pending_pair_requests() -> list[dict]:
        with lock:
            cleanup_pair_requests_locked()
            rows = []
            for request_id, req in pending_pair_requests.items():
                rows.append(
                    {
                        "request_id": request_id,
                        "from_device_id": req["from_device_id"],
                        "from_device_name": req["from_device_name"],
                        "from_host": req["from_host"],
                        "from_port": int(req["from_port"]),
                        "created_at": int(req["created_at"]),
                    }
                )
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return rows

    def post_json(url: str, payload: dict, timeout: float = 4.0) -> tuple[int, dict]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            text = body.decode("utf-8", errors="ignore") if body else ""
            data = json.loads(text) if text else {}
            return int(getattr(resp, "status", 200)), data

    def notify_desktop_clients(event: dict) -> None:
        broadcast(event, target_device_id=DESKTOP_DEVICE_ID)

    def send_pairing_response_callback(
        target_base_url: str,
        request_id: str,
        accepted: bool,
        reason: str,
    ) -> tuple[bool, str]:
        callback_url = f"{target_base_url.rstrip('/')}/pairing/response"
        payload = {
            "request_id": request_id,
            "accepted": bool(accepted),
            "reason": reason,
            "responder_device_id": self_device_id,
            "responder_device_name": self_device_name,
            "responder_port": int(app.config["HTTP_PORT"]),
        }
        try:
            status, data = post_json(callback_url, payload, timeout=4.0)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return False, str(exc)
        if status >= 400:
            return False, str(data.get("error") or f"HTTP {status}")
        return True, ""

    def load_paired_desktops() -> None:
        settings = load_runtime_settings()
        payload = settings.get("paired_desktops")
        if not isinstance(payload, list):
            return
        now = int(time.time())
        with lock:
            for item in payload:
                if not isinstance(item, dict):
                    continue
                device_id = normalize_device_id(item.get("device_id"))
                if not device_id or device_id == self_device_id:
                    continue
                host = str(item.get("host") or "").strip()
                if not host:
                    continue
                try:
                    port = int(item.get("port"))
                except (TypeError, ValueError):
                    continue
                if port <= 0 or port > 65535:
                    continue
                paired_desktops[device_id] = {
                    "device_name": normalize_peer_name(item.get("device_name"), fallback=f"电脑-{device_id[:8]}"),
                    "host": host,
                    "port": port,
                    "paired_at": int(item.get("paired_at") or now),
                    "last_seen_at": 0,
                }

    def get_requested_desktop_target_id() -> str:
        return normalize_device_id(request.headers.get("X-Target-Device-Id"))

    def get_paired_peer_snapshot(device_id: str) -> Optional[dict]:
        target_id = normalize_device_id(device_id)
        if not target_id:
            return None
        with lock:
            peer = paired_desktops.get(target_id)
            if peer is None:
                return None
            discovered = discovered_desktops.get(target_id)
            host = ""
            port = None
            if discovered is not None:
                host = str(discovered.get("host") or "").strip()
                port = parse_peer_port(discovered.get("port"))
            if not host:
                host = str(peer.get("host") or "").strip()
            if port is None:
                port = parse_peer_port(peer.get("port"))
            if not host or port is None:
                return None
            return {
                "device_id": target_id,
                "device_name": normalize_peer_name(peer.get("device_name"), fallback=f"电脑-{target_id[:8]}"),
                "host": host,
                "port": port,
            }

    def build_relay_read_timeout_seconds(file_size_hint: int = 0) -> int:
        safe_size = max(0, int(file_size_hint or 0))
        # 按最低约 256KB/s 估算，给慢速网络和大文件更充足超时窗口
        dynamic = 120 + int(safe_size / (256 * 1024))
        return max(120, min(1800, dynamic))

    def check_peer_health(
        host: str,
        port: int,
        attempts: int = 2,
        timeout: tuple[float, float] = (1.5, 3.0),
    ) -> bool:
        url = f"http://{host}:{int(port)}/health"
        for attempt in range(attempts):
            try:
                resp = requests.get(url, timeout=timeout)
            except requests.RequestException as exc:
                _logger.debug("health check attempt %d failed for %s:%d: %s", attempt + 1, host, port, exc)
                if attempt < attempts - 1:
                    time.sleep(random.uniform(0.2, 0.8))
                continue
            if resp.status_code == 200:
                return True
        return False

    def find_reachable_paired_peer(
        device_id: str,
        exclude_endpoint: Optional[tuple[str, int]] = None,
    ) -> Optional[dict]:
        target_id = normalize_device_id(device_id)
        if not target_id:
            return None
        with lock:
            peer = paired_desktops.get(target_id)
            if peer is None:
                return None
            discovered = discovered_desktops.get(target_id)
            host_candidates = []
            for host in (
                str(discovered["host"]) if discovered is not None else "",
                str(peer.get("host") or ""),
            ):
                if host and host not in host_candidates:
                    host_candidates.append(host)
            seed_ports = []
            discovered_port = parse_peer_port(discovered.get("port")) if discovered is not None else None
            peer_port = parse_peer_port(peer.get("port"))
            for value in (discovered_port, peer_port):
                if value is not None and value not in seed_ports:
                    seed_ports.append(value)
            device_name = str(peer.get("device_name") or f"电脑-{target_id[:8]}")

        for host in host_candidates:
            candidate_ports = []
            for seed in seed_ports:
                if seed not in candidate_ports:
                    candidate_ports.append(seed)
                for offset in (
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                    9,
                    10,
                    11,
                    12,
                    13,
                    14,
                    15,
                    16,
                    17,
                    18,
                    19,
                    20,
                    21,
                    22,
                    23,
                    24,
                    25,
                    26,
                    27,
                    28,
                    29,
                    30,
                    -1,
                    -2,
                    -3,
                    -4,
                    -5,
                    -6,
                    -7,
                    -8,
                    -9,
                    -10,
                ):
                    maybe = seed + offset
                    if 1 <= maybe <= 65535 and maybe not in candidate_ports:
                        candidate_ports.append(maybe)
            for fallback_port in range(5000, 5051):
                if fallback_port not in candidate_ports:
                    candidate_ports.append(fallback_port)

            # Parallel port probing for faster reachability detection
            def _probe_port(port: int) -> Optional[tuple[int, str, int]]:
                endpoint = (host, int(port))
                if exclude_endpoint is not None and endpoint == exclude_endpoint:
                    return None
                if check_peer_health(host, port):
                    return (port, host, int(port))
                return None

            dedup_ports: list[int] = []
            seen_p: set[int] = set()
            for p in candidate_ports:
                if p not in seen_p:
                    seen_p.add(p)
                    dedup_ports.append(p)

            # Probe in parallel batches of 10, return first success
            batch_size = 10
            found: Optional[tuple[int, str, int]] = None
            for batch_start in range(0, len(dedup_ports), batch_size):
                batch = dedup_ports[batch_start:batch_start + batch_size]
                with ThreadPoolExecutor(max_workers=min(len(batch), 8)) as executor:
                    futures = {executor.submit(_probe_port, p): p for p in batch}
                    for future in as_completed(futures):
                        result = future.result()
                        if result is not None:
                            found = result
                            break
                if found is not None:
                    break

            if found is not None:
                _port, _host, port = found
                with lock:
                    refresh_discovered_from_peer_locked(target_id, device_name, host, int(port), seen_at=time.time())
                persist_paired_desktops()
                return {
                    "device_id": target_id,
                    "device_name": device_name,
                    "host": host,
                    "port": int(port),
                }
        return None

    def resolve_desktop_transfer_target(target_device_id: str) -> tuple[str, str, Optional[dict], Optional[str]]:
        normalized_target = normalize_device_id(target_device_id)
        if not normalized_target:
            mobile_id, mobile_name = preferred_mobile_device_for_desktop()
            return mobile_id, mobile_name, None, None
        target_peer = get_paired_peer_snapshot(normalized_target)
        if target_peer is None:
            return "", "", None, "目标电脑未配对或不可用"
        return target_peer["device_id"], target_peer["device_name"], target_peer, None

    def relay_file_to_paired_desktop(
        *,
        target_peer: dict,
        file_name: str,
        file_stream,
        file_size_hint: int = 0,
    ) -> tuple[bool, Optional[str], dict]:
        headers = {
            "X-Peer-Device-Id": self_device_id,
            "X-Peer-Device-Name": encode_header_text(
                self_device_name, fallback=f"desktop-{self_device_id[:8]}"
            ),
            "X-Peer-Port": str(int(app.config["HTTP_PORT"])),
        }
        read_timeout = build_relay_read_timeout_seconds(file_size_hint)
        endpoint_candidates = [target_peer]

        last_error = ""
        last_payload: dict = {}
        idx = 0
        while idx < len(endpoint_candidates):
            peer_endpoint = endpoint_candidates[idx]
            if idx > 0 and hasattr(file_stream, "seek"):
                try:
                    file_stream.seek(0)
                except Exception as exc:
                    _logger.warning("relay retry seek(0) failed for %s: %s", file_name, exc)
                    return False, f"重试发送前无法复位文件流: {exc}", last_payload
                if hasattr(file_stream, "tell"):
                    try:
                        if file_stream.tell() != 0:
                            _logger.warning("relay retry seek verification failed for %s, stream position != 0", file_name)
                            return False, "重试发送前文件流复位校验失败", last_payload
                    except Exception as exc:
                        _logger.warning("relay retry tell() failed for %s: %s", file_name, exc)
                        return False, f"重试发送前无法读取文件流位置: {exc}", last_payload
                if file_size_hint > 0 and hasattr(file_stream, "seek"):
                    try:
                        file_stream.seek(0, os.SEEK_END)
                        end_pos = file_stream.tell()
                        file_stream.seek(0, os.SEEK_SET) if hasattr(file_stream, "tell") else file_stream.seek(0)
                        if end_pos != file_size_hint:
                            _logger.warning("relay retry file size mismatch for %s: expected %d, got %d", file_name, file_size_hint, end_pos)
                            return False, f"重试前文件大小不一致: 预期 {file_size_hint}，实际 {end_pos}", last_payload
                    except Exception as exc:
                        _logger.warning("relay retry size verification failed for %s: %s", file_name, exc)
                        return False, f"重试前无法校验文件大小: {exc}", last_payload
            peer_host = str(peer_endpoint.get("host") or "").strip()
            peer_port = parse_peer_port(peer_endpoint.get("port"))
            if not peer_host or peer_port is None:
                last_error = "目标设备地址无效，请删除配对后重新配对"
                idx += 1
                continue
            relay_url = f"http://{peer_host}:{peer_port}/peer/upload"
            try:
                response = requests.post(
                    relay_url,
                    headers=headers,
                    data={"source_device_name": self_device_name},
                    files={"file": (file_name, file_stream, "application/octet-stream")},
                    timeout=(5, read_timeout),
                )
            except requests.RequestException as exc:
                last_error = f"目标设备不可达: {exc}"
                if idx == 0:
                    exclude_port = parse_peer_port(target_peer.get("port")) or 0
                    alt_peer = find_reachable_paired_peer(
                        str(target_peer.get("device_id") or ""),
                        exclude_endpoint=(str(target_peer.get("host") or ""), exclude_port),
                    )
                    if alt_peer is not None:
                        endpoint_candidates.append(alt_peer)
                idx += 1
                continue

            try:
                payload = response.json()
            except ValueError:
                payload = {}
            last_payload = payload
            if response.status_code < 400:
                with lock:
                    refresh_discovered_from_peer_locked(
                        str(peer_endpoint["device_id"]),
                        str(peer_endpoint["device_name"]),
                        peer_host,
                        peer_port,
                        seen_at=time.time(),
                    )
                persist_paired_desktops()
                return True, None, payload
            last_error = str(payload.get("error") or f"目标设备返回错误: HTTP {response.status_code}")
            if idx == 0 and response.status_code in (404, 500, 502, 503, 504):
                exclude_port = parse_peer_port(target_peer.get("port")) or 0
                alt_peer = find_reachable_paired_peer(
                    str(target_peer.get("device_id") or ""),
                    exclude_endpoint=(str(target_peer.get("host") or ""), exclude_port),
                )
                if alt_peer is not None:
                    endpoint_candidates.append(alt_peer)
            idx += 1

        return False, (last_error or "发送到目标设备失败"), last_payload

    def record_desktop_send_history(
        *,
        file_name: str,
        file_path: str,
        file_size: int,
        device_id: str,
        device_name: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        history_id = uuid.uuid4().hex
        created_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            insert_history_record(
                history_id=history_id,
                device_id=device_id,
                device_name=device_name,
                file_name=file_name,
                file_path=file_path,
                direction="上传",
                status="成功",
                file_size=max(0, int(file_size or 0)),
                source="desktop",
                desktop_side="outgoing",
                timestamp_text=created_at_text,
            )
        except Exception as exc:
            return None, f"写入历史记录失败: {exc}"

        send_history_event(history_id, target_device_id=DESKTOP_DEVICE_ID)
        row = history_row_by_id(history_id)
        if row is None:
            return None, "历史记录不存在"
        return public_history_record(row, include_file_path=True), None

    def insert_history_record(
        *,
        history_id: str,
        device_id: str,
        device_name: str,
        file_name: str,
        file_path: str,
        direction: str,
        status: str,
        file_size: int,
        source: str,
        desktop_side: str,
        timestamp_text: Optional[str] = None,
    ) -> None:
        ts = timestamp_text or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with history_connection() as conn:
            conn.execute(
                """
                INSERT INTO transfer_history
                (id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source, desktop_side)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history_id,
                    device_id,
                    device_name,
                    file_name,
                    file_path,
                    direction,
                    ts,
                    status,
                    max(0, int(file_size or 0)),
                    source if source in ("desktop", "mobile") else "mobile",
                    normalize_desktop_side(desktop_side),
                ),
            )

    def update_history_status(history_id: str, status: str) -> None:
        with history_connection() as conn:
            conn.execute("UPDATE transfer_history SET status = ? WHERE id = ?", (status, history_id))

    def update_history_record(
        history_id: str,
        *,
        status: Optional[str] = None,
        file_name: Optional[str] = None,
        file_path: Optional[str] = None,
    ) -> None:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if file_name is not None:
            updates.append("file_name = ?")
            params.append(file_name)
        if file_path is not None:
            updates.append("file_path = ?")
            params.append(file_path)
        if not updates:
            return
        params.append(history_id)
        with history_connection() as conn:
            conn.execute(
                f"UPDATE transfer_history SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )

    def history_rows(include_all: bool, device_id: Optional[str]) -> list[sqlite3.Row]:
        with history_connection() as conn:
            if include_all:
                cursor = conn.execute(
                    """
                    SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source, desktop_side
                    FROM transfer_history
                    ORDER BY timestamp ASC, id ASC
                    """
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source, desktop_side
                    FROM transfer_history
                    WHERE device_id = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (device_id or "",),
                )
            return cursor.fetchall()

    def history_row_by_id(history_id: str) -> Optional[sqlite3.Row]:
        with history_connection() as conn:
            cursor = conn.execute(
                """
                SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source, desktop_side
                FROM transfer_history
                WHERE id = ?
                LIMIT 1
                """,
                (history_id,),
            )
            return cursor.fetchone()

    def public_history_record(row: sqlite3.Row, include_file_path: bool = False) -> dict:
        history_id = str(row["id"])
        with lock:
            active = record_map.get(history_id)
        return {
            "id": history_id,
            "device_id": str(row["device_id"]),
            "device_name": str(row["device_name"]),
            "name": str(row["file_name"]),
            "file_path": str(row["file_path"]) if include_file_path else "",
            "direction": str(row["direction"]),
            "status": str(row["status"]),
            "size": int(row["file_size"] or 0),
            "source": str(row["source"] or "mobile"),
            "desktop_side": normalize_desktop_side(row["desktop_side"]),
            "created_at": str(row["timestamp"]),
            "download_url": f"/files/{history_id}" if active is not None else "",
        }

    def send_history_event(history_id: str, target_device_id: str) -> None:
        row = history_row_by_id(history_id)
        if row is None:
            return
        broadcast({"type": "new_record", "record": public_history_record(row)}, target_device_id=target_device_id)

    def send_history_update_event(history_id: str, target_device_id: str) -> None:
        row = history_row_by_id(history_id)
        if row is None:
            return
        broadcast({"type": "record_updated", "record": public_history_record(row)}, target_device_id=target_device_id)

    def remove_record_and_file(transfer_id: str) -> None:
        removed = None
        with lock:
            removed = record_map.pop(transfer_id, None)
            if removed is None:
                return
            records[:] = [r for r in records if r["id"] != transfer_id]

        try:
            removed_path = removed.get("path")
            if isinstance(removed_path, Path) and removed_path.exists():
                removed_path.unlink(missing_ok=True)
        except Exception:
            pass

    def remove_record_cache_only(transfer_id: str) -> None:
        with lock:
            if transfer_id in record_map:
                record_map.pop(transfer_id, None)
            records[:] = [r for r in records if r["id"] != transfer_id]

    def normalize_history_ids(raw_ids: object) -> list[str]:
        if not isinstance(raw_ids, list):
            return []
        normalized: list[str] = []
        seen: set[str] = set()
        for item in raw_ids:
            value = str(item or "").strip()
            if not value or len(value) > 80:
                continue
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def persist_runtime_setting(key: str, value) -> None:
        try:
            with _settings_lock:
                settings = _read_settings_unlocked()
                settings[key] = value
                _write_settings_unlocked(settings)
        except Exception:
            _logger.warning("Failed to persist runtime setting '%s': skipping", key)

    def stream_to_disk(
        file_stream,
        destination: Path,
        chunk_size: int = 1024 * 1024,
        max_bytes: Optional[int] = None,
    ) -> int:
        total = 0
        with destination.open("wb") as f:
            while True:
                chunk = file_stream.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError("上传文件超过大小限制")
        return total

    def cleanup_stale_transient_files(max_age_hours: float = 24.0) -> int:
        transient_dir = app.config["TRANSIENT_UPLOAD_DIR"]
        if not transient_dir.exists():
            return 0
        cutoff = time.time() - max_age_hours * 3600
        removed = 0
        for entry in transient_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass
        _logger.info("transient cleanup removed %d stale files from %s", removed, transient_dir)
        return removed

    def cleanup_transient_record_file(
        transfer_id: str,
        source_path: Path,
        target_path: Path,
        keep_source_when_same: bool = False,
    ) -> None:
        remove_record_cache_only(transfer_id)
        if keep_source_when_same and source_path.resolve() == target_path.resolve():
            return
        try:
            if source_path.exists():
                source_path.unlink(missing_ok=True)
        except Exception as exc:
            app.logger.warning(
                "transient cleanup failed transfer_id=%s source=%s target=%s error=%s",
                transfer_id,
                source_path,
                target_path,
                exc,
            )

    MAX_CACHED_RECORDS = 1500

    def cache_record(record: dict) -> None:
        """登记记录到内存缓存；超过上限时淘汰最旧条目（历史记录以数据库为准）。"""
        with lock:
            records.append(record)
            record_map[record["id"]] = record
            while len(records) > MAX_CACHED_RECORDS:
                oldest = records.pop(0)
                record_map.pop(oldest["id"], None)

    def cached_or_db_record(transfer_id: str) -> Optional[dict]:
        """优先取内存缓存；缓存淘汰后从历史数据库回退构造记录（供下载/保存使用）。"""
        with lock:
            record = record_map.get(transfer_id)
        if record is not None:
            return record
        row = history_row_by_id(transfer_id)
        if row is None:
            return None
        path = Path(str(row["file_path"] or "")).expanduser()
        if not path.is_absolute() or not path.exists():
            return None
        transient_dir = Path(app.config["TRANSIENT_UPLOAD_DIR"]).resolve()
        try:
            is_transient = path.resolve().parent == transient_dir
        except OSError:
            is_transient = False
        return {
            "id": transfer_id,
            "name": str(row["file_name"]),
            "size": int(row["file_size"] or 0),
            "source": str(row["source"] or "mobile"),
            "created_at": str(row["timestamp"]),
            "path": path,
            "transient": is_transient,
            "device_id": str(row["device_id"]),
            "device_name": str(row["device_name"]),
            "direction": str(row["direction"]),
            "status": str(row["status"]),
        }

    def broadcast(event: dict, target_device_id: Optional[str] = None) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        dead = []
        with lock:
            targets = list(clients.items())
        for ws, meta in targets:
            if not meta.get("is_desktop"):
                if not target_device_id or meta.get("device_id") != target_device_id:
                    continue
            try:
                ws.send(payload)
            except Exception:
                dead.append(ws)
        if dead:
            with lock:
                for ws in dead:
                    clients.pop(ws, None)

    def run_peer_discovery() -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        announce_targets = infer_directed_broadcast_targets([lan_ip] + list(lan_ip_candidates or []))
        tcp_probe_interval = 8.0
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            listener.bind(("0.0.0.0", peer_discovery_port))
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            # Immediate announcement on startup for faster initial discovery
            announce_payload = {
                "type": "lft_announce",
                "device_id": self_device_id,
                "device_name": self_device_name,
                "http_port": int(app.config["HTTP_PORT"]),
                "ts": int(time.time()),
            }
            packet = json.dumps(announce_payload, ensure_ascii=False).encode("utf-8")
            for target_host in announce_targets:
                try:
                    sender.sendto(packet, (target_host, peer_discovery_port))
                except OSError:
                    pass

            # Active TCP neighbor scan on startup: probe common LAN neighbors
            def _startup_neighbor_scan() -> None:
                """Scan neighboring IPs on the local subnet to discover peers faster."""
                time.sleep(0.5)
                http_port = int(app.config["HTTP_PORT"])
                scan_ports = [http_port] if 5000 <= http_port <= 5050 else [http_port, 5000]
                # Determine scan range from LAN IP
                scan_ip = lan_ip
                try:
                    ip_obj = ipaddress.ip_address(scan_ip)
                except ValueError:
                    return
                # Build neighbor IPs to scan (narrow range: this device +/- 15)
                ip_int = int(ip_obj)
                base = ip_int & 0xFFFFFF00
                local_octet = ip_int & 0xFF
                start_octet = max(1, local_octet - 15)
                end_octet = min(254, local_octet + 15)
                neighbors = []
                for octet in range(start_octet, end_octet + 1):
                    if octet == local_octet:
                        continue
                    neighbors.append(ipaddress.ip_address(base | octet))
                for neighbor_ip in neighbors:
                    neighbor_str = str(neighbor_ip)
                    if not is_usable_ipv4(neighbor_str):
                        continue
                    for scan_port in scan_ports:
                        # 快速探测：单次尝试 + 短超时，避免黑名单/丢包网络下拖慢启动
                        if check_peer_health(neighbor_str, scan_port, attempts=1, timeout=(0.6, 1.0)):
                            _logger.info("Startup neighbor scan found peer at %s:%d", neighbor_str, scan_port)
                            break
                    time.sleep(0.03)  # Small delay to avoid flooding

            def _tcp_probe_loop() -> None:
                """周期 TCP 探活已配对设备；独立线程，避免阻塞 UDP 广播循环。"""
                while True:
                    time.sleep(tcp_probe_interval)
                    now = time.time()
                    with lock:
                        tcp_probe_targets = [
                            (peer_id, str(peer.get("host", "").strip()), int(peer.get("port", 0)))
                            for peer_id, peer in paired_desktops.items()
                            if str(peer.get("host", "").strip()) and int(peer.get("port", 0)) > 0
                        ]
                    for peer_id, peer_host, peer_port in tcp_probe_targets:
                        if check_peer_health(peer_host, peer_port):
                            with lock:
                                peer = paired_desktops.get(peer_id)
                                if peer is not None:
                                    refresh_discovered_from_peer_locked(
                                        peer_id,
                                        str(peer.get("device_name", f"电脑-{peer_id[:8]}")),
                                        peer_host,
                                        peer_port,
                                        seen_at=now,
                                    )
                            _logger.debug("TCP probe succeeded for paired device %s at %s:%d", peer_id, peer_host, peer_port)

            threading.Thread(target=_startup_neighbor_scan, daemon=True, name="lft-startup-scan").start()
            threading.Thread(target=_tcp_probe_loop, daemon=True, name="lft-tcp-probe").start()

            next_announce_at = 0.0
            while True:
                now = time.time()
                if now >= next_announce_at:
                    announce_payload = {
                        "type": "lft_announce",
                        "device_id": self_device_id,
                        "device_name": self_device_name,
                        "http_port": int(app.config["HTTP_PORT"]),
                        "ts": int(now),
                    }
                    packet = json.dumps(announce_payload, ensure_ascii=False).encode("utf-8")
                    for target_host in announce_targets:
                        try:
                            sender.sendto(packet, (target_host, peer_discovery_port))
                        except OSError as exc:
                            app.logger.debug(
                                "peer discovery broadcast failed target=%s port=%s error=%s",
                                target_host,
                                peer_discovery_port,
                                exc,
                            )
                    next_announce_at = now + peer_announce_interval

                wait_seconds = max(0.2, min(1.0, next_announce_at - now))
                listener.settimeout(wait_seconds)
                try:
                    packet, addr = listener.recvfrom(4096)
                except socket.timeout:
                    with lock:
                        cleanup_discovered_desktops_locked()
                        cleanup_pair_requests_locked()
                    continue
                except OSError:
                    break

                host = str(addr[0] or "").strip()
                if not host:
                    continue
                try:
                    message = json.loads(packet.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if message.get("type") != "lft_announce":
                    continue
                peer_device_id = normalize_device_id(message.get("device_id"))
                if not peer_device_id or peer_device_id == self_device_id:
                    continue
                try:
                    peer_port = int(message.get("http_port"))
                except (TypeError, ValueError):
                    continue
                if peer_port <= 0 or peer_port > 65535:
                    continue
                peer_name = normalize_peer_name(message.get("device_name"), fallback=f"电脑-{peer_device_id[:8]}")
                with lock:
                    refresh_discovered_from_peer_locked(
                        peer_device_id, peer_name, host, peer_port, seen_at=time.time()
                    )
                    cleanup_discovered_desktops_locked()
                    cleanup_pair_requests_locked()
        finally:
            listener.close()
            sender.close()

    def start_peer_discovery() -> None:
        threading.Thread(target=run_peer_discovery, daemon=True, name="lft-peer-discovery").start()

    def start_mdns_discovery() -> None:
        """Start mDNS/Zeroconf service advertisement and browsing as secondary discovery channel."""
        _mdns_running = {"zc": None, "info": None}

        def run_mdns() -> None:
            try:
                zc = Zeroconf()
                _mdns_running["zc"] = zc
                service_type = "_lft._tcp.local."
                service_name = f"{self_device_name} ({self_device_id[:8]})._lft._tcp.local."

                # Register our service
                info = ServiceInfo(
                    service_type,
                    service_name,
                    addresses=[socket.inet_aton(lan_ip)],
                    port=int(app.config["HTTP_PORT"]),
                    properties={
                        b"device_id": self_device_id.encode("utf-8"),
                        b"device_name": self_device_name.encode("utf-8"),
                        b"http_port": str(app.config["HTTP_PORT"]).encode("utf-8"),
                    },
                )
                _mdns_running["info"] = info
                zc.register_service(info)

                class _LFTBrowserListener:
                    def add_service(self, zc_obj, type_name, name):
                        try:
                            svc_info = zc_obj.get_service_info(type_name, name, timeout=2000)
                            if svc_info is None:
                                return
                            props = svc_info.properties
                            peer_id_bytes = props.get(b"device_id")
                            if not peer_id_bytes:
                                return
                            peer_id = normalize_device_id(peer_id_bytes.decode("utf-8"))
                            if not peer_id or peer_id == self_device_id:
                                return
                            peer_name_bytes = props.get(b"device_name")
                            peer_name = peer_name_bytes.decode("utf-8") if peer_name_bytes else f"电脑-{peer_id[:8]}"
                            port_bytes = props.get(b"http_port")
                            try:
                                peer_port = int(port_bytes.decode("utf-8")) if port_bytes else 0
                            except (TypeError, ValueError, AttributeError):
                                peer_port = 0
                            if peer_port <= 0 or peer_port > 65535:
                                return
                            host = socket.inet_ntoa(svc_info.addresses[0]) if svc_info.addresses else ""
                            if not host:
                                return
                            with lock:
                                refresh_discovered_from_peer_locked(
                                    peer_id,
                                    normalize_peer_name(peer_name, fallback=f"电脑-{peer_id[:8]}"),
                                    host,
                                    peer_port,
                                    seen_at=time.time(),
                                )
                                cleanup_discovered_desktops_locked()
                        except Exception as exc:
                            _logger.debug("mDNS add_service error: %s", exc)

                    def remove_service(self, zc_obj, type_name, name):
                        pass

                    def update_service(self, zc_obj, type_name, name):
                        self.add_service(zc_obj, type_name, name)

                browser = ServiceBrowser(zc, service_type, listener=_LFTBrowserListener())

                while True:
                    time.sleep(5)
            except Exception as exc:
                _logger.info("mDNS discovery unavailable: %s", exc)
            finally:
                if _mdns_running["zc"] is not None:
                    try:
                        if _mdns_running["info"] is not None:
                            _mdns_running["zc"].unregister_service(_mdns_running["info"])
                        _mdns_running["zc"].close()
                    except Exception:
                        pass

        threading.Thread(target=run_mdns, daemon=True, name="lft-mdns-discovery").start()

    ensure_history_schema()
    load_paired_desktops()
    cleanup_stale_transient_files()
    start_peer_discovery()
    start_mdns_discovery()

    def is_trusted_desktop(ip: Optional[str]) -> bool:
        return bool(ip and ip in trusted_desktop_ips)

    def read_session_id(allow_query: bool = False) -> Optional[str]:
        if allow_query:
            return (
                request.headers.get("X-Session-Id")
                or request.args.get("session_id")
                or request.cookies.get("lft_session")
            )
        return request.headers.get("X-Session-Id") or request.cookies.get("lft_session")

    def get_valid_session(session_id: Optional[str], ip: Optional[str]) -> Optional[dict]:
        if not session_id:
            return None

        with lock:
            now = int(time.time())
            cleanup_expired_sessions_locked(now)
            session = sessions.get(session_id)
            if session is None:
                return None
            if session["ip"] != ip:
                return None
            if now - session["last_seen_at"] > app.config["SESSION_TTL_SECONDS"]:
                sessions.pop(session_id, None)
                return None
            session["last_seen_at"] = now
            return session

    def consume_token_and_issue_session(token: str, ip: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        if not token:
            return None, "缺少一次性令牌"
        if not ip:
            return None, "无法识别设备地址"

        with lock:
            now = time.time()
            cleanup_expired_sessions_locked(int(now))
            if token_state["token"] != token:
                return None, "令牌无效"
            if token_state["consumed"]:
                return None, "令牌已失效"
            if token_state["expires_at"] <= now:
                return None, "令牌已过期"

            token_state["consumed"] = True
            session_id = uuid.uuid4().hex
            sessions[session_id] = {
                "id": session_id,
                "ip": ip,
                "created_at": int(now),
                "last_seen_at": int(now),
            }
            return session_id, None

    def authorize_request(allow_query_session: bool = False) -> bool:
        ip = request.remote_addr
        if is_trusted_desktop(ip):
            return True
        session_id = read_session_id(allow_query=allow_query_session)
        return get_valid_session(session_id, ip) is not None

    @app.get("/")
    def index():
        ip = request.remote_addr
        role = request.args.get("role")
        token = request.args.get("token", "")
        session_id = read_session_id()
        valid_session = get_valid_session(session_id, ip)
        consumed_token = False

        if token:
            if valid_session is not None:
                active_session_id = valid_session["id"]
            else:
                active_session_id, error = consume_token_and_issue_session(token, ip)
                if active_session_id is None:
                    return make_response(
                        render_template(
                            "index.html",
                            access_denied=True,
                            access_denied_reason=error,
                            role_hint="mobile",
                            session_id="",
                            mobile_url="",
                            mobile_qr_data_url="",
                            token_expires_at=0,
                        ),
                        403,
                    )
                consumed_token = True

            response = make_response(
                render_template(
                    "index.html",
                    access_denied=False,
                    access_denied_reason="",
                    role_hint="mobile",
                    session_id=active_session_id,
                    mobile_url="",
                    mobile_qr_data_url="",
                    token_expires_at=0,
                )
            )
            response.set_cookie("lft_session", active_session_id, httponly=True, samesite="Lax")
            if consumed_token:
                notify_desktop_clients(
                    {
                        "type": "mobile_connected",
                        "qr_payload": get_mobile_qr_payload(force_new=True),
                    }
                )
            return response

        if role == "mobile":
            if valid_session is None:
                return make_response(
                    render_template(
                        "index.html",
                        access_denied=True,
                        access_denied_reason="请重新扫码获取一次性登录令牌。",
                        role_hint="mobile",
                        session_id="",
                        mobile_url="",
                        mobile_qr_data_url="",
                        token_expires_at=0,
                    ),
                    403,
                )

            return make_response(
                render_template(
                    "index.html",
                    access_denied=False,
                    access_denied_reason="",
                    role_hint="mobile",
                    session_id=valid_session["id"],
                    mobile_url="",
                    mobile_qr_data_url="",
                    token_expires_at=0,
                )
            )

        if not is_trusted_desktop(ip):
            return make_response(
                render_template(
                    "index.html",
                    access_denied=True,
                    access_denied_reason="未授权访问：请使用电脑端二维码扫码登录。",
                    role_hint="mobile",
                    session_id="",
                    mobile_url="",
                    mobile_qr_data_url="",
                    token_expires_at=0,
                ),
                403,
            )

        qr_payload = get_mobile_qr_payload(force_new=False)
        return render_template(
            "index.html",
            access_denied=False,
            access_denied_reason="",
            role_hint="desktop",
            session_id="",
            mobile_url=qr_payload["mobile_url"],
            mobile_qr_data_url=qr_payload["mobile_qr_data_url"],
            token_expires_at=qr_payload["token_expires_at"],
        )

    @app.get("/records")
    def get_records():
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        include_all = is_trusted_desktop(request.remote_addr)
        filter_device_id = None
        include_file_path = include_all
        if not include_all:
            try:
                filter_device_id, _device_name, _ = resolve_request_device()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        rows = history_rows(include_all=include_all, device_id=filter_device_id)
        data = [public_history_record(row, include_file_path=include_file_path) for row in rows]
        return jsonify({"records": data})

    @app.post("/records/delete")
    def delete_records():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可删除历史记录"}), 403

        payload = request.get_json(silent=True) or {}
        history_ids = normalize_history_ids(payload.get("ids"))
        if not history_ids:
            return jsonify({"error": "请至少选择一条记录"}), 400
        if len(history_ids) > 500:
            return jsonify({"error": "单次最多删除 500 条记录"}), 400

        placeholders = ",".join("?" for _ in history_ids)
        with history_connection() as conn:
            cursor = conn.execute(
                f"SELECT id FROM transfer_history WHERE id IN ({placeholders})",
                tuple(history_ids),
            )
            existing_ids = [str(row["id"]) for row in cursor.fetchall()]
            if existing_ids:
                delete_placeholders = ",".join("?" for _ in existing_ids)
                conn.execute(
                    f"DELETE FROM transfer_history WHERE id IN ({delete_placeholders})",
                    tuple(existing_ids),
                )

        existing_set = set(existing_ids)
        not_found_ids = [item for item in history_ids if item not in existing_set]
        for history_id in existing_ids:
            remove_record_cache_only(history_id)
            broadcast({"type": "remove_record", "id": history_id})

        return jsonify(
            {
                "ok": True,
                "deleted_ids": existing_ids,
                "not_found_ids": not_found_ids,
            }
        )

    @app.get("/settings")
    def get_settings():
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401
        return jsonify(
            {
                "max_upload_bytes": app.config["MAX_UPLOAD_BYTES"],
                "session_ttl_seconds": app.config["SESSION_TTL_SECONDS"],
                "download_dir": str(app.config["DOWNLOAD_DIR"]),
                "default_download_dir": str(default_download_dir()),
            }
        )

    @app.get("/peers/discovered")
    def get_discovered_peers():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可查看设备列表"}), 403
        return jsonify(
            {
                "self": {
                    "device_id": self_device_id,
                    "device_name": self_device_name,
                    "host": lan_ip,
                    "port": int(app.config["HTTP_PORT"]),
                },
                "devices": list_discovered_desktops(),
            }
        )

    @app.get("/peers/paired")
    def get_paired_peers():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可查看配对设备"}), 403
        return jsonify({"devices": list_paired_desktops()})

    @app.delete("/peers/paired/<device_id>")
    def delete_paired_peer(device_id: str):
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可删除配对设备"}), 403
        normalized_device_id = normalize_device_id(device_id)
        if not normalized_device_id:
            return jsonify({"error": "设备标识无效"}), 400
        with lock:
            removed = paired_desktops.pop(normalized_device_id, None)
        if removed is None:
            return jsonify({"error": "配对设备不存在"}), 404
        persist_paired_desktops()
        notify_desktop_clients({"type": "pairing_list_updated"})
        return jsonify({"ok": True, "device_id": normalized_device_id})

    @app.post("/peers/pair-request")
    def send_pair_request():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可发起配对"}), 403

        payload = request.get_json(silent=True) or {}
        target_device_id = normalize_device_id(payload.get("target_device_id"))
        if not target_device_id:
            return jsonify({"error": "缺少目标设备标识"}), 400
        if target_device_id == self_device_id:
            return jsonify({"error": "不能向当前设备发起配对"}), 400

        with lock:
            cleanup_discovered_desktops_locked()
            target_peer = discovered_desktops.get(target_device_id)
            if target_peer is None:
                return jsonify({"error": "目标设备不在线，请稍后重试"}), 404
            target_host = target_peer["host"]
            target_port = int(target_peer["port"])
            target_name = target_peer["device_name"]

        request_id = uuid.uuid4().hex
        req_payload = {
            "request_id": request_id,
            "from_device_id": self_device_id,
            "from_device_name": self_device_name,
            "from_port": int(app.config["HTTP_PORT"]),
            "from_base_url": app.config["BASE_URL"],
            "sent_at": int(time.time()),
        }
        target_url = f"http://{target_host}:{target_port}/pairing/request"
        try:
            status, data = post_json(target_url, req_payload, timeout=4.0)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            message = ""
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8", errors="ignore"))
                    message = str(parsed.get("error") or "")
                except json.JSONDecodeError:
                    message = ""
            return jsonify({"error": message or f"请求失败: HTTP {exc.code}"}), 502
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return jsonify({"error": f"设备不可达: {exc}"}), 502
        if status >= 400:
            return jsonify({"error": str(data.get('error') or f'请求失败: HTTP {status}')}), 502

        with lock:
            outgoing_pair_requests[request_id] = {
                "request_id": request_id,
                "target_device_id": target_device_id,
                "target_device_name": target_name,
                "target_host": target_host,
                "target_port": target_port,
                "created_at": int(time.time()),
            }
            cleanup_pair_requests_locked()

        return jsonify({"ok": True, "request_id": request_id, "target_device_name": target_name})

    @app.get("/pairing/pending")
    def get_pending_pair_requests():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可查看配对请求"}), 403
        return jsonify({"requests": list_pending_pair_requests()})

    @app.post("/pairing/request")
    def receive_pairing_request():
        payload = request.get_json(silent=True) or {}
        request_id = normalize_device_identifier(payload.get("request_id"), max_len=64)
        from_device_id = normalize_device_id(payload.get("from_device_id"))
        if not request_id or not from_device_id:
            return jsonify({"error": "请求参数无效"}), 400
        if from_device_id == self_device_id:
            return jsonify({"error": "无效的请求来源"}), 400

        from_host = str(request.remote_addr or "").strip()
        if not from_host:
            return jsonify({"error": "无法识别设备地址"}), 400

        try:
            from_port = int(payload.get("from_port"))
        except (TypeError, ValueError):
            return jsonify({"error": "请求端口无效"}), 400
        if from_port <= 0 or from_port > 65535:
            return jsonify({"error": "请求端口无效"}), 400

        from_device_name = normalize_peer_name(payload.get("from_device_name"), fallback=f"电脑-{from_device_id[:8]}")
        from_base_url = str(payload.get("from_base_url") or "").strip()
        if not from_base_url:
            from_base_url = f"http://{from_host}:{from_port}"

        auto_accept = False
        request_snapshot = {}
        with lock:
            existing_pair = paired_desktops.get(from_device_id)
            # 校验必须先于 refresh_discovered_from_peer_locked：
            # 发现记录会被请求方来源 IP 覆盖，若先 refresh 再校验，校验恒成立，防劫持失效。
            known_hosts = set()
            if existing_pair is not None:
                known_hosts.add(str(existing_pair.get("host") or "").strip())
            discovered_before = discovered_desktops.get(from_device_id)
            if discovered_before is not None and str(discovered_before.get("host") or "").strip():
                known_hosts.add(str(discovered_before["host"]).strip())

            if existing_pair is not None and from_host in known_hosts:
                # 来源一致：正常续约，自动接受并刷新发现记录
                existing_pair["device_name"] = from_device_name
                existing_pair["host"] = from_host
                existing_pair["port"] = from_port
                existing_pair["last_seen_at"] = int(time.time())
                refresh_discovered_from_peer_locked(from_device_id, from_device_name, from_host, from_port, seen_at=time.time())
                auto_accept = True
            else:
                if existing_pair is not None:
                    _logger.warning(
                        "Pair request from %s for already-paired device %s: host mismatch (known %s), requiring manual approval",
                        from_host,
                        from_device_id,
                        sorted(known_hosts) if known_hosts else "(none)",
                    )
                else:
                    # 新设备配对：来源 IP 为真实 socket 地址，可刷新发现记录
                    refresh_discovered_from_peer_locked(from_device_id, from_device_name, from_host, from_port, seen_at=time.time())
                pending_pair_requests[request_id] = {
                    "request_id": request_id,
                    "from_device_id": from_device_id,
                    "from_device_name": from_device_name,
                    "from_host": from_host,
                    "from_port": from_port,
                    "from_base_url": from_base_url,
                    "created_at": int(time.time()),
                }
                cleanup_pair_requests_locked()
                request_snapshot = {
                    "request_id": request_id,
                    "from_device_id": from_device_id,
                    "from_device_name": from_device_name,
                    "from_host": from_host,
                    "from_port": from_port,
                    "created_at": int(time.time()),
                }

        if auto_accept:
            _logger.info("Auto-accepting pair request from %s (%s)", from_device_id, from_device_name)
            persist_paired_desktops()
            ok, error = send_pairing_response_callback(from_base_url, request_id, True, "")
            notify_desktop_clients(
                {
                    "type": "pairing_result",
                    "accepted": True,
                    "device_id": from_device_id,
                    "device_name": from_device_name,
                    "auto": True,
                    "callback_ok": ok,
                    "callback_error": error,
                }
            )
            return jsonify({"ok": True, "auto_accepted": True})

        notify_desktop_clients({"type": "pairing_request", "request": request_snapshot})
        return jsonify({"ok": True})

    @app.post("/pairing/respond")
    def respond_pair_request():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可处理配对请求"}), 403

        payload = request.get_json(silent=True) or {}
        request_id = normalize_device_identifier(payload.get("request_id"), max_len=64)
        if not request_id:
            return jsonify({"error": "缺少请求标识"}), 400
        accepted = bool(payload.get("accepted"))

        with lock:
            req = pending_pair_requests.pop(request_id, None)
        if req is None:
            return jsonify({"error": "配对请求不存在或已过期"}), 404

        callback_base_url = req.get("from_base_url") or f"http://{req['from_host']}:{int(req['from_port'])}"
        callback_ok, callback_error = send_pairing_response_callback(callback_base_url, request_id, accepted, "")

        if accepted:
            _logger.info("Pair request accepted: %s (%s)", req["from_device_id"], req["from_device_name"])
            with lock:
                paired_desktops[req["from_device_id"]] = {
                    "device_name": req["from_device_name"],
                    "host": req["from_host"],
                    "port": int(req["from_port"]),
                    "paired_at": int(time.time()),
                    "last_seen_at": int(time.time()),
                }
            persist_paired_desktops()

        notify_desktop_clients(
            {
                "type": "pairing_result",
                "accepted": accepted,
                "device_id": req["from_device_id"],
                "device_name": req["from_device_name"],
                "callback_ok": callback_ok,
                "callback_error": callback_error,
            }
        )
        notify_desktop_clients({"type": "pairing_list_updated"})
        return jsonify({"ok": True, "accepted": accepted, "callback_ok": callback_ok, "callback_error": callback_error})

    @app.post("/pairing/response")
    def receive_pair_response():
        payload = request.get_json(silent=True) or {}
        request_id = normalize_device_identifier(payload.get("request_id"), max_len=64)
        if not request_id:
            return jsonify({"error": "缺少请求标识"}), 400

        with lock:
            req = outgoing_pair_requests.pop(request_id, None)
        if req is None:
            return jsonify({"error": "配对请求不存在或已过期"}), 404

        accepted = bool(payload.get("accepted"))
        responder_device_id = normalize_device_id(payload.get("responder_device_id")) or req["target_device_id"]
        responder_device_name = normalize_peer_name(payload.get("responder_device_name"), fallback=req["target_device_name"])
        responder_host = str(request.remote_addr or req["target_host"]).strip() or req["target_host"]
        try:
            responder_port = int(payload.get("responder_port"))
        except (TypeError, ValueError):
            responder_port = int(req["target_port"])
        if responder_port <= 0 or responder_port > 65535:
            responder_port = int(req["target_port"])

        with lock:
            refresh_discovered_from_peer_locked(
                responder_device_id,
                responder_device_name,
                responder_host,
                responder_port,
                seen_at=time.time(),
            )
            if accepted:
                paired_desktops[responder_device_id] = {
                    "device_name": responder_device_name,
                    "host": responder_host,
                    "port": responder_port,
                    "paired_at": int(time.time()),
                    "last_seen_at": int(time.time()),
                }

        if accepted:
            persist_paired_desktops()

        notify_desktop_clients(
            {
                "type": "pairing_result",
                "accepted": accepted,
                "device_id": responder_device_id,
                "device_name": responder_device_name,
                "reason": str(payload.get("reason") or ""),
            }
        )
        notify_desktop_clients({"type": "pairing_list_updated"})
        return jsonify({"ok": True, "accepted": accepted})

    @app.post("/settings/upload-limit")
    def update_upload_limit():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可修改上传限制"}), 403

        payload = request.get_json(silent=True) or {}
        raw_limit = payload.get("max_upload_bytes")
        try:
            new_limit = int(raw_limit)
        except (TypeError, ValueError):
            return jsonify({"error": "max_upload_bytes 必须是整数"}), 400

        min_limit = 1 * 1024 * 1024
        max_limit = 100 * 1024 * 1024 * 1024
        if new_limit < min_limit or new_limit > max_limit:
            return jsonify({"error": "上传限制需在 1MB 到 100GB 之间"}), 400

        app.config["MAX_UPLOAD_BYTES"] = new_limit
        persist_runtime_setting("max_upload_bytes", new_limit)
        return jsonify({"ok": True, "max_upload_bytes": new_limit})

    @app.post("/settings/download-dir")
    def update_download_dir():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可修改下载目录"}), 403

        payload = request.get_json(silent=True) or {}
        raw_dir = str(payload.get("download_dir", "")).strip()
        normalized = normalize_download_dir(raw_dir)
        if normalized is None:
            return jsonify({"error": "下载目录必须是绝对路径"}), 400

        app.config["DOWNLOAD_DIR"] = normalized
        persist_runtime_setting("download_dir", str(normalized))
        return jsonify({"ok": True, "download_dir": str(normalized)})

    @app.post("/settings/open-download-dir")
    def open_download_dir():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可打开下载目录"}), 403

        download_dir_local = Path(app.config["DOWNLOAD_DIR"]).resolve()
        try:
            download_dir_local.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"目录不可用: {exc}"}), 500

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(download_dir_local))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(download_dir_local)])
            else:
                subprocess.Popen(["xdg-open", str(download_dir_local)])
        except Exception as exc:
            return jsonify({"error": f"打开目录失败: {exc}"}), 500

        return jsonify({"ok": True, "download_dir": str(download_dir_local)})

    @app.get("/settings/lan-ip")
    def get_lan_ip_candidates():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可查看局域网IP"}), 403
        candidates = get_lan_ipv4_candidates()
        selected_ip = get_selected_lan_ip(candidates)
        current_ip = selected_ip or (candidates[0] if candidates else "127.0.0.1")
        return jsonify({
            "candidates": candidates,
            "selected_ip": selected_ip,
            "current_ip": current_ip,
        })

    @app.post("/settings/lan-ip")
    def select_lan_ip():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可设置局域网IP"}), 403

        payload = request.get_json(silent=True) or {}
        raw_ip = str(payload.get("selected_ip", "")).strip()
        if raw_ip in ("", "auto"):
            # 恢复自动选择：删除已保存的手动选择，重启后按默认路由出口自动挑选
            try:
                with _settings_lock:
                    settings = _read_settings_unlocked()
                    settings.pop("selected_lan_ip", None)
                    _write_settings_unlocked(settings)
            except Exception as exc:
                return jsonify({"error": f"保存失败: {exc}"}), 500
            _logger.info("用户恢复局域网 IP 自动选择（需重启生效）")
            return jsonify({"ok": True, "selected_ip": "", "note": "已恢复自动选择，重启服务后生效"})

        candidates = get_lan_ipv4_candidates()
        if raw_ip not in candidates:
            return jsonify({"error": f"IP {raw_ip} 不在当前可用候选列表中", "candidates": candidates}), 400

        persist_runtime_setting("selected_lan_ip", raw_ip)
        _logger.info("用户选择 LAN IP: %s (需重启生效)", raw_ip)
        return jsonify({"ok": True, "selected_ip": raw_ip, "note": "重启服务后生效"})

    @app.get("/settings/discovery-port")
    def get_discovery_port():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可查看发现端口"}), 403
        return jsonify({"discovery_port": peer_discovery_port})

    @app.post("/settings/discovery-port")
    def set_discovery_port():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可设置发现端口"}), 403

        payload = request.get_json(silent=True) or {}
        try:
            new_port = int(payload.get("discovery_port", 0))
        except (TypeError, ValueError):
            return jsonify({"error": "discovery_port 必须是整数"}), 400

        if new_port < 1024 or new_port > 65535:
            return jsonify({"error": "发现端口需在 1024-65535 之间"}), 400

        nonlocal peer_discovery_port
        persist_runtime_setting("peer_discovery_port", new_port)
        _logger.info("用户设置发现端口: %d (需重启生效)", new_port)
        return jsonify({"ok": True, "discovery_port": new_port, "note": "重启服务后生效"})

    @app.post("/records/<record_id>/open-folder")
    def open_record_folder(record_id: str):
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可打开文件目录"}), 403

        row = history_row_by_id(record_id)
        if row is None:
            return jsonify({"error": "记录不存在"}), 404

        file_path_raw = str(row["file_path"] or "").strip()
        if not file_path_raw:
            return jsonify({"error": "记录缺少文件路径"}), 400

        entry_path = Path(file_path_raw).expanduser()
        target_dir = entry_path if entry_path.is_dir() else entry_path.parent
        if not target_dir.exists():
            return jsonify({"error": "目录不存在"}), 404

        try:
            if sys.platform.startswith("win"):
                if entry_path.exists() and entry_path.is_file():
                    subprocess.Popen(["explorer", "/select,", str(entry_path)])
                else:
                    os.startfile(str(target_dir))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target_dir)])
            else:
                subprocess.Popen(["xdg-open", str(target_dir)])
        except Exception as exc:
            return jsonify({"error": f"打开目录失败: {exc}"}), 500

        return jsonify({"ok": True, "folder": str(target_dir)})

    @app.post("/records/<record_id>/open-file")
    def open_record_file(record_id: str):
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可打开文件"}), 403

        row = history_row_by_id(record_id)
        if row is None:
            return jsonify({"error": "记录不存在"}), 404

        file_path_raw = str(row["file_path"] or "").strip()
        if not file_path_raw:
            return jsonify({"error": "记录缺少文件路径"}), 400

        entry_path = Path(file_path_raw).expanduser()
        if not entry_path.exists() or not entry_path.is_file():
            return jsonify({"error": "文件不存在"}), 404

        try:
            if sys.platform.startswith("win"):
                os.startfile(str(entry_path))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(entry_path)])
            else:
                subprocess.Popen(["xdg-open", str(entry_path)])
        except Exception as exc:
            return jsonify({"error": f"打开文件失败: {exc}"}), 500

        return jsonify({"ok": True, "file": str(entry_path)})

    @app.post("/peer/upload")
    def receive_peer_upload():
        source_peer_device_id = normalize_device_id(request.headers.get("X-Peer-Device-Id"))
        if not source_peer_device_id:
            return jsonify({"error": "缺少来源设备标识"}), 400
        source_peer_name_header = decode_header_text(request.headers.get("X-Peer-Device-Name"))
        source_peer_name_hint = normalize_peer_name(
            source_peer_name_header,
            fallback=f"电脑-{source_peer_device_id[:8]}",
        )

        remote_ip = str(request.remote_addr or "").strip()
        if not remote_ip:
            return jsonify({"error": "无法识别来源地址"}), 400

        with lock:
            peer = paired_desktops.get(source_peer_device_id)
            if peer is None:
                # 仅允许“来源 IP 与某条配对记录一致”时按新设备 ID 归并（同一台机器换 ID/多实例场景）。
                # 不再按设备名匹配：设备名可伪造，防止攻击者顶替已有配对条目。
                same_host_peers = [
                    (peer_id, peer_item)
                    for peer_id, peer_item in paired_desktops.items()
                    if str(peer_item.get("host") or "") == remote_ip
                ]
                if same_host_peers:
                    chosen = max(same_host_peers, key=lambda item: int(item[1].get("paired_at") or 0))
                    old_peer_id, old_peer = chosen
                    paired_desktops.pop(old_peer_id, None)
                    paired_desktops[source_peer_device_id] = old_peer
                    peer = old_peer
                else:
                    return jsonify({"error": "未配对设备，拒绝接收文件"}), 403
            else:
                # 来源校验：远端 IP 必须与配对记录或最近发现记录一致，
                # 防止局域网内其他设备伪造 X-Peer-Device-Id 向本机推送文件。
                known_hosts = {str(peer.get("host") or "").strip()}
                discovered = discovered_desktops.get(source_peer_device_id)
                if discovered is not None and str(discovered.get("host") or "").strip():
                    known_hosts.add(str(discovered["host"]).strip())
                if remote_ip not in known_hosts:
                    _logger.warning(
                        "Rejected peer upload: device %s claimed from %s, known hosts %s",
                        source_peer_device_id,
                        remote_ip,
                        sorted(known_hosts),
                    )
                    return jsonify({"error": "设备来源地址与配对记录不一致，请重新配对后重试"}), 403
            peer_name = normalize_peer_name(
                source_peer_name_header,
                fallback=str(peer.get("device_name") or f"电脑-{source_peer_device_id[:8]}"),
            )
            peer["device_name"] = peer_name
            peer["host"] = remote_ip
            try:
                remote_port = int(request.headers.get("X-Peer-Port"))
            except (TypeError, ValueError):
                remote_port = int(peer.get("port") or 0)
            if 1 <= remote_port <= 65535:
                peer["port"] = remote_port
            peer["last_seen_at"] = int(time.time())

        persist_paired_desktops()

        uploaded = request.files.get("file")
        if uploaded is None or uploaded.filename == "":
            return jsonify({"error": "缺少文件"}), 400

        original_name = normalize_uploaded_filename(uploaded.filename)
        target_dir = Path(app.config["DOWNLOAD_DIR"]).resolve()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"保存目录不可用: {exc}"}), 500

        with file_lock:
            destination = allocate_unique_file_path(target_dir, original_name, reserve=True)
        max_upload_bytes_local = app.config["MAX_UPLOAD_BYTES"]
        content_len = request.content_length
        if content_len is not None and content_len > max_upload_bytes_local + 1024 * 1024:
            return jsonify({"error": "上传文件超过大小限制"}), 413

        try:
            size = stream_to_disk(uploaded.stream, destination, max_bytes=max_upload_bytes_local)
        except Exception as exc:
            if destination.exists():
                destination.unlink(missing_ok=True)
            if isinstance(exc, ValueError):
                return jsonify({"error": str(exc)}), 413
            return jsonify({"error": f"保存失败: {exc}"}), 500

        transfer_id = uuid.uuid4().hex
        created_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": transfer_id,
            "name": destination.name,
            "size": size,
            "source": "desktop",
            "created_at": created_at_text,
            "path": destination,
            "transient": False,
            "device_id": source_peer_device_id,
            "device_name": peer_name,
            "direction": "上传",
            "status": "成功",
        }

        cache_record(record)

        try:
            insert_history_record(
                history_id=transfer_id,
                device_id=source_peer_device_id,
                device_name=peer_name,
                file_name=destination.name,
                file_path=str(destination),
                direction="上传",
                status="成功",
                file_size=size,
                source="desktop",
                desktop_side="incoming",
                timestamp_text=created_at_text,
            )
        except Exception as exc:
            remove_record_and_file(transfer_id)
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        send_history_event(transfer_id, target_device_id=DESKTOP_DEVICE_ID)
        row = history_row_by_id(transfer_id)
        if row is None:
            return jsonify({"error": "历史记录不存在"}), 500
        return jsonify({"ok": True, "size": size, "record": public_history_record(row, include_file_path=True)})

    @app.post("/upload-desktop-path")
    def upload_desktop_path():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可使用本地路径上传"}), 403

        payload = request.get_json(silent=True) or {}
        raw_file_path = str(payload.get("file_path", "")).strip()
        if not raw_file_path:
            return jsonify({"error": "缺少 file_path"}), 400

        source_path = Path(raw_file_path).expanduser()
        if not source_path.is_absolute():
            return jsonify({"error": "file_path 必须是绝对路径"}), 400
        source_path = source_path.resolve()
        if not source_path.exists() or not source_path.is_file():
            return jsonify({"error": "源文件不存在"}), 404

        target_device_id = get_requested_desktop_target_id()
        device_id, device_name, target_peer, target_error = resolve_desktop_transfer_target(target_device_id)
        if target_error:
            return jsonify({"error": target_error}), 400

        try:
            file_size = int(source_path.stat().st_size)
        except Exception as exc:
            return jsonify({"error": f"读取文件信息失败: {exc}"}), 500

        if target_peer is not None:
            try:
                with source_path.open("rb") as fp:
                    ok, error, _payload = relay_file_to_paired_desktop(
                        target_peer=target_peer,
                        file_name=source_path.name,
                        file_stream=fp,
                        file_size_hint=file_size,
                    )
            except OSError as exc:
                return jsonify({"error": f"读取源文件失败: {exc}"}), 500
            except Exception as exc:
                return jsonify({"error": f"发送到目标电脑失败: {exc}"}), 502

            if not ok:
                return jsonify({"error": error or "发送到目标电脑失败"}), 502

            public_record, history_error = record_desktop_send_history(
                file_name=source_path.name,
                file_path=str(source_path),
                file_size=file_size,
                device_id=device_id,
                device_name=device_name,
            )
            if history_error:
                return jsonify({"error": history_error}), 500
            return jsonify({"ok": True, "record": public_record, "relayed": True})

        transfer_id = uuid.uuid4().hex
        created_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        record = {
            "id": transfer_id,
            "name": source_path.name,
            "size": file_size,
            "source": "desktop",
            "created_at": created_at_text,
            "path": source_path,
            "transient": False,
            "device_id": device_id,
            "device_name": device_name,
            "direction": "上传",
            "status": "成功",
        }

        cache_record(record)

        try:
            insert_history_record(
                history_id=transfer_id,
                device_id=device_id,
                device_name=device_name,
                file_name=source_path.name,
                file_path=str(source_path),
                direction="上传",
                status="成功",
                file_size=file_size,
                source="desktop",
                desktop_side="outgoing",
                timestamp_text=created_at_text,
            )
        except Exception as exc:
            with lock:
                record_map.pop(transfer_id, None)
                records[:] = [r for r in records if r["id"] != transfer_id]
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        send_history_event(transfer_id, target_device_id=device_id)
        row = history_row_by_id(transfer_id)
        if row is None:
            return jsonify({"error": "历史记录不存在"}), 500
        return jsonify({"ok": True, "record": public_history_record(row, include_file_path=True)})

    @app.post("/upload")
    def upload_file():
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        uploaded = request.files.get("file")
        source = "desktop" if is_trusted_desktop(request.remote_addr) else "mobile"
        if source == "desktop":
            target_device_id = get_requested_desktop_target_id()
            device_id, device_name, target_peer, target_error = resolve_desktop_transfer_target(target_device_id)
            if target_error:
                return jsonify({"error": target_error}), 400
        else:
            target_peer = None
            try:
                device_id, device_name, _ = resolve_request_device()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        if uploaded is None or uploaded.filename == "":
            return jsonify({"error": "缺少文件"}), 400

        original_name = normalize_uploaded_filename(uploaded.filename)
        transfer_id = uuid.uuid4().hex
        is_transient = source == "desktop"
        if is_transient:
            safe_name = sanitize_filename_for_windows(original_name)
            saved_name = f"{int(time.time())}_{transfer_id}_{safe_name}"
            target_dir = app.config["TRANSIENT_UPLOAD_DIR"]
        else:
            target_dir = Path(app.config["DOWNLOAD_DIR"]).resolve()
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return jsonify({"error": f"保存目录不可用: {exc}"}), 500
        with file_lock:
            if is_transient:
                # 瞬态文件使用“时间戳+ID+原名”唯一前缀落盘，避免与真实下载目录重名混淆
                destination = allocate_unique_file_path(target_dir, saved_name, reserve=True)
            else:
                destination = allocate_unique_file_path(target_dir, original_name, reserve=True)
        stored_name = original_name if is_transient else destination.name

        max_upload_bytes_local = app.config["MAX_UPLOAD_BYTES"]
        content_len = request.content_length
        if content_len is not None and content_len > max_upload_bytes_local + 1024 * 1024:
            return jsonify({"error": "上传文件超过大小限制"}), 413

        if source == "desktop" and target_peer is not None:
            size_hint = 0
            try:
                size_hint = int(uploaded.content_length or 0)
            except (TypeError, ValueError):
                size_hint = 0
            try:
                ok, error, payload = relay_file_to_paired_desktop(
                    target_peer=target_peer,
                    file_name=original_name,
                    file_stream=uploaded.stream,
                    file_size_hint=size_hint,
                )
            except Exception as exc:
                return jsonify({"error": f"发送到目标电脑失败: {exc}"}), 502
            if not ok:
                return jsonify({"error": error or "发送到目标电脑失败"}), 502

            relayed_size = 0
            try:
                relayed_size = int(payload.get("size") or 0)
            except (TypeError, ValueError):
                relayed_size = 0
            effective_size = relayed_size if relayed_size > 0 else max(0, size_hint)

            public_record, history_error = record_desktop_send_history(
                file_name=original_name,
                file_path=f"[relay]{original_name}",
                file_size=effective_size,
                device_id=device_id,
                device_name=device_name,
            )
            if history_error:
                return jsonify({"error": history_error}), 500
            return jsonify({"ok": True, "record": public_record, "relayed": True})

        try:
            size = stream_to_disk(uploaded.stream, destination, max_bytes=max_upload_bytes_local)
        except Exception as exc:
            if destination.exists():
                destination.unlink(missing_ok=True)
            if isinstance(exc, ValueError):
                return jsonify({"error": str(exc)}), 413
            return jsonify({"error": f"保存失败: {exc}"}), 500

        created_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "id": transfer_id,
            "name": stored_name,
            "size": size,
            "source": source,
            "created_at": created_at_text,
            "path": destination,
            "transient": is_transient,
            "device_id": device_id,
            "device_name": device_name,
            "direction": "上传",
            "status": "成功",
        }

        cache_record(record)

        try:
            insert_history_record(
                history_id=transfer_id,
                device_id=device_id,
                device_name=device_name,
                file_name=stored_name,
                file_path=str(destination),
                direction="上传",
                status="成功",
                file_size=size,
                source=source,
                desktop_side="incoming" if source == "mobile" else "outgoing",
                timestamp_text=created_at_text,
            )
        except Exception as exc:
            remove_record_and_file(transfer_id)
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        send_history_event(transfer_id, target_device_id=device_id)
        row = history_row_by_id(transfer_id)
        if row is None:
            return jsonify({"error": "历史记录不存在"}), 500
        return jsonify({"ok": True, "record": public_history_record(row, include_file_path=is_trusted_desktop(request.remote_addr))})

    @app.get("/files/<transfer_id>")
    def download_file(transfer_id: str):
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        record = cached_or_db_record(transfer_id)
        if record is None:
            return jsonify({"error": "文件不存在"}), 404
        if not is_trusted_desktop(request.remote_addr):
            try:
                req_device_id, req_device_name, _ = resolve_request_device()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            if record.get("device_id") != req_device_id:
                return jsonify({"error": "无权访问该文件"}), 403
        else:
            req_device_id = DESKTOP_DEVICE_ID

        try:
            response = send_file(
                record["path"],
                as_attachment=True,
                download_name=record["name"],
                conditional=True,
            )
        except Exception as exc:
            return jsonify({"error": f"文件不可用: {exc}"}), 404

        def _mark_downloaded() -> None:
            # 响应真正完成后才标记“已下载”，避免中断/失败的下载被记为成功
            try:
                update_history_status(transfer_id, "已下载")
            except Exception:
                pass
            with lock:
                active = record_map.get(transfer_id)
                if active is not None:
                    active["status"] = "已下载"
            send_history_update_event(transfer_id, target_device_id=req_device_id)

        response.call_on_close(_mark_downloaded)
        if record.get("transient"):
            source_resolved = Path(record["path"]).resolve()
            response.call_on_close(
                lambda transfer_id=transfer_id, source_path=source_resolved: cleanup_transient_record_file(
                    transfer_id,
                    source_path,
                    source_path,
                    keep_source_when_same=False,
                )
            )
        return attach_response_close_hooks(response)

    @app.post("/files/<transfer_id>/save")
    def save_file_to_download_dir(transfer_id: str):
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        record = cached_or_db_record(transfer_id)
        if record is None:
            return jsonify({"error": "文件不存在"}), 404

        source_path = record.get("path")
        if not isinstance(source_path, Path) or not source_path.exists():
            return jsonify({"error": "源文件不可用"}), 404
        try:
            source_resolved = source_path.resolve()
        except Exception as exc:
            return jsonify({"error": f"源文件路径不可用: {exc}"}), 500
        if not is_trusted_desktop(request.remote_addr):
            try:
                req_device_id, _req_device_name, _ = resolve_request_device()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            if record.get("device_id") != req_device_id:
                return jsonify({"error": "无权保存该文件"}), 403
        else:
            req_device_id = DESKTOP_DEVICE_ID

        download_dir_local = Path(app.config["DOWNLOAD_DIR"]).resolve()
        target_path: Optional[Path] = None
        source_parent_matches_download_dir = False
        try:
            download_dir_local.mkdir(parents=True, exist_ok=True)
            source_parent_matches_download_dir = source_resolved.parent == download_dir_local
            if source_parent_matches_download_dir:
                target_path = source_resolved
            else:
                with file_lock:
                    target_path = allocate_unique_file_path(download_dir_local, record["name"], reserve=True)
                shutil.copy2(source_path, target_path)
            target_resolved = target_path.resolve()
        except Exception as exc:
            if target_path is not None and not source_parent_matches_download_dir:
                try:
                    if target_path.exists():
                        target_path.unlink(missing_ok=True)
                except Exception:
                    _logger.debug("Failed to clean up failed save target for %s", record.get("name", "unknown"))
            return jsonify({"error": f"保存失败: {exc}"}), 500

        try:
            update_history_record(
                transfer_id,
                status="已下载",
                file_name=target_path.name,
                file_path=str(target_path),
            )
            with lock:
                active = record_map.get(transfer_id)
                if active is not None:
                    active["status"] = "已下载"
                    active["name"] = target_path.name
                    active["path"] = target_path
        except Exception as exc:
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        if record.get("transient"):
            cleanup_transient_record_file(
                transfer_id,
                source_resolved,
                target_resolved,
                keep_source_when_same=True,
            )

        send_history_update_event(transfer_id, target_device_id=req_device_id)
        row = history_row_by_id(transfer_id)
        if row is None:
            return jsonify({"error": "历史记录不存在"}), 500

        return jsonify(
            {
                "ok": True,
                "saved_path": str(target_path),
                "file_name": target_path.name,
                "download_dir": str(download_dir_local),
                "record": public_history_record(
                    row,
                    include_file_path=is_trusted_desktop(request.remote_addr),
                ),
            }
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/auth/mobile-token")
    def get_mobile_token():
        if not is_trusted_desktop(request.remote_addr):
            return jsonify({"error": "仅电脑端可刷新二维码"}), 403
        return jsonify(get_mobile_qr_payload(force_new=True))

    @sock.route("/ws")
    def ws_handler(ws):
        # CSWSH 防护：非浏览器客户端（无 Origin）放行；浏览器必须来自受信页面
        origin = request.headers.get("Origin") or request.headers.get("Sec-WebSocket-Origin")
        if not origin_allowed(origin):
            _logger.warning("WS connection from untrusted origin %s rejected", origin)
            ws.close()
            return
        if not authorize_request(allow_query_session=True):
            ws.close()
            return

        is_desktop_client = is_trusted_desktop(request.remote_addr)
        device_id = DESKTOP_DEVICE_ID
        if not is_desktop_client:
            try:
                device_id, _device_name, _ = resolve_request_device(allow_query=True)
            except ValueError:
                ws.close()
                return

        init_rows = history_rows(include_all=is_desktop_client, device_id=None if is_desktop_client else device_id)
        init_records = [public_history_record(row, include_file_path=is_desktop_client) for row in init_rows]

        with lock:
            clients[ws] = {"is_desktop": is_desktop_client, "device_id": device_id}
        ws.send(json.dumps({"type": "init", "records": init_records}, ensure_ascii=False))

        try:
            while True:
                message = ws.receive()
                if message is None:
                    break
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if data.get("type") == "ping":
                    ws.send(json.dumps({"type": "pong", "ts": int(time.time() * 1000)}))
        finally:
            with lock:
                clients.pop(ws, None)

    return app


def start_server(
    port: int = 5000,
    save_dir: Optional[Path] = None,
    auto_open_browser: bool = True,
    print_terminal_qr: bool = True,
    strict_port: bool = False,
) -> None:
    upload_dir = (save_dir or default_save_dir()).resolve()
    transient_upload_dir = default_transient_dir().resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    transient_upload_dir.mkdir(parents=True, exist_ok=True)

    selected_port = port if strict_port else find_available_port(port)
    if strict_port:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", port))
        except OSError as exc:
            raise RuntimeError(f"端口 {port} 被占用，无法启动。") from exc
        finally:
            probe.close()

    if selected_port != port:
        print(f"Port {port} is occupied, switched to {selected_port}")

    lan_ip_candidates = get_lan_ipv4_candidates()
    # 优先使用用户在设置中选择的局域网 IP；未选择或已失效时回退候选列表第一个
    lan_ip = get_selected_lan_ip(lan_ip_candidates) or (lan_ip_candidates[0] if lan_ip_candidates else "127.0.0.1")
    base_url = f"http://{lan_ip}:{selected_port}"
    initial_mobile_token = uuid.uuid4().hex
    mobile_url = f"{base_url}/?token={initial_mobile_token}"
    desktop_url = f"{base_url}/?role=desktop"

    if print_terminal_qr:
        print(f"Save directory: {upload_dir}")
        print(f"Open in browser (desktop): {desktop_url}")
        print(f"QR target (mobile): {mobile_url}")
        print_qr(mobile_url)

    if auto_open_browser:
        def open_desktop_page() -> None:
            time.sleep(1.0)
            try:
                webbrowser.open(desktop_url, new=1)
            except Exception as exc:
                print(f"Auto-open browser skipped: {exc}")

        threading.Thread(target=open_desktop_page, daemon=True).start()

    runtime_settings = load_runtime_settings()
    runtime_max_upload = runtime_settings.get("max_upload_bytes")
    if not isinstance(runtime_max_upload, int) or runtime_max_upload <= 0:
        runtime_max_upload = 10 * 1024 * 1024 * 1024

    runtime_download_dir = normalize_download_dir(str(runtime_settings.get("download_dir", "")))
    if runtime_download_dir is None:
        runtime_download_dir = default_download_dir()
    local_device_id, local_device_name = load_or_create_local_device_identity()

    app = create_app(
        upload_dir=upload_dir,
        transient_upload_dir=transient_upload_dir,
        base_url=base_url,
        lan_ip=lan_ip,
        lan_ip_candidates=lan_ip_candidates,
        http_port=selected_port,
        local_device_id=local_device_id,
        local_device_name=local_device_name,
        initial_mobile_token=initial_mobile_token,
        max_upload_bytes=runtime_max_upload,
        download_dir=runtime_download_dir,
    )
    app.run(host="0.0.0.0", port=selected_port, threaded=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LAN file transfer server")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--save-dir", default=None, help="保存目录（默认自动选择）")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开电脑端页面")
    parser.add_argument("--no-terminal-qr", action="store_true", help="不在终端打印二维码")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    start_server(
        port=args.port,
        save_dir=resolve_save_dir(args.save_dir),
        auto_open_browser=not args.no_browser,
        print_terminal_qr=not args.no_terminal_qr,
    )


if __name__ == "__main__":
    main()
