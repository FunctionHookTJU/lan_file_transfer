import argparse
import base64
import io
import json
import os
import socket
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request, send_file
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


def create_app(upload_dir: Path, mobile_url: str, template_dir: Optional[Path] = None) -> Flask:
    app = Flask(__name__, template_folder=str(template_dir or runtime_template_dir()))
    app.config["UPLOAD_DIR"] = upload_dir
    app.config["JSON_AS_ASCII"] = False
    app.config["MOBILE_URL"] = mobile_url
    app.config["MOBILE_QR_DATA_URL"] = build_qr_data_url(mobile_url)

    sock = Sock(app)
    records = []
    record_map = {}
    clients = set()
    lock = threading.Lock()

    def public_record(record: dict) -> dict:
        return {
            "id": record["id"],
            "name": record["name"],
            "size": record["size"],
            "source": record["source"],
            "created_at": record["created_at"],
            "download_url": f"/files/{record['id']}",
        }

    def stream_to_disk(file_stream, destination: Path, chunk_size: int = 1024 * 1024) -> int:
        total = 0
        with destination.open("wb") as f:
            while True:
                chunk = file_stream.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
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

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            mobile_url=app.config["MOBILE_URL"],
            mobile_qr_data_url=app.config["MOBILE_QR_DATA_URL"],
        )

    @app.get("/records")
    def get_records():
        with lock:
            data = [public_record(r) for r in records]
        return jsonify({"records": data})

    @app.post("/upload")
    def upload_file():
        uploaded = request.files.get("file")
        source = request.form.get("source", "mobile")

        if uploaded is None or uploaded.filename == "":
            return jsonify({"error": "缺少文件"}), 400

        original_name = uploaded.filename.strip()
        safe_name = secure_filename(original_name) or f"file-{int(time.time())}"
        transfer_id = uuid.uuid4().hex
        saved_name = f"{int(time.time())}_{transfer_id}_{safe_name}"
        destination = app.config["UPLOAD_DIR"] / saved_name

        try:
            size = stream_to_disk(uploaded.stream, destination)
        except Exception as exc:
            return jsonify({"error": f"保存失败: {exc}"}), 500

        record = {
            "id": transfer_id,
            "name": original_name,
            "size": size,
            "source": source,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "path": destination,
        }

        with lock:
            records.append(record)
            record_map[transfer_id] = record

        event = {"type": "new_record", "record": public_record(record)}
        broadcast(event)

        return jsonify({"ok": True, "record": public_record(record)})

    @app.get("/files/<transfer_id>")
    def download_file(transfer_id: str):
        with lock:
            record = record_map.get(transfer_id)

        if record is None:
            return jsonify({"error": "文件不存在"}), 404

        return send_file(
            record["path"],
            as_attachment=True,
            download_name=record["name"],
            conditional=True,
        )

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @sock.route("/ws")
    def ws_handler(ws):
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
) -> None:
    upload_dir = (save_dir or default_save_dir()).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)

    lan_ip = get_lan_ip()
    base_url = f"http://{lan_ip}:{port}"
    mobile_url = f"{base_url}/?role=mobile"
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

    app = create_app(upload_dir, mobile_url)
    app.run(host="0.0.0.0", port=port, threaded=True)


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
