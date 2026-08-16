# -*- coding: utf-8 -*-
"""LAN File Transfer 修复后冒烟测试（Flask test client，不启动真实服务器）。

覆盖：
1. 基础鉴权（非受信 IP 访问 /records 401）
2. Origin 跨站防护（写请求 + 白名单内放行 + 无 Origin 放行）
3. /peer/upload 来源校验（配对 IP 一致放行 / 不一致 403 / 未配对 403）
4. 配对 auto-accept 来源校验（一致自动接受 / 不一致转人工确认）
5. /upload 瞬态路径唯一前缀命名 + 记录名保持原名
6. 下载后状态"已下载"（响应完成后）+ 瞬态文件清理
7. 手机端 token 会话上传落盘
8. settings.json 持久化（隔离到临时文件）
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import app as backend  # noqa: E402

PASS = 0
FAIL = 0
FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  -> {detail}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lft_smoke_"))
    upload_dir = tmp / "received"
    transient_dir = tmp / "transient"
    download_dir = tmp / "downloads"
    history_db = tmp / "history.db"
    settings_file = tmp / "settings.json"
    for d in (upload_dir, transient_dir, download_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 隔离 settings：读写重定向到临时文件
    def _read():
        if not settings_file.exists():
            return {}
        try:
            return json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(settings: dict) -> None:
        settings_file.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")

    real_read = backend._read_settings_unlocked
    real_write = backend._write_settings_unlocked
    backend._read_settings_unlocked = _read
    backend._write_settings_unlocked = _write

    # 预置一个已配对电脑（peer1 @ 192.168.1.50:5001）
    _write({
        "paired_desktops": [
            {
                "device_id": "peer1",
                "device_name": "PeerPC",
                "host": "192.168.1.50",
                "port": 5001,
                "paired_at": int(time.time()),
            }
        ]
    })

    PORT = 5599
    app = backend.create_app(
        upload_dir=upload_dir,
        transient_upload_dir=transient_dir,
        base_url=f"http://127.0.0.1:{PORT}",
        lan_ip="127.0.0.1",
        lan_ip_candidates=[],
        http_port=PORT,
        local_device_id="test-local",
        local_device_name="TestPC",
        initial_mobile_token="tokentoken123",
        history_db=history_db,
        download_dir=download_dir,
    )
    client = app.test_client()

    try:
        print("== 1. 基础鉴权 ==")
        r = client.get("/health")
        check("GET /health 200", r.status_code == 200, f"got {r.status_code}")
        r = client.get("/records", environ_base={"REMOTE_ADDR": "192.168.1.99"})
        check("非受信 IP /records -> 401", r.status_code == 401, f"got {r.status_code}")
        r = client.get("/records")
        check("受信桌面 /records -> 200", r.status_code == 200, f"got {r.status_code}")

        print("== 2. Origin 跨站防护 ==")
        r = client.post(
            "/settings/upload-limit",
            json={"max_upload_bytes": 2 * 1024 * 1024 * 1024},
            headers={"Origin": "http://evil.example"},
        )
        check("恶意 Origin 写请求 -> 403", r.status_code == 403, f"got {r.status_code}")
        r = client.post(
            "/settings/upload-limit",
            json={"max_upload_bytes": 2 * 1024 * 1024 * 1024},
            headers={"Origin": f"http://127.0.0.1:{PORT}"},
        )
        check("白名单 Origin 写请求 -> 200", r.status_code == 200, f"got {r.status_code}")
        r = client.post(
            "/settings/upload-limit",
            json={"max_upload_bytes": 2 * 1024 * 1024 * 1024},
        )
        check("无 Origin（服务端调用）写请求 -> 200", r.status_code == 200, f"got {r.status_code}")

        print("== 2b. WebSocket 握手 Origin 校验（真实服务器 + 原始 socket）==")
        from werkzeug.serving import make_server  # noqa: E402
        import socket as _socket  # noqa: E402

        srv_ws = make_server("127.0.0.1", 5597, app, threaded=True)
        threading.Thread(target=srv_ws.serve_forever, daemon=True).start()
        try:
            def ws_handshake(origin: str) -> str:
                s = _socket.create_connection(("127.0.0.1", 5597), timeout=5)
                try:
                    req = (
                        "GET /ws HTTP/1.1\r\n"
                        "Host: 127.0.0.1:5597\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                        "Sec-WebSocket-Version: 13\r\n"
                        f"Origin: {origin}\r\n"
                        "\r\n"
                    )
                    s.sendall(req.encode())
                    return s.recv(4096).decode("latin-1").split("\r\n")[0]
                finally:
                    s.close()

            status_evil = ws_handshake("http://evil.example")
            check("WS 恶意 Origin 握手 -> 403", status_evil.startswith("HTTP/1.1 403"), f"got {status_evil!r}")
            status_ok = ws_handshake(f"http://127.0.0.1:{PORT}")
            check("WS 白名单 Origin 握手 -> 101", status_ok.startswith("HTTP/1.1 101"), f"got {status_ok!r}")
        finally:
            srv_ws.shutdown()

        print("== 3. /peer/upload 来源校验 ==")
        file_data = b"peer-file-content-123"
        headers = {
            "X-Peer-Device-Id": "peer1",
            "X-Peer-Device-Name": "PeerPC",
            "X-Peer-Port": "5001",
        }
        r = client.post(
            "/peer/upload",
            data={"source_device_name": "PeerPC", "file": (io.BytesIO(file_data), "peer_doc.txt", "application/octet-stream")},
            headers=headers,
            environ_base={"REMOTE_ADDR": "192.168.1.50"},
        )
        check("配对 IP 一致 -> 200", r.status_code == 200, f"got {r.status_code}: {r.get_data(as_text=True)[:200]}")
        saved = download_dir / "peer_doc.txt"
        check("文件落入下载目录且内容一致", saved.exists() and saved.read_bytes() == file_data, f"saved={saved.exists()}")

        r = client.post(
            "/peer/upload",
            data={"source_device_name": "PeerPC", "file": (io.BytesIO(b"x"), "evil.txt", "application/octet-stream")},
            headers=headers,
            environ_base={"REMOTE_ADDR": "192.168.1.99"},
        )
        check("伪造来源 IP -> 403", r.status_code == 403, f"got {r.status_code}")
        check("伪造请求未落盘", not (download_dir / "evil.txt").exists(), "evil.txt 已存在")

        r = client.post(
            "/peer/upload",
            data={"source_device_name": "Stranger", "file": (io.BytesIO(b"x"), "stranger.txt", "application/octet-stream")},
            headers={"X-Peer-Device-Id": "unknown-dev", "X-Peer-Device-Name": "Stranger", "X-Peer-Port": "5000"},
            environ_base={"REMOTE_ADDR": "192.168.1.88"},
        )
        check("未配对设备 -> 403", r.status_code == 403, f"got {r.status_code}")

        print("== 4. 配对 auto-accept 来源校验 ==")
        payload = {
            "request_id": "req-abc",
            "from_device_id": "peer1",
            "from_device_name": "PeerPC",
            "from_port": 5001,
            "from_base_url": "http://192.168.1.50:5001",
        }
        r = client.post("/pairing/request", json=payload, environ_base={"REMOTE_ADDR": "192.168.1.50"})
        body = r.get_json(silent=True) or {}
        check("来源一致 -> auto_accepted", r.status_code == 200 and body.get("auto_accepted") is True,
              f"got {r.status_code} {body}")

        r = client.post("/pairing/request", json=payload, environ_base={"REMOTE_ADDR": "192.168.1.99"})
        body = r.get_json(silent=True) or {}
        check("来源不一致 -> 转人工确认", r.status_code == 200 and body.get("auto_accepted") is not True,
              f"got {r.status_code} {body}")
        r = client.get("/pairing/pending")
        body = r.get_json(silent=True) or {}
        check("劫持请求进入待确认列表", any(item.get("request_id") == "req-abc" for item in body.get("requests", [])),
              f"pending={body.get('requests')}")

        print("== 5. /upload 瞬态路径唯一前缀 ==")
        r = client.post(
            "/upload",
            data={"source_device_name": "TestPC", "file": (io.BytesIO(b"jpeg-bytes"), "photo.jpg", "image/jpeg")},
        )
        body = r.get_json(silent=True) or {}
        rec = body.get("record") or {}
        check("桌面瞬态上传 -> 200", r.status_code == 200, f"got {r.status_code}: {body}")
        check("记录名保持原名", rec.get("name") == "photo.jpg", f"name={rec.get('name')}")
        transient_files = [p.name for p in transient_dir.iterdir() if p.is_file()]
        check(
            "瞬态文件使用唯一前缀名",
            any(name.startswith("1") and name.endswith("_photo.jpg") and "photo.jpg" != name for name in transient_files),
            f"files={transient_files}",
        )
        tid = rec.get("id")
        check("返回下载 URL", bool(rec.get("download_url")), f"download_url={rec.get('download_url')}")

        print("== 6. 下载（test client 数据完整性）==")
        r = client.get(f"/files/{tid}")
        check("下载 -> 200", r.status_code == 200, f"got {r.status_code}")
        check("下载内容一致", r.data == b"jpeg-bytes", f"len={len(r.data)}")

        print("== 6b. 真实服务器：call_on_close 下载状态与瞬态清理 ==")
        import urllib.request  # noqa: E402
        from werkzeug.serving import make_server  # noqa: E402

        srv = make_server("127.0.0.1", 5598, app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            import requests as _requests

            up = _requests.post(
                "http://127.0.0.1:5598/upload",
                files={"file": ("live_photo.png", io.BytesIO(b"live-bytes"), "image/png")},
                timeout=10,
            )
            live_rec = up.json().get("record") or {}
            check("真实服务器瞬态上传 -> 200", up.status_code == 200, f"got {up.status_code}")
            dl = _requests.get(f"http://127.0.0.1:5598/files/{live_rec.get('id')}", timeout=10)
            check("真实服务器下载 -> 200", dl.status_code == 200, f"got {dl.status_code}")
            check("真实服务器下载内容一致", dl.content == b"live-bytes", f"len={len(dl.content)}")
            del dl
            time.sleep(0.5)  # 等待响应 close 回调执行
            conn = backend.sqlite3.connect(str(history_db))
            row = conn.execute("SELECT status FROM transfer_history WHERE id = ?", (live_rec.get("id"),)).fetchone()
            conn.close()
            check("真实服务器：下载后状态=已下载", row is not None and row[0] == "已下载", f"row={row}")
            check(
                "真实服务器：瞬态文件已清理",
                not any(p.name.endswith("_live_photo.png") for p in transient_dir.iterdir()),
                "残留瞬态文件",
            )
        finally:
            srv.shutdown()

        print("== 7. 手机端 token 会话上传 ==")
        r = client.get("/?token=tokentoken123", environ_base={"REMOTE_ADDR": "192.168.1.99"})
        check("手机 token 换取会话 -> 200", r.status_code == 200, f"got {r.status_code}")
        cookie = r.headers.get("Set-Cookie", "").split(";")[0]
        check("下发会话 Cookie", cookie.startswith("lft_session="), f"cookie={cookie[:40]}")
        r = client.post(
            "/upload",
            data={"source_device_name": "Pixel", "file": (io.BytesIO(b"phone-data"), "from_phone.bin", "application/octet-stream")},
            headers={"X-Device-Id": "phone1", "X-Device-Name": "Pixel"},
            environ_base={"REMOTE_ADDR": "192.168.1.99"},
        )
        body = r.get_json(silent=True) or {}
        check("手机上传 -> 200", r.status_code == 200, f"got {r.status_code}: {body}")
        check("手机文件落下载目录", (download_dir / "from_phone.bin").exists(), "未落盘")
        rec = body.get("record") or {}
        check("手机记录方向=上传/desktop_side=incoming",
              rec.get("desktop_side") == "incoming", f"desktop_side={rec.get('desktop_side')}")

        print("== 8. settings 持久化 ==")
        candidates = backend.get_lan_ipv4_candidates()
        if candidates:
            chosen = candidates[0]
            r = client.post("/settings/lan-ip", json={"selected_ip": chosen})
            check("POST /settings/lan-ip -> 200", r.status_code == 200, f"got {r.status_code}")
            persisted = _read()
            check("selected_lan_ip 已持久化", persisted.get("selected_lan_ip") == chosen,
                  f"settings={persisted}")
        else:
            r = client.post("/settings/lan-ip", json={"selected_ip": "127.0.0.1"})
            check("无候选时选择 loopback -> 400（行为正确）", r.status_code == 400, f"got {r.status_code}")

        print("== 9. /records 权限隔离 ==")
        fresh_client = app.test_client()  # 全新 cookie jar，避免携带上一会话
        r = fresh_client.get("/records", environ_base={"REMOTE_ADDR": "192.168.1.99"})
        check("未授权手机 /records -> 401", r.status_code == 401, f"got {r.status_code}")
    finally:
        backend._read_settings_unlocked = real_read
        backend._write_settings_unlocked = real_write

    print(f"\n结果: {PASS} PASS, {FAIL} FAIL")
    if FAILURES:
        print("失败项:")
        for item in FAILURES:
            print(f"  - {item}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())



