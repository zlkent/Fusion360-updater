# Changelog

## v0.1.0

### 中文

- 首个公开版本。
- 增加 Fusion 360 更新器图形界面。
- 支持检查 Autodesk live manifest 最新版本。
- 支持查询本机 official 安装状态。
- 支持通过 Autodesk `streamer.exe` 执行更新、自动重试和 official query 验证。
- 默认使用 `--quiet`，减少 Autodesk Application Installer 弹窗。
- 支持代理模式切换、陈旧 `registry_track` 锁恢复、卡住的 cleanup 进程处理。
- 支持 IDM 下载辅助，解析真实 `.tar.xz` 包 URL 并加入 IDM 队列。
- 增加本地包缓存辅助，支持缓存检查、补齐、JSON 清单导出和本地 HTTP 缓存端点。
- 增加 GitHub Actions 自动编译和 tag 发布流程。

### English

- Initial public release.
- Added the Fusion 360 updater desktop UI.
- Added latest-version checks from Autodesk's live manifest.
- Added official local installation query support.
- Added update execution through Autodesk `streamer.exe`, automatic retries, and official query verification.
- Enabled `--quiet` by default to reduce Autodesk Application Installer popups.
- Added proxy mode selection, stale `registry_track` recovery, and stuck cleanup handling.
- Added IDM download assistance that resolves real `.tar.xz` package URLs and queues them in IDM.
- Added local package cache assistance with cache checks, cache filling, JSON manifest export, and a local HTTP cache endpoint.
- Added GitHub Actions build and tag-based release publishing.
