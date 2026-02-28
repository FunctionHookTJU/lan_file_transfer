import argparse
import base64
import io
import json
import os
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, after_this_request, jsonify, make_response, render_template, request, send_file
from flask_sock import Sock
from qrcode import QRCode
from werkzeug.utils import secure_filename


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


def create_app(
    upload_dir: Path,
    transient_upload_dir: Path,
    base_url: str,
    lan_ip: str,
    initial_mobile_token: str,
    token_ttl_seconds: int = 120,
    session_ttl_seconds: int = 8 * 60 * 60,
    max_upload_bytes: int = 10 * 1024 * 1024 * 1024,
    download_dir: Optional[Path] = None,
    template_dir: Optional[Path] = None,
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

    sock = Sock(app)
    records = []
    record_map = {}
    clients = set()
    lock = threading.Lock()
    trusted_desktop_ips = {"127.0.0.1", "::1", lan_ip}
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

    def public_record(record: dict) -> dict:
        return {
            "id": record["id"],
            "name": record["name"],
            "size": record["size"],
            "source": record["source"],
            "created_at": record["created_at"],
            "download_url": f"/files/{record['id']}",
        }

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

        broadcast({"type": "remove_record", "id": transfer_id})

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

    def broadcast(event: dict) -> None:
        payload = json.dumps(event, ensure_ascii=False)
        dead = []
        with lock:
            targets = list(clients)
        for ws in targets:
            try:
                ws.send(payload)
            except Exception:
                dead.append(ws)
        if dead:
            with lock:
                for ws in dead:
                    clients.discard(ws)

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

        with lock:
            data = [public_record(r) for r in records]
        return jsonify({"records": data})

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

    @app.post("/upload")
    def upload_file():
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        uploaded = request.files.get("file")
        source = "desktop" if is_trusted_desktop(request.remote_addr) else "mobile"

        if uploaded is None or uploaded.filename == "":
            return jsonify({"error": "缺少文件"}), 400

        original_name = uploaded.filename.strip()
        safe_name = secure_filename(original_name) or f"file-{int(time.time())}"
        transfer_id = uuid.uuid4().hex
        saved_name = f"{int(time.time())}_{transfer_id}_{safe_name}"
        is_transient = source == "desktop"
        target_dir = app.config["TRANSIENT_UPLOAD_DIR"] if is_transient else app.config["UPLOAD_DIR"]
        destination = target_dir / saved_name

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

        record = {
            "id": transfer_id,
            "name": original_name,
            "size": size,
            "source": source,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "path": destination,
            "transient": is_transient,
        }

        with lock:
            records.append(record)
            record_map[transfer_id] = record

        event = {"type": "new_record", "record": public_record(record)}
        broadcast(event)

        return jsonify({"ok": True, "record": public_record(record)})

    @app.get("/files/<transfer_id>")
    def download_file(transfer_id: str):
        if not authorize_request():
            return jsonify({"error": "未授权访问"}), 401

        with lock:
            record = record_map.get(transfer_id)

        if record is None:
            return jsonify({"error": "文件不存在"}), 404

        response = send_file(
            record["path"],
            as_attachment=True,
            download_name=record["name"],
            conditional=True,
        )
        if record.get("transient"):
            @after_this_request
            def cleanup_transient_file(resp):
                remove_record_and_file(transfer_id)
                return resp
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

        download_dir_local = Path(app.config["DOWNLOAD_DIR"]).resolve()
        try:
            download_dir_local.mkdir(parents=True, exist_ok=True)
            target_path = allocate_unique_file_path(download_dir_local, record["name"])
            shutil.copy2(source_path, target_path)
        except Exception as exc:
            return jsonify({"error": f"保存失败: {exc}"}), 500

        if record.get("transient"):
            remove_record_and_file(transfer_id)

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

        with lock:
            clients.add(ws)
            init_records = [public_record(r) for r in records]
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
                clients.discard(ws)

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

    app = create_app(
        upload_dir=upload_dir,
        transient_upload_dir=transient_upload_dir,
        base_url=base_url,
        lan_ip=lan_ip,
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
