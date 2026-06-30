#!/usr/bin/env python3
"""
nest_rebooter_portable_v3.py

Version 3.1 reliability-hardened portable Google/Nest WiFi network rebooter.

First run:
  py nest_rebooter_portable_v3.py setup

Scheduled/default run:
  py nest_rebooter_portable_v3.py

Reliability hardening in 3.1:
  - PID-aware stale lock detection and cleanup
  - stronger network recovery checks
  - retry/backoff for transient HTTP/API failures
  - auth retry after 401/403
  - scheduler status reports OS truth, not only config truth
  - case-insensitive SSID safety comparison

Unofficial API note:
  This uses private Google Home / Google WiFi behaviours. Google may change them at any time.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.sax.saxutils
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_NAME = "nest-rebooter"
SCRIPT_VERSION = "3.1.0"
CONFIG_SCHEMA_VERSION = 1

FOYER_BASE = "https://googlehomefoyer-pa.googleapis.com"
REBOOT_ENDPOINT_TEMPLATE = "/v2/groups/{system_id}/reboot?prettyPrint=false"
EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"
GENERATE_204_URL = "https://clients3.google.com/generate_204"
FOYER_REACHABILITY_URL = "https://googlehomefoyer-pa.googleapis.com/"

GOOGLE_HOME_APP = "com.google.android.apps.chromecast.app"
GOOGLE_HOME_SERVICE = "oauth2:https://www.google.com/accounts/OAuthLogin"
GOOGLE_HOME_CLIENT_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"

DEFAULT_BROWSER_TIMEOUT_SECONDS = 300
DEFAULT_WAIT_TIMEOUT_SECONDS = 360
DEFAULT_WAIT_INTERVAL_SECONDS = 5
DEFAULT_INITIAL_REBOOT_DELAY_SECONDS = 20
DEFAULT_SPEEDTEST_STABILISATION_SECONDS = 600
DEFAULT_SPEEDTEST_TIMEOUT_SECONDS = 150
DEFAULT_SPEEDTEST_POLL_INTERVAL_SECONDS = 10
DEFAULT_SCHEDULE_TIME = "03:00"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_HTTP_RETRIES = 5
DEFAULT_HTTP_BACKOFF_SECONDS = 1.0
CONNECTIVITY_CHECK_HOST = "8.8.8.8"
CONNECTIVITY_CHECK_PORT = 53

REQUIRED_MODULES = {"gpsoauth": "gpsoauth>=1.0.2", "playwright": "playwright>=1.40"}
RETRYABLE_HTTP_STATUSES = {0, 408, 409, 425, 429, 500, 502, 503, 504}
AUTH_RETRY_STATUSES = {401, 403}

DISCOVERY_ENDPOINTS = [
    "/v2/groups?prettyPrint=false",
    "/v2/systems?prettyPrint=false",
    "/v2/user/homes?prettyPrint=false",
    "/v2/homes?prettyPrint=false",
    "/v2/structures?prettyPrint=false",
]

SPEEDTEST_START_ENDPOINTS = [
    "/v2/groups/{system_id}/speedtest?prettyPrint=false",
    "/v2/groups/{system_id}/speedTest?prettyPrint=false",
    "/v2/groups/{system_id}/speedTest:start?prettyPrint=false",
    "/v2/groups/{system_id}/startSpeedTest?prettyPrint=false",
    "/v2/groups/{system_id}/testInternetSpeed?prettyPrint=false",
    "/v2/groups/{system_id}/runSpeedTest?prettyPrint=false",
]
SPEEDTEST_RESULT_ENDPOINTS = [
    "/v2/groups/{system_id}/speedtest?prettyPrint=false",
    "/v2/groups/{system_id}/speedTest?prettyPrint=false",
    "/v2/groups/{system_id}/speedTestResults?prettyPrint=false",
    "/v2/groups/{system_id}/speedtestResults?prettyPrint=false",
    "/v2/systems/{system_id}/speedtest?prettyPrint=false",
]

TOKEN_PATTERNS = [re.compile(p, re.I) for p in [
    r"oauth2_[^\s'\"]+", r"aas_et/[^\s'\"]+", r"ya29\.[^\s'\"]+", r"Bearer\s+[^\s'\"]+",
    r'("(?:refresh_token|access_token|master_token|oauth_token|authorization)"\s*:\s*")[^"]+(")',
]]

CRON_BEGIN = "# BEGIN Nest-Rebooter"
CRON_END = "# END Nest-Rebooter"
WINDOWS_TASK_NAME = "Nest Rebooter"
MACOS_PLIST_LABEL = "com.nest-rebooter"


class NestRebooterError(RuntimeError): pass
class DependencyError(NestRebooterError): pass
class ConfigError(NestRebooterError): pass
class ApiError(NestRebooterError): pass
class ScheduleError(NestRebooterError): pass


@dataclass
class AppPaths:
    script_path: Path
    script_dir: Path
    config_path: Path
    log_path: Path
    lock_path: Path
    browser_profile_dir: Path
    android_id_path: Path
    speedtest_history_path: Path
    run_history_path: Path


@dataclass
class SpeedTestResult:
    phase: str
    timestamp: str
    supported: bool
    success: bool
    download_mbps: Optional[float] = None
    upload_mbps: Optional[float] = None
    ping_ms: Optional[float] = None
    status: Optional[str] = None
    endpoint: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    raw_summary: Optional[str] = None
    def to_dict(self) -> Dict[str, Any]: return asdict(self)


@dataclass
class AppConfig:
    config_schema_version: int = CONFIG_SCHEMA_VERSION
    email: str = ""
    system_id: str = ""
    system_name: str = ""
    android_id: str = ""
    master_token: str = ""
    ssid_at_setup: Optional[str] = None
    reboot_endpoint_template: str = REBOOT_ENDPOINT_TEMPLATE
    verify_current_ssid_on_reboot: bool = True
    wait_after_reboot: bool = True
    initial_reboot_delay_seconds: int = DEFAULT_INITIAL_REBOOT_DELAY_SECONDS
    wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS
    wait_interval_seconds: int = DEFAULT_WAIT_INTERVAL_SECONDS
    speedtests_enabled: bool = False
    pre_reboot_speedtest_enabled: bool = True
    post_reboot_speedtest_enabled: bool = True
    speedtest_stabilisation_seconds: int = DEFAULT_SPEEDTEST_STABILISATION_SECONDS
    speedtest_timeout_seconds: int = DEFAULT_SPEEDTEST_TIMEOUT_SECONDS
    speedtest_poll_interval_seconds: int = DEFAULT_SPEEDTEST_POLL_INTERVAL_SECONDS
    speedtest_supported: Optional[bool] = None
    last_pre_reboot_speedtest: Optional[Dict[str, Any]] = None
    last_post_reboot_speedtest: Optional[Dict[str, Any]] = None
    schedule_installed: bool = False
    schedule_time: str = DEFAULT_SCHEDULE_TIME
    created_at: str = ""
    updated_at: str = ""
    last_reboot_request: Optional[str] = None
    last_reboot_http_status: Optional[int] = None
    last_run_summary: Optional[Dict[str, Any]] = None
    discovery_candidate_count: int = 0
    script_version: str = SCRIPT_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        data = migrate_config_dict(dict(data))
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        cfg = cls(**{k: v for k, v in data.items() if k in known and k != "extra"})
        cfg.extra = {k: v for k, v in data.items() if k not in known}
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        data.update(extra)
        return data

    def validate_for_run(self) -> None:
        missing = [k for k in ("email", "system_id", "android_id", "master_token") if not getattr(self, k)]
        if missing:
            raise ConfigError(f"Missing config values: {', '.join(missing)}. Run setup first.")
        if not re.fullmatch(r"[0-9a-fA-F]{16}", self.android_id) or self.android_id == "0000000000000000":
            raise ConfigError("Invalid android_id. Run setup again.")
        self.validate_types_and_ranges()

    def validate_types_and_ranges(self) -> None:
        bool_fields = ["verify_current_ssid_on_reboot", "wait_after_reboot", "speedtests_enabled", "pre_reboot_speedtest_enabled", "post_reboot_speedtest_enabled"]
        for name in bool_fields:
            if not isinstance(getattr(self, name), bool):
                raise ConfigError(f"Config value {name} must be boolean.")
        int_ranges = {
            "initial_reboot_delay_seconds": (0, 3600),
            "wait_timeout_seconds": (30, 7200),
            "wait_interval_seconds": (1, 300),
            "speedtest_stabilisation_seconds": (0, 7200),
            "speedtest_timeout_seconds": (10, 1800),
            "speedtest_poll_interval_seconds": (2, 300),
        }
        for name, (low, high) in int_ranges.items():
            value = getattr(self, name)
            if not isinstance(value, int) or not low <= value <= high:
                raise ConfigError(f"Config value {name} must be integer between {low} and {high}.")
        validate_time(self.schedule_time)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def redact(text: str) -> str:
    # Last regex has two groups for JSON token values; handle separately.
    for pattern in TOKEN_PATTERNS[:-1]:
        text = pattern.sub("[REDACTED]", text)
    text = TOKEN_PATTERNS[-1].sub(r'\1[REDACTED]\2', text)
    return text


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def monotonic() -> float:
    return time.monotonic()


def elapsed(start: float) -> float:
    return round(time.monotonic() - start, 3)


def migrate_config_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    version = int(data.get("config_schema_version", 0) or 0)
    if version < 1:
        data["config_schema_version"] = 1
    return data


def resolve_paths() -> AppPaths:
    script_path = Path(__file__).resolve()
    script_dir = script_path.parent
    return AppPaths(
        script_path=script_path,
        script_dir=script_dir,
        config_path=Path(os.environ.get("NEST_REBOOTER_CONFIG", script_dir / "config.json")).expanduser().resolve(),
        log_path=Path(os.environ.get("NEST_REBOOTER_LOG", script_dir / "nest-rebooter.log")).expanduser().resolve(),
        lock_path=Path(os.environ.get("NEST_REBOOTER_LOCK", script_dir / "nest-rebooter.lock")).expanduser().resolve(),
        browser_profile_dir=Path(os.environ.get("NEST_REBOOTER_BROWSER_PROFILE", script_dir / "browser-profile")).expanduser().resolve(),
        android_id_path=Path(os.environ.get("NEST_REBOOTER_ANDROID_ID", script_dir / "android_id.txt")).expanduser().resolve(),
        speedtest_history_path=Path(os.environ.get("NEST_REBOOTER_SPEEDTEST_HISTORY", script_dir / "speedtest-history.jsonl")).expanduser().resolve(),
        run_history_path=Path(os.environ.get("NEST_REBOOTER_RUN_HISTORY", script_dir / "run-history.jsonl")).expanduser().resolve(),
    )


def setup_logging(p: AppPaths, verbose: bool) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    p.script_dir.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.addHandler(console)
    file_handler = logging.FileHandler(p.log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    return logger


def dry_run_enabled() -> bool:
    return os.environ.get("NEST_REBOOTER_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def yes_no(prompt: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{prompt} {suffix}: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer yes or no.")


def validate_time(value: str) -> None:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""):
        raise ScheduleError("Schedule time must be HH:MM 24-hour format.")


def prompt_time(default: str = DEFAULT_SCHEDULE_TIME) -> str:
    while True:
        value = input(f"Daily reboot time HH:MM [{default}]: ").strip() or default
        try:
            validate_time(value)
            return value
        except ScheduleError:
            print("Use 24-hour HH:MM format, for example 03:00.")


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        if platform.system().lower() != "windows":
            with contextlib.suppress(Exception):
                os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def load_config(p: AppPaths) -> AppConfig:
    if not p.config_path.exists():
        return AppConfig()
    try:
        return AppConfig.from_dict(json.loads(p.config_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON config: {p.config_path}: {exc}") from exc


def save_config(p: AppPaths, cfg: AppConfig) -> None:
    cfg.updated_at = now_iso()
    cfg.created_at = cfg.created_at or cfg.updated_at
    cfg.script_version = SCRIPT_VERSION
    cfg.config_schema_version = CONFIG_SCHEMA_VERSION
    atomic_write_json(p.config_path, cfg.to_dict())


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def run_cmd(args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:
        return 1, "", str(exc)


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    system = platform.system().lower()
    if system == "windows":
        code, out, _ = run_cmd(["tasklist", "/FI", f"PID eq {pid}"], timeout=10)
        return code == 0 and str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


@contextlib.contextmanager
def single_instance_lock(p: AppPaths, logger: logging.Logger):
    if p.lock_path.exists():
        try:
            payload = json.loads(p.lock_path.read_text(encoding="utf-8"))
            old_pid = int(payload.get("pid", 0))
            if old_pid and process_exists(old_pid):
                raise NestRebooterError(f"Another instance is running with PID {old_pid}: {p.lock_path}")
            logger.warning("Removing stale lock file: %s", p.lock_path)
            p.lock_path.unlink(missing_ok=True)
        except json.JSONDecodeError:
            logger.warning("Removing unreadable lock file: %s", p.lock_path)
            p.lock_path.unlink(missing_ok=True)
    p.lock_path.write_text(json.dumps({"pid": os.getpid(), "started_at": now_iso()}), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            p.lock_path.unlink()


def current_wifi_ssid() -> Optional[str]:
    system = platform.system().lower()
    if system == "windows":
        code, out, _ = run_cmd(["netsh", "wlan", "show", "interfaces"], timeout=8)
        if code == 0:
            for line in out.splitlines():
                if re.match(r"\s*SSID\s*:", line, re.I) and not re.match(r"\s*BSSID\s*:", line, re.I):
                    return line.split(":", 1)[1].strip() or None
    elif system == "darwin":
        airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
        code, out, _ = run_cmd([airport, "-I"], timeout=8)
        if code == 0:
            for line in out.splitlines():
                if " SSID:" in line or line.strip().startswith("SSID:"):
                    return line.split(":", 1)[1].strip() or None
    else:
        code, out, _ = run_cmd(["iwgetid", "-r"], timeout=8)
        if code == 0 and out.strip():
            return out.strip()
        code, out, _ = run_cmd(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"], timeout=8)
        if code == 0:
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line.split(":", 1)[1].strip() or None
    return None


def ssid_equal(a: Optional[str], b: Optional[str]) -> bool:
    return (a or "").casefold().strip() == (b or "").casefold().strip()


def get_or_create_android_id(p: AppPaths) -> str:
    if p.android_id_path.exists():
        value = p.android_id_path.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(r"[0-9a-f]{16}", value) and value != "0000000000000000":
            return value
    value = "".join(random.SystemRandom().choice("0123456789abcdef") for _ in range(16))
    p.android_id_path.write_text(value, encoding="utf-8")
    return value


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def ensure_dependencies(logger: logging.Logger, auto_install: bool) -> None:
    missing = [pkg for mod, pkg in REQUIRED_MODULES.items() if not module_available(mod)]
    if missing:
        if not auto_install:
            raise DependencyError("Missing dependencies: " + ", ".join(missing))
        logger.info("Installing missing dependencies: %s", ", ".join(missing))
        code, out, err = run_cmd([sys.executable, "-m", "pip", "install", *missing], timeout=900)
        if code != 0:
            raise DependencyError(err or out)
        importlib.invalidate_caches()
    if module_available("playwright"):
        logger.info("Ensuring Playwright Chromium is installed.")
        code, out, err = run_cmd([sys.executable, "-m", "playwright", "install", "chromium"], timeout=900)
        if code != 0:
            raise DependencyError(err or out)


def require_gpsoauth():
    try:
        import gpsoauth  # type: ignore
        return gpsoauth
    except Exception as exc:
        raise DependencyError("gpsoauth missing. Run setup or install requirements.") from exc


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as exc:
        raise DependencyError("playwright missing. Run setup or install requirements.") from exc


def browser_get_oauth_cookie(p: AppPaths, logger: logging.Logger, timeout_seconds: int) -> str:
    sync_playwright = require_playwright()
    p.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Opening browser for Google EmbeddedSetup login.")
    logger.info("Login with the Google Home WiFi owner account. The browser closes when oauth_token is detected.")
    with sync_playwright() as pw:
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=str(p.browser_profile_dir),
                headless=False,
                viewport={"width": 1200, "height": 850},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            raise DependencyError("Could not launch Chromium. Run setup again.") from exc
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(EMBEDDED_SETUP_URL, wait_until="domcontentloaded")
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                for cookie in context.cookies(["https://accounts.google.com", "https://google.com"]):
                    if cookie.get("name") == "oauth_token" and cookie.get("value"):
                        logger.info("oauth_token detected; closing browser.")
                        return str(cookie["value"])
                time.sleep(1)
        finally:
            with contextlib.suppress(Exception):
                context.close()
    raise ApiError("Timed out waiting for oauth_token.")


def exchange_cookie_for_master_token(email: str, oauth_token: str, android_id: str) -> str:
    response = require_gpsoauth().exchange_token(email, oauth_token, android_id)
    if not isinstance(response, dict) or "Token" not in response:
        raise ApiError(f"Could not obtain master token. Response: {type(response)}")
    return str(response["Token"])


def get_access_token(cfg: AppConfig) -> str:
    response = require_gpsoauth().perform_oauth(
        cfg.email, cfg.master_token, cfg.android_id,
        app=GOOGLE_HOME_APP, service=GOOGLE_HOME_SERVICE, client_sig=GOOGLE_HOME_CLIENT_SIG,
    )
    if not isinstance(response, dict) or "Auth" not in response:
        raise ApiError(f"Could not obtain access token. Response: {type(response)}")
    return str(response["Auth"])


def base_http_request(method: str, url: str, token: Optional[str] = None, body: Optional[Dict[str, Any]] = None, timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS) -> Tuple[int, str, float]:
    start = monotonic()
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"User-Agent": f"GoogleHome/3.0 {APP_NAME}/{SCRIPT_VERSION}", "Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), elapsed(start)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), elapsed(start)
    except Exception as exc:
        return 0, str(exc), elapsed(start)


def http_request_with_retry(method: str, url: str, token: Optional[str] = None, body: Optional[Dict[str, Any]] = None, logger: Optional[logging.Logger] = None, attempts: int = DEFAULT_HTTP_RETRIES) -> Tuple[int, str, float]:
    last_status, last_text, total_latency = 0, "", 0.0
    for attempt in range(1, attempts + 1):
        status, text, latency = base_http_request(method, url, token=token, body=body)
        total_latency += latency
        last_status, last_text = status, text
        if logger:
            logger.debug("HTTP %s %s attempt=%s status=%s latency=%ss", method, url, attempt, status, latency)
        if status not in RETRYABLE_HTTP_STATUSES or attempt == attempts:
            return status, text, round(total_latency, 3)
        sleep_for = DEFAULT_HTTP_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.35)
        if logger:
            logger.debug("Retrying after transient HTTP status %s in %.2fs", status, sleep_for)
        time.sleep(sleep_for)
    return last_status, last_text, round(total_latency, 3)


def authenticated_request(cfg: AppConfig, method: str, endpoint_or_url: str, body: Optional[Dict[str, Any]] = None, logger: Optional[logging.Logger] = None) -> Tuple[int, str, float]:
    url = endpoint_or_url if endpoint_or_url.startswith("http") else FOYER_BASE + endpoint_or_url
    token = get_access_token(cfg)
    status, text, latency = http_request_with_retry(method, url, token=token, body=body, logger=logger)
    if status in AUTH_RETRY_STATUSES:
        if logger:
            logger.warning("Auth failure HTTP %s; refreshing token and retrying once.", status)
        token = get_access_token(cfg)
        status, text, latency2 = http_request_with_retry(method, url, token=token, body=body, logger=logger, attempts=2)
        latency = round(latency + latency2, 3)
    return status, text, latency


def get_json_authenticated(cfg: AppConfig, endpoint: str, logger: Optional[logging.Logger] = None) -> Optional[Any]:
    status, text, _ = authenticated_request(cfg, "GET", endpoint, logger=logger)
    if not 200 <= status < 300:
        return None
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None


def check_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def dns_resolves(hostname: str) -> bool:
    try:
        socket.gethostbyname(hostname)
        return True
    except OSError:
        return False


def https_204_ok() -> bool:
    status, _text, _latency = base_http_request("GET", GENERATE_204_URL, timeout=5)
    return status in {200, 204}


def foyer_reachable() -> bool:
    status, _text, _latency = base_http_request("GET", FOYER_REACHABILITY_URL, timeout=5)
    # 400/401/403/404 still means HTTPS and the Google API host are reachable.
    return status in {200, 204, 400, 401, 403, 404, 405}


def network_recovered(logger: Optional[logging.Logger] = None) -> Tuple[bool, Dict[str, bool]]:
    checks = {
        "tcp_google_dns": check_tcp(CONNECTIVITY_CHECK_HOST, CONNECTIVITY_CHECK_PORT),
        "dns_google_foyer": dns_resolves("googlehomefoyer-pa.googleapis.com"),
        "https_generate_204": https_204_ok(),
        "google_foyer_reachable": foyer_reachable(),
    }
    ok = checks["dns_google_foyer"] and checks["https_generate_204"] and checks["google_foyer_reachable"]
    if logger:
        logger.debug("Network recovery checks: %s", checks)
    return ok, checks


def wait_for_network_recovery(logger: logging.Logger, timeout_seconds: int, interval_seconds: int) -> Tuple[bool, Dict[str, bool], float]:
    start = monotonic()
    deadline = time.time() + timeout_seconds
    last_checks: Dict[str, bool] = {}
    while time.time() < deadline:
        ok, last_checks = network_recovered(logger)
        if ok:
            return True, last_checks, elapsed(start)
        time.sleep(interval_seconds)
    logger.warning("Network recovery was not fully confirmed before timeout. Last checks: %s", last_checks)
    return False, last_checks, elapsed(start)


def walk_objects(obj: Any) -> Iterable[Dict[str, Any]]:
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def candidate_name(obj: Dict[str, Any]) -> str:
    keys = ("name", "displayName", "systemName", "networkName", "ssid", "wifiName", "label", "title", "groupName")
    return " | ".join(str(obj.get(k, "")).strip() for k in keys if obj.get(k))


def candidate_id(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("systemId", "system_id", "groupId", "group_id", "id", "resourceId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def discover_system_id(cfg: AppConfig, ssid: Optional[str], logger: Optional[logging.Logger] = None) -> Tuple[Optional[str], Optional[str], List[Dict[str, str]]]:
    candidates: List[Dict[str, str]] = []
    seen = set()
    for endpoint in DISCOVERY_ENDPOINTS:
        data = get_json_authenticated(cfg, endpoint, logger=logger)
        if data is None:
            continue
        for obj in walk_objects(data):
            cid = candidate_id(obj)
            name = candidate_name(obj)
            relevant = any(k in obj for k in ("accessPoints", "aps", "stations", "systemId", "groupId", "networkName", "ssid"))
            if not cid or not (name or relevant):
                continue
            key = (cid, name, endpoint)
            if key not in seen:
                seen.add(key)
                candidates.append({"id": cid, "name": name or "(unnamed candidate)", "source": endpoint})
    if not candidates:
        return None, None, []
    if ssid:
        norm = ssid.casefold().strip()
        matches = [c for c in candidates if norm and norm in c["name"].casefold()]
        if len(matches) == 1:
            return matches[0]["id"], matches[0]["name"], candidates
    if len(candidates) == 1:
        return candidates[0]["id"], candidates[0]["name"], candidates
    return None, None, candidates


def choose_candidate(candidates: List[Dict[str, str]]) -> Tuple[Optional[str], Optional[str]]:
    if not candidates:
        return None, None
    print("\nDiscovered possible Google/Nest WiFi systems:")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c['name']} | id={c['id']} | source={c['source']}")
    choice = input("Select number, or press Enter to type system_id manually: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        c = candidates[int(choice) - 1]
        return c["id"], c["name"]
    return None, None


def normalise_speed(value: float, key: str) -> float:
    key_l = key.lower()
    if ("bps" in key_l and "mbps" not in key_l) or value > 100_000:
        return round(value / 1_000_000, 2)
    return round(value, 2)


def first_number_for_keys(obj: Any, patterns: List[re.Pattern[str]]) -> Optional[float]:
    for item in walk_objects(obj):
        for k, v in item.items():
            if any(p.search(str(k)) for p in patterns):
                if isinstance(v, (int, float)):
                    return normalise_speed(float(v), str(k))
                if isinstance(v, str):
                    m = re.search(r"\d+(?:\.\d+)?", v)
                    if m:
                        return normalise_speed(float(m.group(0)), str(k))
    return None


def parse_speedtest(payload: Any, phase: str, endpoint: str, duration_seconds: Optional[float] = None) -> Optional[SpeedTestResult]:
    if payload is None:
        return None
    down = first_number_for_keys(payload, [re.compile("download", re.I), re.compile("downlink", re.I)])
    up = first_number_for_keys(payload, [re.compile("upload", re.I), re.compile("uplink", re.I)])
    ping = first_number_for_keys(payload, [re.compile("ping", re.I), re.compile("latency", re.I)])
    status = None
    for obj in walk_objects(payload):
        for k, v in obj.items():
            if str(k).lower() in {"status", "state", "result", "teststatus", "speedteststatus"} and isinstance(v, str):
                status = v
                break
        if status:
            break
    if down is None and up is None and status is None:
        return None
    failed = (status or "").lower() in {"failed", "error", "cancelled", "canceled"}
    return SpeedTestResult(
        phase=phase,
        timestamp=now_iso(),
        supported=True,
        success=not failed and (down is not None or up is not None or (status or "").lower() in {"complete", "completed", "success", "done"}),
        download_mbps=down,
        upload_mbps=up,
        ping_ms=ping,
        status=status,
        endpoint=endpoint,
        duration_seconds=duration_seconds,
        raw_summary=redact(json.dumps(payload, ensure_ascii=False)[:1000]),
    )


def start_native_speedtest(cfg: AppConfig, logger: logging.Logger) -> Tuple[bool, Optional[str], Optional[SpeedTestResult], float]:
    total_latency = 0.0
    for template in SPEEDTEST_START_ENDPOINTS:
        endpoint = template.format(system_id=cfg.system_id)
        status, text, latency = authenticated_request(cfg, "POST", endpoint, body={}, logger=logger)
        total_latency += latency
        logger.debug("Speedtest start %s returned HTTP %s latency=%ss", endpoint, status, latency)
        if 200 <= status < 300:
            payload = None
            if text.strip():
                with contextlib.suppress(json.JSONDecodeError):
                    payload = json.loads(text)
            return True, endpoint, parse_speedtest(payload, "speedtest", endpoint, total_latency) if payload is not None else None, total_latency
        if status in AUTH_RETRY_STATUSES:
            raise ApiError(f"Speedtest permission/auth failure on {endpoint}: HTTP {status}")
    return False, None, None, total_latency


def fetch_native_speedtest_result(cfg: AppConfig, phase: str, logger: logging.Logger) -> Optional[SpeedTestResult]:
    for template in SPEEDTEST_RESULT_ENDPOINTS:
        endpoint = template.format(system_id=cfg.system_id)
        payload = get_json_authenticated(cfg, endpoint, logger=logger)
        result = parse_speedtest(payload, phase, endpoint)
        if result:
            logger.debug("Speedtest result parsed from %s", endpoint)
            return result
    return None


def log_speedtest_result(result: SpeedTestResult, p: AppPaths, logger: logging.Logger) -> None:
    append_jsonl(p.speedtest_history_path, result.to_dict())
    if result.success:
        logger.info(
            "Speed test %s complete: download=%s Mbps upload=%s Mbps ping=%s ms duration=%ss status=%s",
            result.phase,
            result.download_mbps if result.download_mbps is not None else "unknown",
            result.upload_mbps if result.upload_mbps is not None else "unknown",
            result.ping_ms if result.ping_ms is not None else "unknown",
            result.duration_seconds if result.duration_seconds is not None else "unknown",
            result.status or "unknown",
        )
    elif not result.supported:
        logger.warning("Speed test %s unavailable: %s", result.phase, result.error)
    else:
        logger.warning("Speed test %s failed/incomplete: %s", result.phase, result.error or result.status or "unknown")


def run_native_speedtest(cfg: AppConfig, phase: str, p: AppPaths, logger: logging.Logger) -> SpeedTestResult:
    start = monotonic()
    logger.info("Starting Google/Nest native speed test (%s).", phase)
    started, endpoint, immediate, _latency = start_native_speedtest(cfg, logger)
    if not started:
        result = SpeedTestResult(phase=phase, timestamp=now_iso(), supported=False, success=False, duration_seconds=elapsed(start), error="No known Google/Nest speed test endpoint accepted the request.")
        log_speedtest_result(result, p, logger)
        return result
    if immediate and (immediate.download_mbps is not None or immediate.upload_mbps is not None):
        immediate.phase = phase
        immediate.duration_seconds = elapsed(start)
        log_speedtest_result(immediate, p, logger)
        return immediate
    deadline = time.time() + max(10, int(cfg.speedtest_timeout_seconds))
    last = immediate
    while time.time() < deadline:
        time.sleep(max(2, int(cfg.speedtest_poll_interval_seconds)))
        result = fetch_native_speedtest_result(cfg, phase, logger)
        if result:
            last = result
            if result.download_mbps is not None or result.upload_mbps is not None:
                result.duration_seconds = elapsed(start)
                log_speedtest_result(result, p, logger)
                return result
    if last:
        last.phase = phase
        last.duration_seconds = elapsed(start)
        if last.download_mbps is None and last.upload_mbps is None:
            last.success = False
            last.error = last.error or "No download/upload values returned before timeout."
        log_speedtest_result(last, p, logger)
        return last
    result = SpeedTestResult(phase=phase, timestamp=now_iso(), supported=True, success=False, endpoint=endpoint, duration_seconds=elapsed(start), error="Started but no result was available before timeout.")
    log_speedtest_result(result, p, logger)
    return result


def maybe_speedtest(cfg: AppConfig, phase: str, p: AppPaths, logger: logging.Logger) -> Optional[SpeedTestResult]:
    if not cfg.speedtests_enabled:
        logger.info("Speed tests disabled.")
        return None
    if phase == "pre-reboot" and not cfg.pre_reboot_speedtest_enabled:
        logger.info("Pre-reboot speed test disabled.")
        return None
    if phase == "post-reboot" and not cfg.post_reboot_speedtest_enabled:
        logger.info("Post-reboot speed test disabled.")
        return None
    return run_native_speedtest(cfg, phase, p, logger)


def install_schedule(p: AppPaths, schedule_time: str, logger: logging.Logger) -> None:
    validate_time(schedule_time)
    system = platform.system().lower()
    if system == "windows":
        task_run = f'"{Path(sys.executable).resolve()}" "{p.script_path}"'
        code, out, err = run_cmd(["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/TR", task_run, "/SC", "DAILY", "/ST", schedule_time, "/F"], timeout=60)
        if code != 0:
            raise ScheduleError(err or out)
        logger.info("Windows scheduled task installed daily at %s.", schedule_time)
    elif system == "darwin":
        install_macos_schedule(p, schedule_time, logger)
    else:
        install_linux_cron_schedule(p, schedule_time, logger)


def install_linux_cron_schedule(p: AppPaths, schedule_time: str, logger: logging.Logger) -> None:
    if shutil.which("crontab") is None:
        raise ScheduleError("crontab not found.")
    hour, minute = schedule_time.split(":")
    cron_line = f'{int(minute)} {int(hour)} * * * "{Path(sys.executable).resolve()}" "{p.script_path}" >> "{p.script_dir / "nest-rebooter.schedule.log"}" 2>&1'
    code, existing, err = run_cmd(["crontab", "-l"], timeout=30)
    if code != 0:
        existing = "" if "no crontab" in err.lower() else existing
    new_cron = remove_marked(existing).rstrip()
    new_cron = (new_cron + "\n" if new_cron else "") + f"{CRON_BEGIN}\n{cron_line}\n{CRON_END}\n"
    proc = subprocess.run(["crontab", "-"], input=new_cron, text=True, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise ScheduleError(proc.stderr or proc.stdout)
    logger.info("Linux cron schedule installed daily at %s.", schedule_time)


def install_macos_schedule(p: AppPaths, schedule_time: str, logger: logging.Logger) -> None:
    hour, minute = [int(x) for x in schedule_time.split(":")]
    plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_PLIST_LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>{xml.sax.saxutils.escape(MACOS_PLIST_LABEL)}</string><key>ProgramArguments</key><array><string>{xml.sax.saxutils.escape(str(Path(sys.executable).resolve()))}</string><string>{xml.sax.saxutils.escape(str(p.script_path))}</string></array><key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict><key>StandardOutPath</key><string>{xml.sax.saxutils.escape(str(p.script_dir / 'nest-rebooter.schedule.log'))}</string><key>StandardErrorPath</key><string>{xml.sax.saxutils.escape(str(p.script_dir / 'nest-rebooter.schedule.err.log'))}</string></dict></plist>
"""
    plist.write_text(content, encoding="utf-8")
    with contextlib.suppress(Exception):
        run_cmd(["launchctl", "unload", str(plist)], timeout=30)
    code, out, err = run_cmd(["launchctl", "load", str(plist)], timeout=30)
    if code != 0:
        raise ScheduleError(err or out)
    logger.info("macOS launchd schedule installed daily at %s.", schedule_time)


