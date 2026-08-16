# 更新日志

日期：2026-08-16

## 修复手机扫码无法进入（v1.9.3）

- **根因**：`get_lan_ipv4_candidates()` 把所有来源 IP 混在一起按字符串排序，Hyper-V/WSL 虚拟网卡（如 `172.21.x`、`172.30.x`）会排在真实局域网 IP（如 `192.168.1.x`）前面，导致二维码指向手机不可达的虚拟网卡地址。
- **修复**：默认路由出口（UDP connect 成功）的 IP 优先，其次才是主机名解析到的其他 IP——base_url/二维码重新指向真实上网网卡。
- **Origin 校验增强**：WebSocket 与写请求的 Origin 校验增加"同源兜底"——Origin 与请求 Host 一致即放行（覆盖任意 IP/端口/代理/转发下的正常访问），跨站恶意页面仍被拒绝。

日期：2026-08-16

## 移除安装器构建功能

- 删除 Inno Setup 安装器构建：`build/build_installer.ps1` 与 `installer/lan_file_transfer.iss` 不再提供，安装器产物（`-Setup.exe`）不再构建。
- `build/build_all.ps1` 一键构建仅产出 `dist\LANFileTransfer.exe` 与 `dist\LANFileTransfer-v<版本号>.exe`。
- README 已同步：仅保留"打包 EXE"与"一键构建"两个方案。
- 分发方式：直接分发单个托盘 EXE（`LANFileTransfer.exe`）。

日期：2026-08-16

## 安全加固与稳定性修复（v1.9.2）

- **跨站防护（CSRF/CSWSH）**：新增 Origin 白名单——所有写请求与 WebSocket 升级请求在握手前校验 Origin，非本机页面发起的请求直接 403；WS 处理器内二次校验。
- **`/peer/upload` 来源校验**：接收端校验请求方 IP 必须与配对记录/最近发现记录一致，拒绝伪造 `X-Peer-Device-Id` 推送文件；移除可按设备名顶替配对条目的迁移逻辑。
- **配对 auto-accept 防劫持**：`/pairing/request` 仅当请求来源 IP 与配对记录/发现记录一致时才自动接受，否则转入人工确认流程（设备 ID 经 UDP/mDNS 公开，仅凭 ID 不能证明身份）。
- **修复 LAN IP 手动选择失效**：`start_server` 启动时应用用户保存的 `selected_lan_ip`（此前接口有保存但启动路径未消费）；设置面板新增“局域网 IP”下拉选择器（重启生效）。
- **修复瞬态文件清理根因**：`send_file` 返回的响应为 `direct_passthrough` 模式，`call_on_close` 回调（下载后清理瞬态文件/标记已下载）此前从未执行；现通过 `attach_response_close_hooks` 包装 `ClosingIterator` 保证响应结束后触发。
- **修复 `/upload` 瞬态路径死代码**：唯一前缀名（时间戳+ID+原名）此前被覆盖未生效，现按前缀落盘，避免与真实下载目录重名混淆。
- **内存有界**：`records`/`record_map` 缓存上限 1500 条，超出自动淘汰最旧；下载/保存接口支持从历史数据库回退，长运行不再无界增长。
- **发现线程解耦**：TCP 探活移入独立线程，UDP 广播循环不再被健康检查阻塞；启动邻居扫描改为单次快速探测（0.6s/1.0s）。
- **数据库与配置可靠性**：历史库启用 WAL + busy_timeout，降低并发 "database is locked" 概率；`settings.json` 读-改-写加锁，避免并发持久化互相覆盖。
- **下载状态语义修正**：`/files/<id>` 在响应真正完成后才标记“已下载”，中断/失败的下载不再记为成功。
- 新增回归测试：`tools/smoke_test.py`（35 项断言，覆盖鉴权、Origin 防护、配对校验、瞬态清理、会话上传等）。

日期：2026-06-06

## 局域网发现可靠性 & 速度优化（v1.9.1）

- 子网掩码真值检测：通过 iphlpapi（Win）/ ioctl（Linux）获取实际子网广播地址，替代硬编码 /24 推导，非标准子网也能正确广播。
- mDNS/Zeroconf 并行发现：UDP 广播被阻断时仍可通过 mDNS 互相发现。
- 并行端口探测：`find_reachable_paired_peer` 内 60+ 端口改为 ThreadPoolExecutor 分批并行探活（batch=10, max_workers=8），可达性检测提速 3-5x。
- 启动即时发现：启动时立即发送 UDP 广播 + TCP 邻居扫描（本机 ±15 IP 范围），秒级初始发现。
- 中继重试流校验 fail-fast：重试时如文件大小不匹配直接返回错误，不再静默跳过。

日期：2026-03-26

## 调试模式与构建选项

- 新增托盘启动参数 `--debug`：开启后跳过单实例互斥检查，允许调试时同时运行多个实例；默认模式仍保持单实例保护。
- 打包脚本新增 `.\build\build_exe.ps1 -DebugBuild` 选项：通过环境变量驱动 `LANFileTransfer.spec` 的 `EXE(debug=...)`，可按需构建 PyInstaller 调试版；默认打包行为保持不变。
- README 已同步补充托盘 `--debug` 与打包 `-DebugBuild` 的使用示例。

日期：2026-03-15

## 移动端拍照上传（v1.8.0）

- 手机端底部操作区新增“拍照上传”入口，支持调起系统相机直接拍照后上传。
- 新增移动端专用拍照文件输入（`accept="image/*"` + `capture="environment"`），上传流程复用现有 `/upload` 接口与进度条逻辑。
- 电脑端界面保持原有上传体验不变：继续使用“发送文件”与拖拽上传，拍照入口默认隐藏。
- 访问未授权状态下，同步禁用“拍照上传”按钮，保持与“发送文件”一致的安全行为。
- 构建与安装默认版本号更新为 `1.8.0`（安装器脚本与构建脚本）。

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
