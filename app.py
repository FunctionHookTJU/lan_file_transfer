import argparse
import base64
import io
import json
import os
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

from flask import Flask, jsonify, make_response, render_template, request, send_file
from flask_sock import Sock
from qrcode import QRCode
import requests
from werkzeug.utils import secure_filename

APP_NAME = "LANFileTransfer"
DESKTOP_DEVICE_ID = "desktop"


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


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
        user_profile = os.getenv("USERPROFILE")
        if user_profile:
            return (Path(user_profile) / "Downloads").resolve()
    return (Path.home() / "Downloads").resolve()


def settings_file_path() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    settings_dir = (base / "LANFileTransfer").resolve()
    settings_dir.mkdir(parents=True, exist_ok=True)
    return settings_dir / "settings.json"


def load_runtime_settings() -> dict:
    path = settings_file_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_runtime_settings(settings: dict) -> None:
    path = settings_file_path()
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_download_dir(raw_dir: str) -> Optional[Path]:
    if not raw_dir:
        return None
    candidate = Path(raw_dir.strip()).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate.resolve()


def sanitize_filename_for_windows(name: str) -> str:
    invalid = '<>:"/\\|?*'
    result = "".join("_" if ch in invalid else ch for ch in (name or ""))
    result = result.strip(" .")
    return result or "downloaded_file"


