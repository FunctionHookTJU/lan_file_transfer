# 更新日志

日期：2026-03-01

## 分设备持久化传输记录（PyInstaller 单 EXE）

- 新增基于 `sqlite3` 的持久化历史库 `history.db`，存储路径优先为 `%APPDATA%\LANFileTransfer`，若不可用则回退到 `sys.executable` 同目录，并显式避开 `sys._MEIPASS` 临时目录。
- 程序启动时自动初始化数据库表 `transfer_history`（含 `id`、`device_id`、`device_name`、`file_name`、`file_path`、`direction`、`timestamp`、`status` 等核心字段），无需额外依赖即可运行。
- 手机端前端新增 `localStorage` 持久化 `device_id`（UUID）与设备名标识，后续 API 与 WebSocket 请求自动携带，供后端进行设备隔离。
- 历史记录权限调整：电脑端可查看全部设备传输记录；手机端仅能查看并接收属于本 `device_id` 的历史数据与实时推送。
- 电脑端新增记录右键“打开文件所在文件夹”能力（后端新增 `POST /records/<id>/open-folder`）。
- 历史列表展示补充了方向、设备名、状态信息；仅对当前可用文件显示下载按钮，避免无效操作。
- 优化电脑端上传体验：桌面模式改为优先使用“原始文件路径”上传（新增 `/upload-desktop-path`），不再默认复制到 `transient_uploads` 产生重复占用；右键打开目录将定位到原始文件所在文件夹。
- 修复保存路径体验：手机上传到电脑时，文件会直接落到当前配置的下载目录；当文件已在该目录时，点击“下载”不再重复复制。
- 新增快捷操作：电脑端左键点击聊天记录中的文件名可直接打开文件（新增 `POST /records/<id>/open-file`）。

日期：2026-02-26

## 构建与发布

- 新增一键构建脚本：`build/build_all.ps1`、`build/build_all.bat`，可连续构建 EXE 与安装包。
- 安装包支持版本号命名：`LANFileTransfer-v<版本号>-Setup.exe`。
- `build/build_exe.ps1` 改为使用 `LANFileTransfer.spec` 构建，确保图标与资源一致。

## 安全与可靠性改进

- 增加上传大小限制（服务端强校验）：默认 `10GB`。
- 支持桌面端通过前端动态修改上传上限（范围 `1GB~100GB`），并提供后端设置接口。
- 会话安全增强：
  - Cookie 改为 `HttpOnly`。
  - 增加会话 TTL 与过期清理逻辑。
  - HTTP 接口不再通过 query 参数传递 `session_id`（仅保留 Header/Cookie）。
- 托盘模式可靠性修复：后端启动改为严格端口模式，避免端口漂移导致托盘健康检查异常。

## 交互与界面

- 新增“设置”按钮，可展开/收起设置面板。
- 上传限制已收纳到设置面板，便于后续扩展其他设置项。

## 功能调整

- 已移除“保密口令加密传输”相关功能与界面入口，避免在 HTTP/非安全上下文下出现加密失败。
- 文档同步更新（README）。