def remove_marked(text: str) -> str:
    return re.sub(re.escape(CRON_BEGIN) + r".*?" + re.escape(CRON_END) + r"\n?", "", text, flags=re.S)


def uninstall_schedule(p: AppPaths, logger: logging.Logger) -> None:
    system = platform.system().lower()
    if system == "windows":
        run_cmd(["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"], timeout=30)
        logger.info("Windows scheduled task removed if present.")
    elif system == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_PLIST_LABEL}.plist"
        with contextlib.suppress(Exception):
            run_cmd(["launchctl", "unload", str(plist)], timeout=30)
        with contextlib.suppress(FileNotFoundError):
            plist.unlink()
        logger.info("macOS launchd schedule removed if present.")
    else:
        code, existing, _ = run_cmd(["crontab", "-l"], timeout=30)
        if code == 0:
            subprocess.run(["crontab", "-"], input=remove_marked(existing).rstrip() + "\n", text=True, capture_output=True, timeout=30)
        logger.info("Linux cron schedule removed if present.")


def schedule_status() -> Dict[str, Any]:
    system = platform.system().lower()
    if system == "windows":
        code, out, err = run_cmd(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME], timeout=30)
        return {"platform": "windows", "installed": code == 0, "detail": out or err}
    if system == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_PLIST_LABEL}.plist"
        return {"platform": "macos", "installed": plist.exists(), "detail": str(plist)}
    code, out, err = run_cmd(["crontab", "-l"], timeout=30)
    return {"platform": "linux", "installed": code == 0 and CRON_BEGIN in out and CRON_END in out, "detail": out if code == 0 else err}


