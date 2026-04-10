# LAN File Transfer 异常排查记录

记录时间：2026-03-23

## 问题现象

- 局域网内两台电脑互传文件存在不稳定/失败情况。
- 可能出现：A -> B 成功，但 B -> A 失败；或随机失败、超时、文件不完整。

## 当前代码排查结论（静态分析）

### 1) 局域网 IP 选择可能错误（高优先级）

- 位置：`app.py:33-41`, `app.py:2433-2437`, `app.py:1525`, `app.py:1657`
- 说明：
  - `get_lan_ip()` 通过连接 `8.8.8.8` 推断本机 IP。
  - 多网卡 / VPN / 无外网时，可能选错网卡 IP 或回退为 `127.0.0.1`。
  - 影响配对回调地址，导致双向传输不对称。

### 2) 设备发现仅用全局广播（高优先级）

- 位置：`app.py:1144-1211`（特别是 `255.255.255.255` 广播发送）
- 说明：
  - 某些网络环境会丢弃全局广播，导致发现单向可见。

### 3) 对端健康检查超时过短（中高优先级）

- 位置：`app.py:697-703`
- 说明：
  - connect/read timeout 过小（`0.35/0.6` 秒），在轻微网络抖动下误判离线。

### 4) 中继重试时文件流复位不可靠（中高优先级）

- 位置：`app.py:833-839`, `app.py:848-853`
- 说明：
  - 重试时尝试 `seek(0)`，失败被静默忽略。
  - 可能导致第二次请求发送空流/截断流。

### 5) 同名文件分配存在并发竞争（中优先级）

- 位置：`app.py:204-213`
- 说明：
  - `allocate_unique_file_path()` 非原子检查，多个并发上传同名文件时可能冲突覆盖。

### 6) 临时文件清理路径不一致（中优先级）

- 位置：`app.py:2229-2265`, `app.py:2325-2331`, `README.md:14`
- 说明：
  - 某些下载路径不会触发清理，可能造成 `transient_uploads` 堆积，长期影响稳定性。

## 当前日志能力评估

- 关键流程日志不足，且存在多个静默 `pass`，排障信息不够。
- 位置示例：`app.py:2431`, `app.py:2440-2442`, `app.py:2332-2338`。

## 可复现场景与验证命令（明天可直接执行）

### A. 检查双方设备发现是否对称

```powershell
Invoke-RestMethod http://127.0.0.1:5000/peers/discovered | ConvertTo-Json -Depth 6
Invoke-RestMethod http://127.0.0.1:5000/peers/paired | ConvertTo-Json -Depth 6
```

### B. 检查本机对外公布地址是否可达

```powershell
Invoke-RestMethod http://127.0.0.1:5000/auth/mobile-token | Select-Object -ExpandProperty mobile_url
Test-NetConnection <mobile_url中的IP> -Port 5000
```

### C. 检查 UDP 发现监听状态

```powershell
Get-NetUDPEndpoint | Where-Object LocalPort -eq 54546
```

### D. 互传后做文件完整性校验

```powershell
Get-FileHash "<源文件路径>" -Algorithm SHA256
Get-FileHash "<接收文件路径>" -Algorithm SHA256
```

### E. 检查临时目录是否持续膨胀

```powershell
Get-ChildItem "G:\leetcode\lan_file_transfer\transient_uploads" -File | Measure-Object Length -Sum
```

## 明日修改建议（按优先级）

1. 修正 LAN IP 选择策略（支持多网卡可选或更稳健探测）。
2. 改进发现机制（补充子网广播/可配置发现方式）。
3. 放宽健康检查超时并增加重试策略。
4. 修复中继重试流复位逻辑，避免 silent ignore。
5. 为同名写入增加原子化保护（或加锁）。
6. 统一临时文件生命周期与清理策略。
7. 为配对/发现/中继关键路径补充结构化日志。

