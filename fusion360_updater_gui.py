import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox, ttk
import tkinter as tk


APP_ID = "73e72ada57b7480280f7a6f4a289729f"
APP_STREAM = "67316f5e79bc48318aa5f7b6bb58243d"
SERVICE = "production"
MANIFEST_URL = (
    f"https://dl.appstreaming.autodesk.com/{SERVICE}/{APP_STREAM}/{APP_ID}/full.json"
)
DEFAULT_PROXY = "http://127.0.0.1:8001"
META_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "webdeploy" / "meta"
PRODUCTION_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Autodesk" / "webdeploy" / "production"
REGISTRY_TRACK = META_DIR / "registry_track"
PACKAGE_ID_RE = re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE)
IDM_PATHS = (
    Path(r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe"),
    Path(r"C:\Program Files\Internet Download Manager\IDMan.exe"),
)


def runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_state_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    path = base / "Fusion360Updater"
    path.mkdir(parents=True, exist_ok=True)
    return path


def writable_config_path() -> Path:
    local_path = runtime_dir() / "fusion360_updater_config.json"
    try:
        if local_path.exists():
            return local_path
        probe = runtime_dir() / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return local_path
    except OSError:
        return user_state_dir() / "fusion360_updater_config.json"


def default_log_dir() -> Path:
    path = user_state_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class CheckResult:
    ok: bool
    message: str
    version: str = ""
    launch_exe: str = ""
    log_path: str = ""


class FusionUpdaterCore:
    def __init__(self, log_func, stop_event: threading.Event):
        self.log = log_func
        self.stop_event = stop_event

    def find_streamer(self) -> str:
        root = META_DIR / "streamer"
        if not root.exists():
            return ""
        streamers = sorted(root.rglob("streamer.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(streamers[0]) if streamers else ""

    def proxy_env(self, mode: str, proxy_url: str) -> dict:
        env = os.environ.copy()
        if mode == "custom":
            proxy = proxy_url.strip()
            if proxy:
                env["HTTP_PROXY"] = proxy
                env["HTTPS_PROXY"] = proxy
                env["ALL_PROXY"] = proxy
        elif mode == "none":
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env.pop(key, None)
        return env

    def urllib_opener(self, mode: str, proxy_url: str):
        if mode == "custom" and proxy_url.strip():
            proxy = proxy_url.strip()
            return urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        if mode == "none":
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    def fetch_json(self, url: str, mode: str, proxy_url: str, timeout: int = 45) -> dict:
        opener = self.urllib_opener(mode, proxy_url)
        req = urllib.request.Request(url, headers={"User-Agent": "Fusion360Updater/1.0"})
        last_error = None
        for attempt in range(1, 3):
            try:
                with opener.open(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", errors="replace"))
            except Exception as exc:
                last_error = exc
                self.log(f"urllib 读取失败({attempt}/2): {exc}")
                time.sleep(1)
        self.log("切换到 curl.exe 读取 manifest。")
        text = self.curl_capture(url, mode, proxy_url, head=False, timeout=90)
        if not text:
            raise urllib.error.URLError(f"manifest 读取失败: {last_error}")
        return json.loads(text)

    def head_url(self, url: str, mode: str, proxy_url: str, timeout: int = 30) -> tuple[bool, str]:
        opener = self.urllib_opener(mode, proxy_url)
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Fusion360Updater/1.0"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                length = resp.headers.get("Content-Length", "unknown")
                return True, f"HTTP {resp.status}, Content-Length={length}"
        except Exception as exc:
            self.log(f"urllib HEAD 失败，切换 curl.exe: {exc}")
        try:
            out = self.curl_capture(url, mode, proxy_url, head=True, timeout=60)
            statuses = re.findall(r"HTTP/\S+\s+(\d+)", out)
            status = statuses[-1] if statuses else "unknown"
            length_match = re.search(r"(?im)^Content-Length:\s*(\S+)", out)
            length = length_match.group(1) if length_match else "unknown"
            ok = status.startswith("2") or status.startswith("3")
            return ok, f"curl HTTP {status}, Content-Length={length}"
        except Exception as exc:
            return False, str(exc)

    def curl_args(self, url: str, mode: str, proxy_url: str, head: bool, timeout: int) -> list[str]:
        args = [
            "curl.exe",
            "-L",
            "--connect-timeout",
            "20",
            "--max-time",
            str(timeout),
        ]
        if self.curl_supports_retry_all_errors():
            args.append("--retry-all-errors")
        args += ["--retry", "3", "--retry-delay", "2"]
        if head:
            args.append("-I")
        else:
            args += ["--fail", "--silent", "--show-error"]
        if mode == "custom" and proxy_url.strip():
            args += ["-x", proxy_url.strip()]
        elif mode == "none":
            args += ["--noproxy", "*"]
        args.append(url)
        return args

    def curl_supports_retry_all_errors(self) -> bool:
        try:
            completed = subprocess.run(
                ["curl.exe", "--help", "all"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=10,
            )
            return "--retry-all-errors" in completed.stdout
        except Exception:
            return False

    def curl_capture(self, url: str, mode: str, proxy_url: str, head: bool, timeout: int) -> str:
        last_output = ""
        for attempt in range(1, 4):
            completed = subprocess.run(
                self.curl_args(url, mode, proxy_url, head=head, timeout=timeout),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            output = completed.stdout + ("\n" + completed.stderr if completed.stderr else "")
            if completed.returncode == 0:
                return output
            last_output = output.strip() or f"curl 退出码 {completed.returncode}"
            self.log(f"curl 失败({attempt}/3): {last_output}")
            time.sleep(2)
        raise urllib.error.URLError(last_output)

    def get_latest_manifest(self, mode: str, proxy_url: str) -> tuple[str, str, dict]:
        manifest = self.fetch_json(MANIFEST_URL, mode, proxy_url)
        version = str(manifest.get("build-version", ""))
        release = str(manifest.get("release-version", ""))
        return version, release, manifest

    def running_streamer_pids(self) -> list[int]:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq streamer.exe", "/FO", "CSV", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL,
                encoding="mbcs",
                errors="ignore",
            )
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            parts = [part.strip('"') for part in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == "streamer.exe":
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return pids

    def streamer_processes(self) -> list[dict]:
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name = 'streamer.exe'\" | "
            "ForEach-Object { [pscustomobject]@{"
            "ProcessId=$_.ProcessId;"
            "CreationDate=$_.CreationDate.ToString('o');"
            "CommandLine=$_.CommandLine"
            "} } | ConvertTo-Json -Compress"
        )
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return []
        if not out:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else [data]

    def cleanup_track_active(self) -> bool:
        if not REGISTRY_TRACK.exists():
            return False
        try:
            return "doing cleanup" in REGISTRY_TRACK.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            return False

    def terminate_stuck_cleanup(self, min_age_seconds: int = 300) -> bool:
        if not self.cleanup_track_active():
            return False
        now = datetime.now(timezone.utc)
        killed = False
        for proc in self.streamer_processes():
            command = str(proc.get("CommandLine") or "").lower()
            if "--cleanup" not in command or "-p uninstall" not in command:
                continue
            try:
                created = datetime.fromisoformat(str(proc.get("CreationDate")).replace("Z", "+00:00"))
            except ValueError:
                created = now
            age = (now - created.astimezone(timezone.utc)).total_seconds()
            if age < min_age_seconds:
                self.log(f"发现 cleanup 进程但仍在宽限期内: PID={proc.get('ProcessId')}, age={int(age)}s")
                continue
            pid = int(proc.get("ProcessId"))
            self.log(f"终止长时间停滞的 cleanup 进程: PID={pid}, age={int(age)}s")
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    timeout=15,
                )
                killed = True
            except Exception as exc:
                self.log(f"终止 cleanup 进程失败: {exc}")
        if killed:
            time.sleep(2)
        return killed

    def backup_registry_track(self, reason: str, only_if_stale: bool = False) -> str:
        if not REGISTRY_TRACK.exists():
            return ""
        if only_if_stale and self.running_streamer_pids():
            return ""
        backup = REGISTRY_TRACK.with_name(f"registry_track.bak-{reason}-{now_stamp()}")
        shutil.move(str(REGISTRY_TRACK), str(backup))
        return str(backup)

    def run_streamer(self, streamer: str, args: list[str], env: dict, log_path: Path) -> int:
        cmd = [streamer]
        if "--quiet" not in args:
            cmd.append("--quiet")
        cmd += args + ["-v", "DEBUG", "-f", str(log_path)]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.log(f"进程已启动 PID={proc.pid}")
        last_size = 0
        last_change = time.monotonic()
        saw_error = False
        while proc.poll() is None:
            if self.stop_event.is_set():
                self.log("收到停止请求，正在终止 updater 进程...")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return 130
            last_size, changed, error_in_tail = self.tail_log(log_path, last_size)
            if changed:
                last_change = time.monotonic()
            saw_error = saw_error or error_in_tail
            if saw_error and time.monotonic() - last_change > 45:
                self.log("检测到错误后日志停滞，终止本轮并准备自动重试。")
                proc.kill()
                return 75
            time.sleep(1.0)
        self.tail_log(log_path, last_size)
        return proc.returncode

    def tail_log(self, path: Path, last_size: int) -> tuple[int, bool, bool]:
        if not path.exists():
            return last_size, False, False
        size = path.stat().st_size
        if size < last_size:
            last_size = 0
        if size == last_size:
            return last_size, False, False
        error_in_tail = False
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(last_size)
            text = fh.read()
        for line in text.splitlines()[-25:]:
            if self.is_interesting_log_line(line):
                self.log(self.clean_log_line(line))
            if self.is_real_error(line):
                error_in_tail = True
        return size, True, error_in_tail

    def is_interesting_log_line(self, line: str) -> bool:
        markers = (
            "needs to upgrade",
            "Currently installed version",
            "already installed",
            "No apps need",
            "Opening manifest",
            "Downloading archive",
            "package installation complete",
            "Executing special action",
            "FULL_CHECKSUM_VALIDATION",
            "Configure app complete",
            "Process Complete",
            "Found launch executable",
            "UNEXPECTED_EOF",
            "WinError",
            " - ERROR ::",
            " - CRITICAL ::",
            "registry is locked",
        )
        return any(marker in line for marker in markers)

    def is_real_error(self, line: str) -> bool:
        markers = (" - ERROR ::", " - CRITICAL ::", "UNEXPECTED_EOF", "registry is locked")
        return any(marker in line for marker in markers)

    def clean_log_line(self, line: str) -> str:
        parts = line.split(" :: ", 1)
        return parts[1] if len(parts) == 2 else line

    def parse_log(self, path: Path, operation: str) -> CheckResult:
        if not path.exists():
            return CheckResult(False, "日志文件不存在", log_path=str(path))
        text = path.read_text(encoding="utf-8", errors="replace")
        real_errors = [
            line for line in text.splitlines()
            if self.is_real_error(line) or "Traceback" in line or "SSLNetworkFailure" in line
        ]
        query_match = re.search(r"Configure app complete Query to ([^\s]+)", text)
        update_match = re.search(r"Configure app complete Update to ([^\s]+)", text)
        installed_matches = re.findall(
            rf"Currently installed version for app {APP_ID} is ([^\s(]+)",
            text,
        )
        launch_match = re.search(r"Found launch executable:\s*(.+)", text)
        version = ""
        if operation == "query" and query_match:
            version = query_match.group(1)
        elif operation == "update" and update_match:
            version = update_match.group(1)
        launch = launch_match.group(1).strip() if launch_match else ""
        if "Process Complete" in text and (query_match or update_match):
            return CheckResult(True, "streamer 操作完成", version=version, launch_exe=launch, log_path=str(path))
        if operation == "update" and (
            "is already installed, skip updating" in text
            or "No apps need to install/live update" in text
        ):
            version = installed_matches[-1] if installed_matches else ""
            return CheckResult(True, "已是当前版本，无需更新", version=version, launch_exe=launch, log_path=str(path))
        if real_errors:
            return CheckResult(False, real_errors[-1], log_path=str(path))
        return CheckResult(False, "未找到完成标记", log_path=str(path))

    def run_query(self, streamer: str, mode: str, proxy_url: str, log_dir: Path) -> CheckResult:
        env = self.proxy_env(mode, proxy_url)
        args = ["-p", "query", "-o", "single", "-a", APP_ID, "-s", SERVICE]
        stale = self.backup_registry_track("query-stale-preflight", only_if_stale=True)
        if stale:
            self.log(f"query 前已移走陈旧锁文件: {stale}")
        last_result = CheckResult(False, "尚未执行")
        for attempt in range(1, 4):
            log_path = log_dir / f"fusion_query_{now_stamp()}_{attempt}.log"
            code = self.run_streamer(streamer, args, env, log_path)
            result = self.parse_log(log_path, "query")
            last_result = result
            if code != 0 and not result.ok:
                result.message = f"query 退出码 {code}: {result.message}"
            if result.ok and result.launch_exe:
                exe_path = result.launch_exe.replace("\\\\?\\", "")
                if Path(exe_path).exists():
                    result.launch_exe = exe_path
                return result
            if "registry is locked" not in result.message and "registry is locked" not in str(result.message).lower():
                return result
            self.log(f"query 遇到 registry locked，第 {attempt}/3 次。")
            if self.running_streamer_pids():
                if not self.terminate_stuck_cleanup():
                    return result
            backup = self.backup_registry_track(f"query-locked-{attempt}", only_if_stale=False)
            if backup:
                self.log(f"已备份锁文件并准备重试: {backup}")
            time.sleep(3)
        return last_result

    def run_update_with_retries(
        self,
        streamer: str,
        mode: str,
        proxy_url: str,
        log_dir: Path,
        max_retries: int,
        thread_count: int,
        full_deploy: bool,
        no_cleanup: bool,
    ) -> CheckResult:
        stale = self.backup_registry_track("stale-preflight", only_if_stale=True)
        if stale:
            self.log(f"已移走疑似陈旧锁文件: {stale}")
        env = self.proxy_env(mode, proxy_url)
        last_result = CheckResult(False, "尚未执行")
        for attempt in range(1, max_retries + 1):
            if self.stop_event.is_set():
                return CheckResult(False, "用户已停止")
            self.log(f"开始第 {attempt}/{max_retries} 次更新尝试")
            log_path = log_dir / f"fusion_update_attempt{attempt}_{now_stamp()}.log"
            args = ["-p", "update", "-o", "single", "-a", APP_ID, "-s", SERVICE]
            if full_deploy:
                args.append("--full-deploy")
            if no_cleanup:
                args.append("--no_cleanup")
            args += ["--threadscount", str(max(1, thread_count))]
            code = self.run_streamer(streamer, args, env, log_path)
            result = self.parse_log(log_path, "update")
            last_result = result
            if result.ok:
                self.log(f"更新器完成: {result.version}")
                return result
            self.log(f"本轮未完成，退出码 {code}: {result.message}")
            if attempt < max_retries:
                backup = self.backup_registry_track(f"retry{attempt}", only_if_stale=False)
                if backup:
                    self.log(f"已备份 registry_track: {backup}")
                self.log("准备断点重试...")
                time.sleep(3)
        return last_result

    def verify_exe_version(self, exe_path: str) -> str:
        path = Path(exe_path)
        if not path.exists():
            return ""
        script = (
            "param([string]$p);"
            "$v=(Get-Item -LiteralPath $p).VersionInfo;"
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::UTF8;"
            "if($v.ProductVersion){$v.ProductVersion}else{$v.FileVersion}"
        )
        try:
            return subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", f"& {{ {script} }}", str(path)],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            return ""

    def build_package_downloads(self, mode: str, proxy_url: str) -> tuple[str, str, list[tuple[str, str, int]]]:
        version, release, manifest = self.get_latest_manifest(mode, proxy_url)
        downloads = []
        seen = set()
        packages = manifest.get("packages", [])
        for index, pkg in enumerate(packages, start=1):
            package_id = pkg.get("checksum")
            if not package_id:
                continue
            self.log(f"解析 package manifest {index}/{len(packages)}: {package_id}")
            package_url = f"https://dl.appstreaming.autodesk.com/{SERVICE}/packages/{package_id}.json"
            package_manifest = self.fetch_json(package_url, mode, proxy_url)
            archive_ids = []
            for key in ("non-patched", "patched"):
                value = package_manifest.get(key)
                if isinstance(value, list):
                    archive_ids.extend(str(item) for item in value if item)
                elif isinstance(value, str):
                    archive_ids.append(value)
            content_size = int(
                (package_manifest.get("properties") or {}).get("content-size")
                or pkg.get("compressed-size")
                or 0
            )
            for archive_id in archive_ids:
                if archive_id in seen:
                    continue
                seen.add(archive_id)
                filename = f"{archive_id}.tar.xz"
                url = f"https://dl.appstreaming.autodesk.com/{SERVICE}/packages/{filename}"
                downloads.append((url, filename, content_size if len(archive_ids) == 1 else 0))
        return version, release, downloads

    def export_download_plan(self, mode: str, proxy_url: str, destination: Path) -> Path:
        version, release, downloads = self.build_package_downloads(mode, proxy_url)
        destination.mkdir(parents=True, exist_ok=True)
        out = destination / f"fusion_idm_downloads_{version or 'unknown'}_{now_stamp()}.txt"
        total_size = sum(item[2] for item in downloads)
        lines = [
            f"Fusion 360 manifest: {MANIFEST_URL}",
            f"build-version: {version}",
            f"release-version: {release}",
            f"package-count: {len(downloads)}",
            f"compressed-size-bytes: {total_size}",
            "",
            "Package archive URLs:",
        ]
        lines.extend(url for url, _, _ in downloads)
        out.write_text("\n".join(lines), encoding="utf-8")
        return out

    def find_idm(self, configured_path: str = "") -> str:
        if configured_path.strip() and Path(configured_path.strip()).exists():
            return configured_path.strip()
        for path in IDM_PATHS:
            if path.exists():
                return str(path)
        found = shutil.which("IDMan.exe")
        return found or ""

    def send_manifest_packages_to_idm(
        self,
        mode: str,
        proxy_url: str,
        destination: Path,
        idm_path: str = "",
        start_queue: bool = True,
    ) -> tuple[Path, int]:
        idm = self.find_idm(idm_path)
        if not idm:
            raise FileNotFoundError("没有找到 IDMan.exe")
        version, release, downloads = self.build_package_downloads(mode, proxy_url)
        destination.mkdir(parents=True, exist_ok=True)
        list_path = destination / f"fusion_idm_downloads_{version or 'unknown'}_{now_stamp()}.txt"
        list_path.write_text("\n".join(url for url, _, _ in downloads), encoding="utf-8")
        for index, (url, filename, _) in enumerate(downloads, start=1):
            self.log(f"加入 IDM 队列 {index}/{len(downloads)}: {filename}")
            subprocess.run(
                [idm, "/d", url, "/p", str(destination), "/f", filename, "/a"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                timeout=20,
            )
        if start_queue:
            subprocess.Popen(
                [idm, "/s"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        return list_path, len(downloads)

    def download_url(self, url: str, mode: str, proxy_url: str, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        name = url.rstrip("/").split("/")[-1] or f"download-{now_stamp()}"
        target = destination / name
        opener = self.urllib_opener(mode, proxy_url)
        req = urllib.request.Request(url, headers={"User-Agent": "Fusion360Updater/1.0"})
        try:
            with opener.open(req, timeout=120) as resp, target.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
        except Exception as exc:
            self.log(f"urllib 下载失败，切换 curl.exe: {exc}")
            args = self.curl_args(url, mode, proxy_url, head=False, timeout=300)
            args = args[:-1] + ["-o", str(target), url]
            completed = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode != 0:
                raise urllib.error.URLError((completed.stdout + completed.stderr).strip())
        return target


class FusionUpdaterApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Fusion 360 更新器")
        self.root.geometry("980x720")
        self.stop_event = threading.Event()
        self.messages = queue.Queue()
        self.worker = None
        self.config_path = writable_config_path()
        self.config = self.load_config()
        self.core = FusionUpdaterCore(self.enqueue_log, self.stop_event)

        self.streamer_var = StringVar(value=self.config.get("streamer_path") or self.core.find_streamer())
        self.proxy_mode_var = StringVar(value=self.config.get("proxy_mode", "custom"))
        self.proxy_url_var = StringVar(value=self.config.get("proxy_url", DEFAULT_PROXY))
        self.latest_var = StringVar(value="未检查")
        self.installed_var = StringVar(value="未查询")
        self.official_var = StringVar(value="未验证")
        self.status_var = StringVar(value="就绪")
        self.retries_var = IntVar(value=int(self.config.get("max_retries", 8)))
        self.thread_count_var = IntVar(value=int(self.config.get("thread_count", 1)))
        self.full_deploy_var = BooleanVar(value=bool(self.config.get("full_deploy", True)))
        self.no_cleanup_var = BooleanVar(value=bool(self.config.get("no_cleanup", True)))
        self.log_dir_var = StringVar(value=self.config.get("log_dir") or str(default_log_dir()))
        self.download_url_var = StringVar(value=MANIFEST_URL)
        self.idm_path_var = StringVar(value=self.config.get("idm_path") or self.core.find_idm())

        self.build_ui()
        self.poll_messages()
        self.save_config()

    def load_config(self) -> dict:
        defaults = {
            "streamer_path": "",
            "proxy_mode": "custom",
            "proxy_url": DEFAULT_PROXY,
            "max_retries": 8,
            "thread_count": 1,
            "full_deploy": True,
            "no_cleanup": True,
            "log_dir": str(default_log_dir()),
            "idm_path": "",
        }
        if self.config_path.exists():
            try:
                defaults.update(json.loads(self.config_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return defaults

    def save_config(self):
        data = {
            "streamer_path": self.streamer_var.get().strip(),
            "proxy_mode": self.proxy_mode_var.get(),
            "proxy_url": self.proxy_url_var.get().strip(),
            "max_retries": self.retries_var.get(),
            "thread_count": self.thread_count_var.get(),
            "full_deploy": self.full_deploy_var.get(),
            "no_cleanup": self.no_cleanup_var.get(),
            "log_dir": self.log_dir_var.get().strip(),
            "idm_path": self.idm_path_var.get().strip(),
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def build_ui(self):
        root = ttk.Frame(self.root, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(root)
        top.pack(fill=tk.X)
        ttk.Label(top, text="streamer.exe").grid(row=0, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.streamer_var).grid(row=0, column=1, sticky=tk.EW, pady=4)
        ttk.Button(top, text="自动探测", command=self.detect_streamer).grid(row=0, column=2, padx=4)
        ttk.Button(top, text="选择", command=self.pick_streamer).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="日志目录").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.log_dir_var).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Button(top, text="选择", command=self.pick_log_dir).grid(row=1, column=2, padx=4)
        ttk.Button(top, text="打开", command=self.open_log_dir).grid(row=1, column=3, padx=4)
        top.columnconfigure(1, weight=1)

        proxy = ttk.LabelFrame(root, text="代理")
        proxy.pack(fill=tk.X, pady=(10, 0))
        ttk.Radiobutton(proxy, text="使用系统/当前环境", variable=self.proxy_mode_var, value="system").grid(row=0, column=0, padx=8, pady=8)
        ttk.Radiobutton(proxy, text="不使用代理", variable=self.proxy_mode_var, value="none").grid(row=0, column=1, padx=8, pady=8)
        ttk.Radiobutton(proxy, text="指定代理", variable=self.proxy_mode_var, value="custom").grid(row=0, column=2, padx=8, pady=8)
        ttk.Entry(proxy, textvariable=self.proxy_url_var, width=34).grid(row=0, column=3, padx=8, pady=8)
        ttk.Button(proxy, text="测试 manifest", command=self.test_manifest).grid(row=0, column=4, padx=8, pady=8)
        proxy.columnconfigure(3, weight=1)

        status = ttk.LabelFrame(root, text="状态")
        status.pack(fill=tk.X, pady=(10, 0))
        labels = [
            ("最新版本", self.latest_var),
            ("本机版本", self.installed_var),
            ("official 验证", self.official_var),
            ("当前动作", self.status_var),
        ]
        for idx, (name, var) in enumerate(labels):
            ttk.Label(status, text=name).grid(row=idx // 2, column=(idx % 2) * 2, sticky=tk.W, padx=8, pady=6)
            ttk.Label(status, textvariable=var).grid(row=idx // 2, column=(idx % 2) * 2 + 1, sticky=tk.W, padx=8, pady=6)
        status.columnconfigure(1, weight=1)
        status.columnconfigure(3, weight=1)

        opts = ttk.LabelFrame(root, text="更新策略")
        opts.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(opts, text="最大重试").grid(row=0, column=0, padx=8, pady=8)
        ttk.Spinbox(opts, from_=1, to=30, textvariable=self.retries_var, width=6).grid(row=0, column=1, padx=8)
        ttk.Label(opts, text="下载线程").grid(row=0, column=2, padx=8, pady=8)
        ttk.Spinbox(opts, from_=1, to=8, textvariable=self.thread_count_var, width=6).grid(row=0, column=3, padx=8)
        ttk.Checkbutton(opts, text="full deploy", variable=self.full_deploy_var).grid(row=0, column=4, padx=8)
        ttk.Checkbutton(opts, text="no cleanup", variable=self.no_cleanup_var).grid(row=0, column=5, padx=8)

        actions = ttk.Frame(root)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(actions, text="检查最新", command=self.check_latest).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="查询本机", command=self.query_installed).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="开始自动更新", command=self.start_update).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="停止", command=self.stop_work).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="清理陈旧锁", command=self.clean_stale_lock).pack(side=tk.LEFT, padx=6)

        manual = ttk.LabelFrame(root, text="第三方下载/人工辅助")
        manual.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(manual, text="URL").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        ttk.Entry(manual, textvariable=self.download_url_var).grid(row=0, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(manual, text="下载 URL", command=self.download_url).grid(row=0, column=2, padx=8, pady=8)
        ttk.Button(manual, text="用 IDM 下载清单", command=self.export_plan).grid(row=0, column=3, padx=8, pady=8)
        ttk.Label(manual, text="IDM").grid(row=1, column=0, padx=8, pady=8, sticky=tk.W)
        ttk.Entry(manual, textvariable=self.idm_path_var).grid(row=1, column=1, sticky=tk.EW, padx=8, pady=8)
        ttk.Button(manual, text="探测 IDM", command=self.detect_idm).grid(row=1, column=2, padx=8, pady=8)
        manual.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(root, text="日志")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, height=18)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scroll.set)

        self.append_log(f"配置文件: {self.config_path}")
        self.append_log(f"默认 manifest: {MANIFEST_URL}")

    def enqueue_log(self, text: str):
        self.messages.put(("log", text))

    def set_status(self, text: str):
        self.messages.put(("status", text))

    def poll_messages(self):
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "log":
                    self.append_log(value)
                elif kind == "status":
                    self.status_var.set(value)
                elif kind == "latest":
                    self.latest_var.set(value)
                elif kind == "installed":
                    self.installed_var.set(value)
                elif kind == "official":
                    self.official_var.set(value)
        except queue.Empty:
            pass
        self.root.after(150, self.poll_messages)

    def append_log(self, text: str):
        self.log_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.log_text.see(tk.END)

    def run_worker(self, name: str, func):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("正在运行", "已有任务正在运行。")
            return
        self.save_config()
        self.stop_event.clear()
        self.set_status(name)
        self.worker = threading.Thread(target=func, daemon=True)
        self.worker.start()

    def detect_streamer(self):
        path = self.core.find_streamer()
        if path:
            self.streamer_var.set(path)
            self.save_config()
            self.append_log(f"已探测 streamer: {path}")
        else:
            messagebox.showerror("未找到", "没有找到 Autodesk webdeploy streamer.exe")

    def pick_streamer(self):
        path = filedialog.askopenfilename(filetypes=[("streamer.exe", "streamer.exe"), ("exe", "*.exe")])
        if path:
            self.streamer_var.set(path)
            self.save_config()

    def pick_log_dir(self):
        path = filedialog.askdirectory()
        if path:
            self.log_dir_var.set(path)
            self.save_config()

    def open_log_dir(self):
        path = Path(self.log_dir_var.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def log_dir(self) -> Path:
        path = Path(self.log_dir_var.get().strip() or str(default_log_dir()))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def streamer_path(self) -> str:
        path = self.streamer_var.get().strip() or self.core.find_streamer()
        if path:
            self.streamer_var.set(path)
        return path

    def proxy_settings(self) -> tuple[str, str]:
        return self.proxy_mode_var.get(), self.proxy_url_var.get().strip()

    def test_manifest(self):
        def work():
            mode, proxy = self.proxy_settings()
            self.enqueue_log(f"测试 manifest: mode={mode}, proxy={proxy or '-'}")
            ok, msg = self.core.head_url(MANIFEST_URL, mode, proxy)
            self.enqueue_log(("成功: " if ok else "失败: ") + msg)
            self.set_status("就绪")
        self.run_worker("测试 manifest", work)

    def check_latest(self):
        def work():
            mode, proxy = self.proxy_settings()
            try:
                version, release, _ = self.core.get_latest_manifest(mode, proxy)
                label = f"{version or 'unknown'} ({release or 'no release'})"
                self.messages.put(("latest", label))
                self.enqueue_log(f"最新 manifest: {label}")
            except Exception as exc:
                self.enqueue_log(f"检查最新失败: {exc}")
            self.set_status("就绪")
        self.run_worker("检查最新", work)

    def query_installed(self):
        def work():
            streamer = self.streamer_path()
            if not streamer or not Path(streamer).exists():
                self.enqueue_log("streamer.exe 不存在")
                self.set_status("就绪")
                return
            mode, proxy = self.proxy_settings()
            result = self.core.run_query(streamer, mode, proxy, self.log_dir())
            if result.ok:
                exe_version = self.core.verify_exe_version(result.launch_exe) if result.launch_exe else ""
                label = result.version
                if exe_version:
                    label += f" / exe {exe_version}"
                self.messages.put(("installed", label))
                self.messages.put(("official", f"Query OK: {result.version}"))
                self.enqueue_log(f"query 成功: {label}")
                if result.launch_exe:
                    self.enqueue_log(f"启动入口: {result.launch_exe}")
            else:
                self.messages.put(("official", "Query 失败"))
                self.enqueue_log(f"query 失败: {result.message}")
            self.set_status("就绪")
        self.run_worker("查询本机", work)

    def start_update(self):
        def work():
            streamer = self.streamer_path()
            if not streamer or not Path(streamer).exists():
                self.enqueue_log("streamer.exe 不存在，先点自动探测或手动选择。")
                self.set_status("就绪")
                return
            mode, proxy = self.proxy_settings()
            try:
                version, release, _ = self.core.get_latest_manifest(mode, proxy)
                self.messages.put(("latest", f"{version} ({release})"))
                self.enqueue_log(f"目标版本: {version} ({release})")
            except Exception as exc:
                self.enqueue_log(f"读取最新 manifest 失败，仍可尝试 updater 自行处理: {exc}")
            result = self.core.run_update_with_retries(
                streamer=streamer,
                mode=mode,
                proxy_url=proxy,
                log_dir=self.log_dir(),
                max_retries=max(1, self.retries_var.get()),
                thread_count=max(1, self.thread_count_var.get()),
                full_deploy=self.full_deploy_var.get(),
                no_cleanup=self.no_cleanup_var.get(),
            )
            if not result.ok:
                self.messages.put(("official", "更新未完成"))
                self.enqueue_log(f"更新失败或未完成: {result.message}")
                self.set_status("就绪")
                return
            self.enqueue_log("开始 official query 验证...")
            query = self.core.run_query(streamer, mode, proxy, self.log_dir())
            if not query.ok:
                self.messages.put(("official", "更新完成但 query 未通过"))
                self.enqueue_log(f"query 验证失败: {query.message}")
                self.set_status("就绪")
                return
            exe_version = self.core.verify_exe_version(query.launch_exe) if query.launch_exe else ""
            if exe_version and exe_version != query.version:
                self.messages.put(("official", f"版本不一致: query {query.version}, exe {exe_version}"))
                self.enqueue_log(f"版本不一致: query={query.version}, exe={exe_version}")
            else:
                self.messages.put(("official", f"完成: {query.version}"))
                self.messages.put(("installed", f"{query.version} / exe {exe_version or 'unknown'}"))
                self.enqueue_log(f"更新收口完成: {query.version}")
                if query.launch_exe:
                    self.enqueue_log(f"启动入口: {query.launch_exe}")
            self.set_status("就绪")
        self.run_worker("自动更新", work)

    def stop_work(self):
        self.stop_event.set()
        self.append_log("已请求停止。")

    def clean_stale_lock(self):
        pids = self.core.running_streamer_pids()
        if pids:
            messagebox.showwarning("不能清理", f"streamer.exe 正在运行: {pids}")
            return
        try:
            backup = self.core.backup_registry_track("manual", only_if_stale=False)
            if backup:
                self.append_log(f"已清理 registry_track: {backup}")
            else:
                self.append_log("没有 registry_track 需要清理。")
        except Exception as exc:
            messagebox.showerror("清理失败", str(exc))

    def download_url(self):
        def work():
            mode, proxy = self.proxy_settings()
            url = self.download_url_var.get().strip()
            if not url:
                self.enqueue_log("URL 为空")
                self.set_status("就绪")
                return
            dest = self.log_dir() / "downloads"
            try:
                target = self.core.download_url(url, mode, proxy, dest)
                self.enqueue_log(f"已下载: {target}")
            except Exception as exc:
                self.enqueue_log(f"下载失败: {exc}")
            self.set_status("就绪")
        self.run_worker("下载 URL", work)

    def detect_idm(self):
        path = self.core.find_idm(self.idm_path_var.get())
        if path:
            self.idm_path_var.set(path)
            self.save_config()
            self.append_log(f"已探测 IDM: {path}")
        else:
            messagebox.showerror("未找到", "没有找到 IDMan.exe")

    def export_plan(self):
        def work():
            mode, proxy = self.proxy_settings()
            try:
                out, count = self.core.send_manifest_packages_to_idm(
                    mode,
                    proxy,
                    self.log_dir() / "idm_downloads",
                    self.idm_path_var.get(),
                    start_queue=True,
                )
                self.enqueue_log(f"已加入 IDM 队列: {count} 个包")
                self.enqueue_log(f"URL 清单: {out}")
            except Exception as exc:
                self.enqueue_log(f"IDM 下载清单失败: {exc}")
            self.set_status("就绪")
        self.run_worker("IDM 下载清单", work)


def main():
    root = Tk()
    try:
        root.call("source", "azure.tcl")
    except tk.TclError:
        pass
    app = FusionUpdaterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