def command_setup(args: argparse.Namespace, p: AppPaths, logger: logging.Logger) -> int:
    if not args.skip_dependency_check:
        ensure_dependencies(logger, auto_install=args.yes or yes_no("Install or repair required dependencies now?", True))
    cfg = load_config(p)
    cfg.android_id = cfg.android_id or get_or_create_android_id(p)
    cfg.email = args.email or input("Google Home owner's email: ").strip()
    if not cfg.email:
        raise ConfigError("Email is required.")
    cfg.ssid_at_setup = args.ssid or current_wifi_ssid()
    logger.info("Current WiFi SSID: %s", cfg.ssid_at_setup or "(not detected)")
    oauth_token = input("oauth_token: ").strip() if args.manual_cookie else browser_get_oauth_cookie(p, logger, args.browser_timeout)
    logger.info("Exchanging oauth_token for master token.")
    cfg.master_token = exchange_cookie_for_master_token(cfg.email, oauth_token, cfg.android_id)
    system_id = args.system_id
    system_name = None
    if not system_id and not args.no_discover:
        logger.info("Attempting network autodiscovery.")
        system_id, system_name, candidates = discover_system_id(cfg, cfg.ssid_at_setup, logger=logger)
        cfg.discovery_candidate_count = len(candidates)
        if not system_id and candidates:
            system_id, system_name = choose_candidate(candidates)
    if not system_id:
        system_id = input("Enter WiFi network/group system_id: ").strip()
    if not system_id:
        raise ConfigError("system_id is required.")
    cfg.system_id = system_id
    cfg.system_name = system_name or cfg.system_name
    if args.enable_speedtests is not None:
        cfg.speedtests_enabled = args.enable_speedtests
    elif args.yes:
        cfg.speedtests_enabled = False
    else:
        cfg.speedtests_enabled = yes_no("Run Google/Nest speed tests before and after scheduled reboots?", False)
    if cfg.speedtests_enabled:
        cfg.pre_reboot_speedtest_enabled = True if args.yes else yes_no("Run pre-reboot speed test for baseline logging?", True)
        cfg.post_reboot_speedtest_enabled = True if args.yes else yes_no("Run post-reboot speed test after recovery?", True)
        if cfg.post_reboot_speedtest_enabled and not args.yes:
            val = input(f"Post-reboot stabilisation delay seconds [{cfg.speedtest_stabilisation_seconds}]: ").strip()
            if val:
                cfg.speedtest_stabilisation_seconds = max(0, int(val))
    save_config(p, cfg)
    logger.info("Config saved: %s", p.config_path)
    logger.info("Log file: %s", p.log_path)
    install_sched = args.install_schedule if args.install_schedule is not None else (True if args.yes else yes_no("Create or update daily scheduled reboot now?", True))
    if install_sched:
        t = args.schedule_time or (cfg.schedule_time if args.yes else prompt_time(cfg.schedule_time))
        install_schedule(p, t, logger)
        os_status = schedule_status()
        cfg.schedule_installed = bool(os_status.get("installed"))
        cfg.schedule_time = t
        save_config(p, cfg)
        if not cfg.schedule_installed:
            logger.warning("Schedule command completed but OS schedule status did not confirm installation.")
    logger.info("Setup complete.")
    return 0