def allocate_unique_file_path(directory: Path, desired_name: str) -> Path:
    clean_name = sanitize_filename_for_windows(desired_name)
    stem = Path(clean_name).stem or "downloaded_file"
    suffix = Path(clean_name).suffix
    candidate = directory / clean_name
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1
    return candidate


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
    trusted_desktop_ips = {"127.0.0.1", "::1", lan_ip}
    peer_discovery_port = 54546
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
                    source TEXT NOT NULL DEFAULT 'mobile'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfer_history_device_ts ON transfer_history(device_id, timestamp)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_transfer_history_ts ON transfer_history(timestamp)"
            )

    def normalize_device_id(raw: Optional[str]) -> str:
        return normalize_device_identifier(raw)

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

    def check_peer_health(host: str, port: int) -> bool:
        url = f"http://{host}:{int(port)}/health"
        try:
            resp = requests.get(url, timeout=(0.35, 0.6))
        except requests.RequestException:
            return False
        return resp.status_code == 200

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

            for port in candidate_ports:
                endpoint = (host, int(port))
                if exclude_endpoint is not None and endpoint == exclude_endpoint:
                    continue
                if not check_peer_health(host, port):
                    continue
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
                except Exception:
                    pass
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
        timestamp_text: Optional[str] = None,
    ) -> None:
        ts = timestamp_text or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with history_connection() as conn:
            conn.execute(
                """
                INSERT INTO transfer_history
                (id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )

    def update_history_status(history_id: str, status: str) -> None:
        with history_connection() as conn:
            conn.execute("UPDATE transfer_history SET status = ? WHERE id = ?", (status, history_id))

    def history_rows(include_all: bool, device_id: Optional[str]) -> list[sqlite3.Row]:
        with history_connection() as conn:
            if include_all:
                cursor = conn.execute(
                    """
                    SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source
                    FROM transfer_history
                    ORDER BY timestamp ASC, id ASC
                    """
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source
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
                SELECT id, device_id, device_name, file_name, file_path, direction, timestamp, status, file_size, source
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
            "created_at": str(row["timestamp"]),
            "download_url": f"/files/{history_id}" if active is not None else "",
        }

    def send_history_event(history_id: str, target_device_id: str) -> None:
        row = history_row_by_id(history_id)
        if row is None:
            return
        broadcast({"type": "new_record", "record": public_history_record(row)}, target_device_id=target_device_id)

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
            settings = load_runtime_settings()
            settings[key] = value
            save_runtime_settings(settings)
        except Exception:
            pass

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
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            listener.bind(("0.0.0.0", peer_discovery_port))
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

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
                    try:
                        sender.sendto(packet, ("255.255.255.255", peer_discovery_port))
                    except OSError:
                        pass
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

    ensure_history_schema()
    load_paired_desktops()
    start_peer_discovery()

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
            refresh_discovered_from_peer_locked(from_device_id, from_device_name, from_host, from_port, seen_at=time.time())
            existing_pair = paired_desktops.get(from_device_id)
            if existing_pair is not None:
                existing_pair["device_name"] = from_device_name
                existing_pair["host"] = from_host
                existing_pair["port"] = from_port
                existing_pair["last_seen_at"] = int(time.time())
                auto_accept = True
            else:
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
                same_host_peers = [
                    (peer_id, peer_item)
                    for peer_id, peer_item in paired_desktops.items()
                    if str(peer_item.get("host") or "") == remote_ip
                ]
                chosen: Optional[tuple[str, dict]] = None
                if same_host_peers:
                    name_matched = [
                        item
                        for item in same_host_peers
                        if normalize_peer_name(item[1].get("device_name"), fallback="") == source_peer_name_hint
                    ]
                    pool = name_matched if name_matched else same_host_peers
                    chosen = max(pool, key=lambda item: int(item[1].get("paired_at") or 0))
                elif paired_desktops:
                    name_candidates = [
                        (peer_id, peer_item)
                        for peer_id, peer_item in paired_desktops.items()
                        if normalize_peer_name(peer_item.get("device_name"), fallback="") == source_peer_name_hint
                    ]
                    if len(name_candidates) == 1:
                        chosen = name_candidates[0]

                if chosen is not None:
                    old_peer_id, old_peer = chosen
                    paired_desktops.pop(old_peer_id, None)
                    paired_desktops[source_peer_device_id] = old_peer
                    peer = old_peer
                else:
                    return jsonify({"error": "未配对设备，拒绝接收文件"}), 403
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

        original_name = uploaded.filename.strip()
        target_dir = Path(app.config["DOWNLOAD_DIR"]).resolve()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return jsonify({"error": f"保存目录不可用: {exc}"}), 500

        destination = allocate_unique_file_path(target_dir, original_name)
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

        with lock:
            records.append(record)
            record_map[transfer_id] = record

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

        with lock:
            records.append(record)
            record_map[transfer_id] = record

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

        original_name = uploaded.filename.strip()
        transfer_id = uuid.uuid4().hex
        is_transient = source == "desktop"
        if is_transient:
            safe_name = secure_filename(original_name) or f"file-{int(time.time())}"
            saved_name = f"{int(time.time())}_{transfer_id}_{safe_name}"
            target_dir = app.config["TRANSIENT_UPLOAD_DIR"]
            destination = target_dir / saved_name
            stored_name = original_name
        else:
            target_dir = Path(app.config["DOWNLOAD_DIR"]).resolve()
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                return jsonify({"error": f"保存目录不可用: {exc}"}), 500
            destination = allocate_unique_file_path(target_dir, original_name)
            stored_name = destination.name

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

        with lock:
            records.append(record)
            record_map[transfer_id] = record

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

        with lock:
            record = record_map.get(transfer_id)

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
            req_device_name = "电脑端"

        try:
            update_history_status(transfer_id, "已下载")
            download_history_id = uuid.uuid4().hex
            insert_history_record(
                history_id=download_history_id,
                device_id=req_device_id,
                device_name=req_device_name,
                file_name=record["name"],
                file_path=str(record["path"]),
                direction="下载",
                status="成功",
                file_size=int(record["size"]),
                source="desktop" if is_trusted_desktop(request.remote_addr) else "mobile",
            )
        except Exception as exc:
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        response = send_file(
            record["path"],
            as_attachment=True,
            download_name=record["name"],
            conditional=True,
        )
        send_history_event(download_history_id, target_device_id=req_device_id)
        return response

    @app.post("/files/<transfer_id>/save")
    def save_file_to_download_dir(transfer_id: str):
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        with lock:
            record = record_map.get(transfer_id)

        if record is None:
            return jsonify({"error": "文件不存在"}), 404

        source_path = record.get("path")
        if not isinstance(source_path, Path) or not source_path.exists():
            return jsonify({"error": "源文件不可用"}), 404
        if not is_trusted_desktop(request.remote_addr):
            try:
                req_device_id, _req_device_name, _ = resolve_request_device()
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            if record.get("device_id") != req_device_id:
                return jsonify({"error": "无权保存该文件"}), 403

        download_dir_local = Path(app.config["DOWNLOAD_DIR"]).resolve()
        try:
            download_dir_local.mkdir(parents=True, exist_ok=True)
            source_resolved = source_path.resolve()
            if source_resolved.parent == download_dir_local:
                target_path = source_resolved
            else:
                target_path = allocate_unique_file_path(download_dir_local, record["name"])
                shutil.copy2(source_path, target_path)
        except Exception as exc:
            return jsonify({"error": f"保存失败: {exc}"}), 500

        try:
            update_history_status(transfer_id, "已保存")
            saved_history_id = uuid.uuid4().hex
            insert_history_record(
                history_id=saved_history_id,
                device_id=DESKTOP_DEVICE_ID,
                device_name="电脑端",
                file_name=target_path.name,
                file_path=str(target_path),
                direction="下载",
                status="成功",
                file_size=int(record["size"]),
                source="desktop",
            )
        except Exception as exc:
            return jsonify({"error": f"写入历史记录失败: {exc}"}), 500

        if record.get("transient"):
            remove_record_and_file(transfer_id)

        send_history_event(saved_history_id, target_device_id=DESKTOP_DEVICE_ID)

        return jsonify(
            {
                "ok": True,
                "saved_path": str(target_path),
                "file_name": target_path.name,
                "download_dir": str(download_dir_local),
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

    lan_ip = get_lan_ip()
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
