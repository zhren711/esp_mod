from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

try:
    import psutil
except ImportError:
    psutil = None

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


DEFAULT_CONFIG = {
    "transport": "wifi",
    "device_url": "http://192.168.1.100/status",
    "port": "auto",
    "baud": 115200,
    "poll_seconds": 2,
    "http_timeout_seconds": 10,
    "inactive_seconds": 15,
    "db_active_seconds": 30,
    "usage_cache_max_age_seconds": 900,
    "hook_active_seconds": 60,
    "offline_quota_fallback": True,
    "codex_home": "",
    "claude_enabled": True,
    "claude_home": "",
    "claude_device_url": "",
    "claude_poll_seconds": 300,
}


def load_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        config.update(json.loads(path.read_text(encoding="utf-8")))
    return config


def codex_home(config: dict[str, Any]) -> Path:
    configured = str(config.get("codex_home", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def read_hook_state(home: Path, active_seconds: float = 60) -> str | None:
    path = home / "lcd_hook_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        state = str(data.get("state", "")).upper()
        age = time.time() - float(data.get("updated_at", 0))
        ttl = 12 if state == "DONE" else active_seconds
        if age <= ttl and state in {
            "IDLE", "THINKING", "WRITING", "RUNNING", "DONE", "ERROR", "NEED_CONFIRM"
        }:
            return state
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None

def read_db_activity(home: Path) -> tuple[float, str | None, str | None, int | None]:
    """Read activity from every thread, but metadata only from a user CLI thread."""
    path = home / "state_5.sqlite"
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as connection:
            heartbeat = connection.execute(
                "SELECT MAX(COALESCE(updated_at_ms, updated_at * 1000)) FROM threads WHERE archived = 0"
            ).fetchone()
            row = connection.execute(
                """
                SELECT model, cwd, tokens_used
                FROM threads
                WHERE archived = 0 AND source = 'cli'
                ORDER BY COALESCE(updated_at_ms, updated_at * 1000) DESC
                LIMIT 1
                """
            ).fetchone()
        updated_at = float(heartbeat[0] or 0) / 1000.0 if heartbeat else 0.0
        if not row:
            return updated_at, None, None, None
        model = str(row[0]) if row[0] else None
        workspace = Path(str(row[1])).name if row[1] else None
        tokens = int(row[2]) if row[2] is not None else None
        return updated_at, model, workspace, tokens
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return 0.0, None, None, None

def newest_rollout(home: Path) -> Path | None:
    sessions = home / "sessions"
    # Check explicit recent date paths. Do not enumerate the whole session tree:
    # real Codex installations can contain hundreds of thousands of rollouts.
    today = dt.datetime.now().date()
    for offset in range(31):
        day = today - dt.timedelta(days=offset)
        directory = sessions / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        try:
            files = list(directory.glob("rollout-*.jsonl"))
        except OSError:
            continue
        if files:
            return max(files, key=lambda p: p.stat().st_mtime)
    return None

def read_tail_lines(path: Path, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = file.tell()
        file.seek(max(0, size - max_bytes))
        data = file.read()
    if size > max_bytes:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace").splitlines()


def deep_find(obj: Any, names: set[str]) -> Any:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in names and value not in (None, ""):
                return value
        for value in obj.values():
            found = deep_find(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = deep_find(value, names)
            if found not in (None, ""):
                return found
    return None


def parse_rollout(path: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "model": "-",
        "workspace": "-",
        "tokens": 0,
        "rate_limits": None,
        "last_event": 0.0,
        "failed": False,
    }
    if not path:
        return result
    result["last_event"] = path.stat().st_mtime

    for line in reversed(read_tail_lines(path)):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        if result["model"] == "-":
            value = deep_find(record, {"model", "model_name"})
            if isinstance(value, str):
                result["model"] = value
        if result["workspace"] == "-":
            value = deep_find(record, {"cwd", "working_directory"})
            if isinstance(value, str):
                result["workspace"] = Path(value).name or value
        if not result["tokens"]:
            value = deep_find(record, {"total_token_usage"})
            if isinstance(value, dict):
                result["tokens"] = int(value.get("total_tokens", 0) or 0)
            elif isinstance(value, (int, float)):
                result["tokens"] = int(value)
        if result["rate_limits"] is None:
            value = deep_find(record, {"rate_limits", "rateLimits"})
            if isinstance(value, dict):
                result["rate_limits"] = value

        text = json.dumps(record, ensure_ascii=False).lower()
        if '"status": "failed"' in text or '"status":"failed"' in text:
            result["failed"] = True

        if result["model"] != "-" and result["workspace"] != "-" and result["tokens"]:
            # Continue a little farther only when rate limits are still needed.
            if result["rate_limits"] is not None:
                break
    return result


def codex_process_running(excluded_pid: int | None = None) -> bool:
    if psutil is None:
        return False
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if excluded_pid is not None and proc.pid == excluded_pid:
                continue
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info["cmdline"] or []).lower()
            if "codex" in name or cmdline.strip().endswith(" codex"):
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


class AppServer:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.next_id = 1
        self.cached_limits: dict[str, Any] | None = None
        self.refresh_thread: threading.Thread | None = None
        self.last_refresh_attempt = 0.0

    def start(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (FileNotFoundError, OSError):
            self.process = None
            return False
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self.request(
                "initialize",
                {"clientInfo": {"name": "codex-lcd", "title": "Codex LCD Monitor", "version": "1.0.0"}},
                timeout=5,
            )
            self.notify("initialized", {})
            return True
        except Exception:
            self.close()
            return False

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            try:
                message = json.loads(line)
                if "id" in message:
                    self.responses.put(message)
            except json.JSONDecodeError:
                continue

    def _write(self, message: dict[str, Any]) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("app-server is not running")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 5) -> Any:
        request_id = self.next_id
        self.next_id += 1
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        self._write(message)
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        try:
            while time.monotonic() < deadline:
                response = self.responses.get(timeout=max(0.05, deadline - time.monotonic()))
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(str(response["error"]))
                    return response.get("result")
                deferred.append(response)
        finally:
            for response in deferred:
                self.responses.put(response)
        raise TimeoutError(method)

    def _refresh_rate_limits(self) -> None:
        if not self.start():
            return
        try:
            result = self.request("account/rateLimits/read", timeout=8)
            if isinstance(result, dict) and isinstance(result.get("rateLimits"), dict):
                self.cached_limits = result["rateLimits"]
        except Exception:
            pass

    def rate_limits(self) -> dict[str, Any] | None:
        now = time.monotonic()
        refreshing = self.refresh_thread is not None and self.refresh_thread.is_alive()
        if not refreshing and now - self.last_refresh_attempt >= 60:
            self.last_refresh_attempt = now
            self.refresh_thread = threading.Thread(target=self._refresh_rate_limits, daemon=True)
            self.refresh_thread.start()
        return self.cached_limits

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None


def select_quota_windows(limits: dict[str, Any]) -> tuple[Any, Any]:
    entries: list[tuple[float, dict[str, Any]]] = []
    for value in limits.values():
        if not isinstance(value, dict):
            continue
        duration = value.get("windowDurationMins", value.get("window_minutes"))
        if isinstance(duration, (int, float)):
            entries.append((float(duration), value))
    if entries:
        five_candidates = [item for item in entries if abs(item[0] - 300) <= abs(item[0] - 10080)]
        week_candidates = [item for item in entries if abs(item[0] - 10080) < abs(item[0] - 300)]
        five = min(five_candidates, key=lambda item: abs(item[0] - 300))[1] if five_candidates else None
        week = min(week_candidates, key=lambda item: abs(item[0] - 10080))[1] if week_candidates else None
        return five, week
    return limits.get("primary"), limits.get("secondary")

def read_usage_cache(home: Path, max_age_seconds: float = 900) -> dict[str, Any]:
    """Translate codex-cli-usage schema v2 into app-server style windows."""
    path = home / "usage-limits.json"
    try:
        if time.time() - path.stat().st_mtime > max_age_seconds:
            return {}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}

    translated: dict[str, Any] = {}
    for key in ("primary", "secondary", "5h", "7d"):
        source = data.get(key)
        if not isinstance(source, dict):
            continue
        pct = source.get("pct", source.get("used_percent", source.get("usedPercent")))
        seconds = source.get("window_secs", source.get("limit_window_seconds"))
        if seconds is None:
            seconds = 18000 if key == "5h" else 604800 if key == "7d" else None
        reset = source.get("resets_at", source.get("resetsAt"))
        if isinstance(reset, str):
            try:
                reset = dt.datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
            except ValueError:
                reset = None
        window: dict[str, Any] = {"usedPercent": pct, "resetsAt": reset}
        if isinstance(seconds, (int, float)):
            window["windowDurationMins"] = float(seconds) / 60.0
        translated[key] = window
    return translated

def normalize_window(window: Any) -> tuple[int | None, str]:
    if not isinstance(window, dict):
        return None, "-"
    used = window.get("usedPercent", window.get("used_percent"))
    reset = window.get("resetsAt", window.get("resets_at"))
    if reset is None and window.get("resets_in_seconds") is not None:
        reset = time.time() + float(window["resets_in_seconds"])
    remaining = None if used is None else max(0, min(100, round(100 - float(used))))
    if reset is None:
        return remaining, "-"
    seconds = max(0, int(float(reset) - time.time()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return remaining, f"{hours:02d}:{minutes:02d}"


def select_port(configured: str) -> str | None:
    if configured.lower() != "auto":
        return configured
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    preferred = [p for p in ports if any(x in (p.description or "").lower() for x in ("ch340", "cp210", "usb serial"))]
    candidates = preferred or ports
    return candidates[0].device if candidates else None


def build_payload(config: dict[str, Any], server: AppServer) -> dict[str, Any]:
    home = codex_home(config)
    rollout = newest_rollout(home)
    local = parse_rollout(rollout)
    age = time.time() - float(local["last_event"] or 0)
    db_updated_at, db_model, db_workspace, db_tokens = read_db_activity(home)
    db_age = time.time() - db_updated_at if db_updated_at else float("inf")
    if local["failed"] and age < config["inactive_seconds"]:
        state = "ERROR"
    elif db_age < float(config.get("db_active_seconds", 30)):
        state = "THINKING"
    elif age < config["inactive_seconds"]:
        state = "RUNNING"
    else:
        state = "IDLE"
    # Hook events are more precise than process/log heuristics. They expire so
    # stale state never leaves the display permanently busy.
    hook_state = read_hook_state(home, float(config.get("hook_active_seconds", 60)))
    if hook_state is not None:
        state = hook_state

    limits = server.rate_limits()
    if not limits and config.get("offline_quota_fallback"):
        limits = local.get("rate_limits")
    limits = limits or {}
    five_window, week_window = select_quota_windows(limits)
    cache_limits = read_usage_cache(
        home, float(config.get("usage_cache_max_age_seconds", 900))
    )
    cache_five, cache_week = select_quota_windows(cache_limits)
    if five_window is None:
        five_window = cache_five
    if week_window is None:
        week_window = cache_week
    five_left, reset5 = normalize_window(five_window)
    week_left, _ = normalize_window(week_window)
    return {
        "state": state,
        "model": str(db_model or local["model"])[:20],
        "workspace": str(db_workspace or local["workspace"])[:22],
        "tokens": db_tokens if db_tokens is not None else local["tokens"],
        "five_left": five_left,
        "week_left": week_left,
        "reset5": reset5,
    }


def claude_home(config: dict[str, Any]) -> Path:
    configured = str(config.get("claude_home", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def reset_countdown(value: Any) -> str:
    if not value:
        return "-"
    try:
        if isinstance(value, str):
            target = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        else:
            target = float(value)
        seconds = max(0, int(target - time.time()))
        hours, remainder = divmod(seconds, 3600)
        return f"{hours:02d}:{remainder // 60:02d}"
    except (TypeError, ValueError, OverflowError):
        return "-"


def latest_claude_project(home: Path) -> str:
    history = home / "history.jsonl"
    try:
        lines = read_tail_lines(history, 128 * 1024)
        for line in reversed(lines):
            record = json.loads(line)
            project = record.get("project")
            if project:
                return Path(str(project)).name or str(project)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return "-"


class ClaudeUsage:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.cached: dict[str, Any] | None = None
        self.last_attempt = 0.0
        self.last_error = ""

    def _fetch(self) -> dict[str, Any]:
        credentials_path = claude_home(self.config) / ".credentials.json"
        credentials = json.loads(credentials_path.read_text(encoding="utf-8-sig"))
        oauth = credentials.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Claude OAuth access token is missing")
        request = urlrequest.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.161",
            },
            method="GET",
        )
        timeout = float(self.config.get("http_timeout_seconds", 10))
        with urlrequest.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def refresh(self, force: bool = False) -> None:
        interval = float(self.config.get("claude_poll_seconds", 300))
        now = time.monotonic()
        if not force and now - self.last_attempt < interval:
            return
        self.last_attempt = now
        try:
            self.cached = self._fetch()
            self.last_error = ""
        except (OSError, ValueError, RuntimeError, urlerror.URLError, urlerror.HTTPError) as exc:
            self.last_error = str(exc)

    @staticmethod
    def _window(data: dict[str, Any], *names: str) -> dict[str, Any] | None:
        for name in names:
            value = data.get(name)
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _remaining(window: dict[str, Any] | None) -> int | None:
        if not window:
            return None
        used = window.get("utilization", window.get("used_percent"))
        if used is None:
            return None
        value = float(used)
        if 0 <= value <= 1 and not float(value).is_integer():
            value *= 100
        return max(0, min(100, round(100 - value)))

    def payload(self, force: bool = False) -> dict[str, Any]:
        self.refresh(force=force)
        data = self.cached or {}
        five = self._window(data, "five_hour", "fiveHour", "5h")
        week = self._window(data, "seven_day", "sevenDay", "7d")
        credentials_plan = "Claude Code"
        try:
            credentials = json.loads(
                (claude_home(self.config) / ".credentials.json").read_text(encoding="utf-8-sig")
            )
            plan = (credentials.get("claudeAiOauth") or {}).get("subscriptionType")
            if plan:
                credentials_plan = f"Claude {str(plan).title()}"
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return {
            "state": "ONLINE" if data else ("ERROR" if self.last_error else "IDLE"),
            "model": credentials_plan[:20],
            "workspace": latest_claude_project(claude_home(self.config))[:22],
            "tokens": 0,
            "five_left": self._remaining(five),
            "week_left": self._remaining(week),
            "reset5": reset_countdown((five or {}).get("resets_at", (five or {}).get("resetsAt"))),
        }


def provider_url(config: dict[str, Any], provider: str) -> str:
    if provider == "claude":
        explicit = str(config.get("claude_device_url", "")).strip()
        if explicit:
            return explicit
    base = str(config.get("device_url", "")).strip()
    if provider == "claude":
        if base.endswith("/status"):
            return base + "/claude"
        return base.rstrip("/") + "/status/claude"
    return base


def send_wifi_payload(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, str]:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urlrequest.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlrequest.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(128).decode("utf-8", errors="replace")

def main() -> int:
    parser = argparse.ArgumentParser(description="Show local Codex status on an ESP8266 ST7789 display")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--once", action="store_true", help="Print one payload without opening a serial port")
    parser.add_argument("--demo", action="store_true", help="Send a fixed demo payload")
    args = parser.parse_args()
    config = load_config(args.config)
    server = AppServer()
    claude = ClaudeUsage(config)

    if args.demo:
        payload = {"state": "RUNNING", "model": "gpt-5.6", "workspace": "esp32", "tokens": 18342,
                   "five_left": 69, "week_left": 43, "reset5": "01:42"}
    else:
        payload = build_payload(config, server)
    if args.once:
        result = {"codex": payload}
        if config.get("claude_enabled", True):
            result["claude"] = claude.payload(force=True)
        print(json.dumps(result, ensure_ascii=False))
        server.close()
        return 0

    transport = str(config.get("transport", "serial")).lower()
    if transport == "wifi":
        codex_url = provider_url(config, "codex")
        claude_url = provider_url(config, "claude")
        if not codex_url:
            print("device_url is missing in host/config.json", file=sys.stderr)
            return 2
        print(f"Sending Codex to {codex_url}")
        if config.get("claude_enabled", True):
            print(f"Sending Claude usage to {claude_url} every {config.get('claude_poll_seconds', 300)}s")
        print("Ctrl+C to stop.")
        next_claude_at = 0.0
        timeout = float(config.get("http_timeout_seconds", 10))
        try:
            while True:
                if not args.demo:
                    payload = build_payload(config, server)
                try:
                    status, reply = send_wifi_payload(codex_url, payload, timeout)
                    print(f"{dt.datetime.now():%H:%M:%S} Codex HTTP {status} {reply}")
                except (urlerror.URLError, TimeoutError, OSError) as exc:
                    print(f"{dt.datetime.now():%H:%M:%S} Codex send failed: {exc}", file=sys.stderr)

                now = time.monotonic()
                if config.get("claude_enabled", True) and now >= next_claude_at:
                    claude_payload = (
                        {"state": "ONLINE", "model": "Claude Max", "workspace": "esp32",
                         "tokens": 0, "five_left": 72, "week_left": 51, "reset5": "02:31"}
                        if args.demo else claude.payload(force=True)
                    )
                    try:
                        status, reply = send_wifi_payload(claude_url, claude_payload, timeout)
                        print(f"{dt.datetime.now():%H:%M:%S} Claude HTTP {status} {reply}")
                    except (urlerror.URLError, TimeoutError, OSError) as exc:
                        print(f"{dt.datetime.now():%H:%M:%S} Claude send failed: {exc}", file=sys.stderr)
                    if claude.last_error:
                        print(f"{dt.datetime.now():%H:%M:%S} Claude usage warning: {claude.last_error}", file=sys.stderr)
                    next_claude_at = now + float(config.get("claude_poll_seconds", 300))
                time.sleep(float(config["poll_seconds"]))
        except KeyboardInterrupt:
            return 0
        finally:
            server.close()
    if serial is None:
        print("pyserial is not installed. Run: python -m pip install -r host/requirements.txt", file=sys.stderr)
        return 2

    port_name = select_port(str(config["port"]))
    if not port_name:
        print("No serial port found. Set port in host/config.json.", file=sys.stderr)
        return 2
    print(f"Sending Codex status to {port_name} at {config['baud']} baud. Ctrl+C to stop.")
    try:
        with serial.Serial(port_name, int(config["baud"]), timeout=1) as port:
            time.sleep(2)
            while True:
                if not args.demo:
                    payload = build_payload(config, server)
                line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                port.write(line.encode("utf-8"))
                print(f"{dt.datetime.now():%H:%M:%S} {line.strip()}")
                time.sleep(float(config["poll_seconds"]))
    except KeyboardInterrupt:
        return 0
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())