def command_test(args: argparse.Namespace, p: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(p)
    cfg.validate_for_run()
    _ = get_access_token(cfg)
    logger.info("Auth OK. Current SSID=%s system_id=%s", current_wifi_ssid() or "(not detected)", cfg.system_id)
    if args.discover:
        print(json.dumps(discover_system_id(cfg, current_wifi_ssid(), logger=logger)[2], indent=2))
    return 0


def command_speedtest(args: argparse.Namespace, p: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(p)
    cfg.validate_for_run()
    if args.dry_run or dry_run_enabled():
        logger.info("Dry run enabled. No speed test API call will be sent.")
        return 0
    result = run_native_speedtest(cfg, args.phase, p, logger)
    cfg.speedtest_supported = result.supported
    if args.phase == "pre-reboot":
        cfg.last_pre_reboot_speedtest = result.to_dict()
    elif args.phase == "post-reboot":
        cfg.last_post_reboot_speedtest = result.to_dict()
    save_config(p, cfg)
    return 0 if result.success or not result.supported else 2


def command_reboot(args: argparse.Namespace, p: AppPaths, logger: logging.Logger) -> int:
    workflow_start = monotonic()
    run_summary: Dict[str, Any] = {"started_at": now_iso(), "version": SCRIPT_VERSION, "dry_run": bool(args.dry_run or dry_run_enabled())}
    with single_instance_lock(p, logger):
        cfg = load_config(p)
        cfg.validate_for_run()
        ssid = current_wifi_ssid()
        if cfg.verify_current_ssid_on_reboot and cfg.ssid_at_setup and ssid and not ssid_equal(ssid, cfg.ssid_at_setup):
            raise ConfigError(f"Safety stop: current SSID '{ssid}' != setup SSID '{cfg.ssid_at_setup}'.")
        logger.info("Starting reboot workflow. Version=%s DryRun=%s", SCRIPT_VERSION, args.dry_run or dry_run_enabled())
        logger.info("Network=%s CurrentSSID=%s", cfg.system_name or cfg.system_id, ssid or "(not detected)")
        logger.info("Config=%s Log=%s SpeedHistory=%s", p.config_path, p.log_path, p.speedtest_history_path)
        if args.dry_run or dry_run_enabled():
            logger.info("Dry run enabled. No Google API call will be sent.")
            run_summary.update({"completed_at": now_iso(), "success": True, "total_duration_seconds": elapsed(workflow_start)})
            append_jsonl(p.run_history_path, run_summary)
            return 0
        pre = maybe_speedtest(cfg, "pre-reboot", p, logger)
        if pre:
            cfg.last_pre_reboot_speedtest = pre.to_dict()
            cfg.speedtest_supported = pre.supported
            save_config(p, cfg)
        logger.info("Sending reboot request.")
        status, text, request_latency = authenticated_request(cfg, "POST", cfg.reboot_endpoint_template.format(system_id=cfg.system_id), body={}, logger=logger)
        cfg.last_reboot_request = now_iso()
        cfg.last_reboot_http_status = status
        save_config(p, cfg)
        run_summary.update({"reboot_http_status": status, "reboot_request_latency_seconds": request_latency})
        if not 200 <= status < 300:
            run_summary.update({"completed_at": now_iso(), "success": False, "error": f"HTTP {status}", "total_duration_seconds": elapsed(workflow_start)})
            append_jsonl(p.run_history_path, run_summary)
            raise ApiError(f"Reboot request failed: HTTP {status}: {text[:500]}")
        logger.info("Reboot request accepted. API latency=%ss", request_latency)
        if cfg.wait_after_reboot:
            time.sleep(max(0, cfg.initial_reboot_delay_seconds))
            recovered, checks, recovery_duration = wait_for_network_recovery(logger, cfg.wait_timeout_seconds, cfg.wait_interval_seconds)
            run_summary.update({"network_recovered": recovered, "network_recovery_checks": checks, "network_recovery_duration_seconds": recovery_duration})
            if recovered:
                logger.info("Network recovery confirmed after %ss.", recovery_duration)
            if cfg.speedtests_enabled and cfg.post_reboot_speedtest_enabled:
                delay = max(0, cfg.speedtest_stabilisation_seconds)
                if delay:
                    logger.info("Waiting %s seconds before post-reboot speed test.", delay)
                    time.sleep(delay)
                post = maybe_speedtest(cfg, "post-reboot", p, logger)
                if post:
                    cfg.last_post_reboot_speedtest = post.to_dict()
                    cfg.speedtest_supported = post.supported
                    save_config(p, cfg)
        run_summary.update({"completed_at": now_iso(), "success": True, "total_duration_seconds": elapsed(workflow_start)})
        cfg.last_run_summary = run_summary
        save_config(p, cfg)
        append_jsonl(p.run_history_path, run_summary)
        logger.info("Network restart workflow complete. Duration=%ss", run_summary["total_duration_seconds"])
        return 0


def command_status(args: argparse.Namespace, p: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(p)
    os_schedule = schedule_status()
    schedule_mismatch = bool(cfg.schedule_installed) != bool(os_schedule.get("installed"))
    print(json.dumps({
        "version": SCRIPT_VERSION,
        "script_path": str(p.script_path),
        "config_path": str(p.config_path),
        "log_path": str(p.log_path),
        "speedtest_history_path": str(p.speedtest_history_path),
        "run_history_path": str(p.run_history_path),
        "configured": all([cfg.email, cfg.master_token, cfg.android_id, cfg.system_id]),
        "email": cfg.email,
        "system_id": cfg.system_id,
        "system_name": cfg.system_name,
        "ssid_at_setup": cfg.ssid_at_setup,
        "current_ssid_now": current_wifi_ssid(),
        "speedtests_enabled": cfg.speedtests_enabled,
        "speedtest_supported_last_result": cfg.speedtest_supported,
        "schedule_config_claims_installed": cfg.schedule_installed,
        "schedule_os_detected_installed": os_schedule.get("installed"),
        "schedule_mismatch": schedule_mismatch,
        "schedule_time": cfg.schedule_time,
        "last_reboot_request": cfg.last_reboot_request,
        "last_pre_reboot_speedtest": cfg.last_pre_reboot_speedtest,
        "last_post_reboot_speedtest": cfg.last_post_reboot_speedtest,
        "last_run_summary": cfg.last_run_summary,
        "scheduler": os_schedule,
        "network_recovery_now": network_recovered(logger=None)[0],
    }, indent=2))
    if schedule_mismatch:
        logger.warning("Scheduler mismatch: config says %s, OS says %s", cfg.schedule_installed, os_schedule.get("installed"))
    return 0


def command_install_deps(args, p, logger):
    ensure_dependencies(logger, True)
    logger.info("Dependencies installed/verified.")
    return 0


def command_install_schedule(args, p, logger):
    t = args.time or prompt_time(DEFAULT_SCHEDULE_TIME)
    install_schedule(p, t, logger)
    cfg = load_config(p)
    os_status = schedule_status()
    cfg.schedule_installed = bool(os_status.get("installed"))
    cfg.schedule_time = t
    save_config(p, cfg)
    if not cfg.schedule_installed:
        raise ScheduleError("Schedule install command completed, but OS status did not confirm installation.")
    return 0


def command_uninstall_schedule(args, p, logger):
    uninstall_schedule(p, logger)
    cfg = load_config(p)
    cfg.schedule_installed = bool(schedule_status().get("installed"))
    save_config(p, cfg)
    return 0


def command_schedule_status(args, p, logger):
    print(json.dumps(schedule_status(), indent=2))
    return 0


def command_self_test(args, p, logger):
    logger.info("Running self-test.")
    cfg = AppConfig(email="user@example.com", system_id="test-system-id", android_id="0123456789abcdef", master_token="aas_et/test")
    cfg.validate_for_run()
    assert redact("Bearer ya29.secret oauth2_4/secret aas_et/secret") == "[REDACTED] [REDACTED] [REDACTED]"
    parsed = parse_speedtest({"downloadMbps": 842.5, "uploadMbps": 47.2, "pingMs": 12, "status": "COMPLETE"}, "pre-reboot", "/test")
    assert parsed and parsed.download_mbps == 842.5 and parsed.upload_mbps == 47.2 and parsed.ping_ms == 12
    assert remove_marked(f"a\n{CRON_BEGIN}\nb\n{CRON_END}\nc\n") == "a\nc\n"
    assert ssid_equal(" HomeWiFi ", "homewifi")
    assert not process_exists(-1)
    logger.info("Self-test OK.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Google/Nest WiFi network rebooter")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command")
    s = sub.add_parser("setup")
    s.add_argument("--email")
    s.add_argument("--system-id")
    s.add_argument("--ssid")
    s.add_argument("--manual-cookie", action="store_true")
    s.add_argument("--no-discover", action="store_true")
    s.add_argument("--browser-timeout", type=int, default=DEFAULT_BROWSER_TIMEOUT_SECONDS)
    s.add_argument("--skip-dependency-check", action="store_true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--enable-speedtests", dest="enable_speedtests", action="store_true")
    s.add_argument("--disable-speedtests", dest="enable_speedtests", action="store_false")
    s.set_defaults(enable_speedtests=None)
    s.add_argument("--install-schedule", dest="install_schedule", action="store_true")
    s.add_argument("--no-install-schedule", dest="install_schedule", action="store_false")
    s.set_defaults(install_schedule=None)
    s.add_argument("--schedule-time")
    s.set_defaults(func=command_setup)
    t = sub.add_parser("test")
    t.add_argument("--discover", action="store_true")
    t.set_defaults(func=command_test)
    sp = sub.add_parser("speedtest")
    sp.add_argument("--phase", choices=["manual", "pre-reboot", "post-reboot"], default="manual")
    sp.set_defaults(func=command_speedtest)
    sub.add_parser("reboot").set_defaults(func=command_reboot)
    sub.add_parser("install-deps").set_defaults(func=command_install_deps)
    isch = sub.add_parser("install-schedule")
    isch.add_argument("--time")
    isch.set_defaults(func=command_install_schedule)
    sub.add_parser("uninstall-schedule").set_defaults(func=command_uninstall_schedule)
    sub.add_parser("schedule-status").set_defaults(func=command_schedule_status)
    sub.add_parser("status").set_defaults(func=command_status)
    sub.add_parser("self-test").set_defaults(func=command_self_test)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    p = resolve_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = setup_logging(p, getattr(args, "verbose", False))
    if not getattr(args, "command", None):
        args.command = "reboot"
        args.func = command_reboot
    try:
        return int(args.func(args, p, logger))
    except NestRebooterError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
