# LAN 文件传输系统（Flask）

无需安装 App，手机扫码后用浏览器即可与电脑互传文件。

## 功能

- 服务器启动后自动检测局域网 IP，并在控制台打印二维码（指向 `http://IP:PORT`）。
- 手机端网页：聊天风格记录区 + 底部“发送文件”按钮。
- 使用 `fetch` + `FormData` 实现无刷新上传。
- 电脑端收到文件后自动保存到本地目录（默认 `received_files/`）。
- 电脑端网页支持拖拽上传文件，手机端会弹出下载提示。
- 前端监听 `visibilitychange`，并在后台尝试使用 Notification API；同时保持 WebSocket 心跳。

## 运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py --port 5000
```

启动后控制台会输出：

- 保存目录
- 可访问地址（例如 `http://192.168.1.8:5000`）
- 终端二维码（手机扫码即可）

## 可选参数

```bash
python app.py --port 5000 --save-dir received_files
```

## 使用说明

- 手机：扫码打开网页，点击“发送文件”上传到电脑。
- 电脑：同一地址打开网页（可加 `?role=desktop`），支持拖拽文件到页面，手机会收到下载提示。

## 大文件内存占用处理

后端上传接口没有把整个文件一次性读入内存，而是按块写盘：

- 从 `uploaded.stream` 以 `1MB` chunk 循环读取。
- 每读一块就直接写入目标文件。
- 这样内存峰值近似为单个 chunk 大小 + 框架少量开销，而不是文件总大小。

下载接口使用 `send_file(..., conditional=True)`，让服务器以文件流形式返回，并支持分段请求，避免大文件下载造成额外内存压力。
