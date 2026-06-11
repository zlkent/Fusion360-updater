# Fusion 360 Updater

[中文说明](#中文说明) | [English](#english)

## 中文说明

Fusion 360 Updater 是一个 Windows 桌面工具，用来绕过 Autodesk Fusion 360 内置更新器在代理/网络环境下经常出现的失败。它复用 Autodesk 官方 `streamer.exe` 做最终安装和验证，但提供更清楚的 UI、代理配置、自动重试、锁文件恢复和 IDM 下载辅助。

### 主要功能

- 检查 Autodesk live manifest 的最新 Fusion 360 版本。
- 查询本机 official 安装状态。
- 使用 Autodesk `streamer.exe` 执行更新。
- 默认使用 `--quiet`，避免弹出 Autodesk Application Installer 窗口。
- 支持代理模式切换：
  - 使用系统/当前环境
  - 不使用代理
  - 指定代理，例如 `http://127.0.0.1:8001`
- 自动处理常见问题：
  - `SSL: UNEXPECTED_EOF_WHILE_READING`
  - `WinError 10060`
  - `registry_track` 陈旧锁
  - 长时间停滞的 `streamer.exe --cleanup -p uninstall`
- 更新完成后再次执行 query，并检查 `Fusion360.exe` 文件版本。
- 支持 IDM 下载辅助：
  - 读取 full manifest
  - 逐个解析 `packages/<package-id>.json`
  - 提取真实 `packages/<archive-id>.tar.xz`
  - 批量加入 IDM 队列

### 运行

直接运行已打包程序：

```powershell
.\dist\Fusion360Updater.exe
```

或用 Python 运行源码：

```powershell
python .\fusion360_updater_gui.py
```

### 打包

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

打包产物：

```text
dist\Fusion360Updater.exe
```

### 默认配置

- Fusion app id: `73e72ada57b7480280f7a6f4a289729f`
- Service: `production`
- 默认代理: `http://127.0.0.1:8001`
- 默认更新参数: `--full-deploy --no_cleanup --threadscount 1`
- 默认日志目录: `%LOCALAPPDATA%\Fusion360Updater\logs`

配置文件会优先写入程序同目录的 `fusion360_updater_config.json`。如果程序目录不可写，会写入：

```text
%LOCALAPPDATA%\Fusion360Updater\fusion360_updater_config.json
```

### 验收口径

工具不会把“下载完成”或“候选目录存在”当作正式完成。只有同时满足以下条件，才显示为完成：

- updater 日志包含 `Configure app complete Update to ...` 或已经是当前版本。
- query 日志包含 `Configure app complete Query to ...`。
- query 找到的 `Fusion360.exe` 存在。
- `Fusion360.exe` 文件版本与 query 版本一致。

### 关于离线包和 IDM

IDM 下载的 `.tar.xz` 文件目前是下载辅助和排查工具，不是 Autodesk 官方离线安装源。Autodesk `streamer.exe` 没有稳定公开的参数可以指定从本地目录读取这些包。

如果未来需要“先由 IDM 下载，再由 updater 从本地包更新”，需要新增本地缓存代理：

1. IDM 把 `.tar.xz` 下载到固定目录。
2. 更新器启动本地 HTTP 代理。
3. `streamer.exe` 请求 Autodesk package URL 时，代理优先返回本地缓存。
4. 缓存缺失时再转发到 Autodesk。

当前版本仍以 Autodesk `streamer.exe` 的 official query 结果为最终真值。

## English

Fusion 360 Updater is a Windows desktop utility for repairing and running Autodesk Fusion 360 updates in proxy-sensitive or unstable network environments. It still uses Autodesk's official `streamer.exe` for the actual install and verification, but adds a clearer UI, proxy selection, automatic retry, lock recovery, and IDM download assistance.

### Features

- Check the latest Fusion 360 version from Autodesk's live manifest.
- Query the official local installation state.
- Run updates through Autodesk `streamer.exe`.
- Use `--quiet` by default to avoid Autodesk Application Installer popups.
- Proxy modes:
  - system/current environment
  - no proxy
  - custom proxy, for example `http://127.0.0.1:8001`
- Automatic recovery for common failures:
  - `SSL: UNEXPECTED_EOF_WHILE_READING`
  - `WinError 10060`
  - stale `registry_track`
  - stuck `streamer.exe --cleanup -p uninstall`
- Re-query after update and verify the `Fusion360.exe` file version.
- IDM download assistance:
  - read the full manifest
  - parse each `packages/<package-id>.json`
  - extract real `packages/<archive-id>.tar.xz` URLs
  - add them to the IDM queue

### Run

Run the packaged app:

```powershell
.\dist\Fusion360Updater.exe
```

Or run from source:

```powershell
python .\fusion360_updater_gui.py
```

### Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

Output:

```text
dist\Fusion360Updater.exe
```

### Defaults

- Fusion app id: `73e72ada57b7480280f7a6f4a289729f`
- Service: `production`
- Default proxy: `http://127.0.0.1:8001`
- Default update flags: `--full-deploy --no_cleanup --threadscount 1`
- Default log directory: `%LOCALAPPDATA%\Fusion360Updater\logs`

The config file is written next to the executable when possible:

```text
fusion360_updater_config.json
```

If that directory is not writable, it is written to:

```text
%LOCALAPPDATA%\Fusion360Updater\fusion360_updater_config.json
```

### Completion Criteria

The app does not treat a downloaded file or a candidate directory as official completion. Completion requires:

- updater log contains `Configure app complete Update to ...`, or the app is already at the current version.
- query log contains `Configure app complete Query to ...`.
- the queried `Fusion360.exe` exists.
- the `Fusion360.exe` file version matches the queried version.

### Offline Packages and IDM

The `.tar.xz` files downloaded by IDM are currently diagnostic/download-assist artifacts. They are not used as an Autodesk-supported offline install source. Autodesk `streamer.exe` does not expose a stable public option for installing directly from a local package directory.

To support true “download with IDM first, then update from local packages”, a local cache proxy would be needed:

1. IDM downloads `.tar.xz` files to a fixed cache directory.
2. The updater starts a local HTTP proxy.
3. When `streamer.exe` requests Autodesk package URLs, the proxy serves local cached files first.
4. Missing files are forwarded to Autodesk.

For now, Autodesk `streamer.exe` query output remains the official source of truth.

