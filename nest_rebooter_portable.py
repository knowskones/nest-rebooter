#!/usr/bin/env python3
"""
nest_rebooter_portable_v3.py

Portable Google/Nest WiFi network rebooter for Windows Task Scheduler.

Task Scheduler goal:
  Program/script: py
  Arguments:      "C:\\Scripts\\nest_rebooter_portable_v3.py"

If run with no arguments, this script defaults to the scheduled action: reboot.
Setup and diagnostics are available through optional flags/commands.

Install once:
  py -m pip install gpsoauth playwright
  py -m playwright install chromium

One-time setup:
  py nest_rebooter_portable_v3.py setup

Manual test without reboot:
  py nest_rebooter_portable_v3.py test

Dry run no-argument path:
  set NEST_REBOOTER_DRY_RUN=1
  py nest_rebooter_portable_v3.py

Security notes:
  - Google password is not stored by this script.
  - A long-lived master token is stored in config.json. Protect the script folder accordingly.
  - Logs are written beside this script, but token-like values are redacted.

Unofficial API note:
  This script uses private/unofficial Google Home Foyer behaviours. Google may change these at any time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import platform
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_NAME = "nest-rebooter"
SCRIPT_VERSION = "3.0.0"

FOYER_BASE = "https://googlehomefoyer-pa.googleapis.com"
REBOOT_ENDPOINT_TEMPLATE = "/v2/groups/{system_id}/reboot?prettyPrint=false"
EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"

GOOGLE_HOME_APP = "com.google.android.apps.chromecast.app"
GOOGLE_HOME_SERVICE = "oauth2:https://www.google.com/accounts/OAuthLogin"
GOOGLE_HOME_CLIENT_SIG = "24bb24c05e47e0aefa68a58a766179d9b613a600"

CONNECTIVITY_CHECK_HOST = "8.8.8.8"
CONNECTIVITY_CHECK_PORT = 53

DEFAULT_WAIT_TIMEOUT_SECONDS = 240
DEFAULT_WAIT_INTERVAL_SECONDS = 5
DEFAULT_INITIAL_REBOOT_DELAY_SECONDS = 20
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_BROWSER_TIMEOUT_SECONDS = 240

# Private/unofficial endpoints. Best-effort only.
DISCOVERY_ENDPOINTS = [
    "/v2/groups?prettyPrint=false",
    "/v2/systems?prettyPrint=false",
    "/v2/user/homes?prettyPrint=false",
    "/v2/homes?prettyPrint=false",
    "/v2/structures?prettyPrint=false",
]

TOKEN_REDACTION_PATTERNS = [
    re.compile(r"oauth2_[^\s'\"]+", re.I),
    re.compile(r"aas_et/[^\s'\"]+", re.I),
    re.compile(r"ya29\.[^\s'\"]+", re.I),
    re.compile(r"Bearer\s+[^\s'\"]+", re.I),
]


class NestRebooterError(RuntimeError):
    """Base application error."""


class DependencyError(NestRebooterError):
    """Required dependency is missing."""


class ConfigError(NestRebooterError):
    """Configuration is missing or invalid."""


class ApiError(NestRebooterError):
    """Remote API failed."""


@dataclass
class AppPaths:
    script_path: Path
    script_dir: Path
    config_path: Path
    log_path: Path
    lock_path: Path
    browser_profile_dir: Path
    android_id_path: Path


@dataclass
class AppConfig:
    email: str = ""
    system_id: str = ""
    android_id: str = ""
    master_token: str = ""
    ssid_at_setup: Optional[str] = None
    reboot_endpoint_template: str = REBOOT_ENDPOINT_TEMPLATE
    verify_current_ssid_on_reboot: bool = True
    wait_after_reboot: bool = True
    initial_reboot_delay_seconds: int = DEFAULT_INITIAL_REBOOT_DELAY_SECONDS
    wait_timeout_seconds: int = DEFAULT_WAIT_TIMEOUT_SECONDS
    wait_interval_seconds: int = DEFAULT_WAIT_INTERVAL_SECONDS
    created_at: str = ""
    updated_at: str = ""
    last_reboot_request: Optional[str] = None
    last_reboot_http_status: Optional[int] = None
    discovery_candidate_count: int = 0
    script_version: str = SCRIPT_VERSION
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known and k != "extra"}
        cfg = cls(**kwargs)
        cfg.extra = {k: v for k, v in data.items() if k not in known}
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        extra = data.pop("extra", {}) or {}
        data.update(extra)
        return data

    def validate_for_reboot(self) -> None:
        missing = [name for name in ("email", "system_id", "android_id", "master_token") if not getattr(self, name)]
        if missing:
            raise ConfigError(f"Missing config values: {', '.join(missing)}. Run setup first.")
        if not re.fullmatch(r"[0-9a-fA-F]{16}", self.android_id):
            raise ConfigError("Invalid android_id in config. Run setup again.")


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact(rendered)


def redact(text: str) -> str:
    redacted = text
    for pattern in TOKEN_REDACTION_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def resolve_paths() -> AppPaths:
    script_path = Path(__file__).resolve()
    script_dir = script_path.parent
    config_path = Path(os.environ.get("NEST_REBOOTER_CONFIG", script_dir / "config.json")).expanduser().resolve()
    log_path = Path(os.environ.get("NEST_REBOOTER_LOG", script_dir / "nest-rebooter.log")).expanduser().resolve()
    lock_path = Path(os.environ.get("NEST_REBOOTER_LOCK", script_dir / "nest-rebooter.lock")).expanduser().resolve()
    browser_profile_dir = Path(os.environ.get("NEST_REBOOTER_BROWSER_PROFILE", script_dir / "browser-profile")).expanduser().resolve()
    android_id_path = Path(os.environ.get("NEST_REBOOTER_ANDROID_ID", script_dir / "android_id.txt")).expanduser().resolve()
    return AppPaths(script_path, script_dir, config_path, log_path, lock_path, browser_profile_dir, android_id_path)


def setup_logging(paths: AppPaths, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    paths.script_dir.mkdir(parents=True, exist_ok=True)

    fmt = RedactingFormatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(paths.log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_name, path)
        if platform.system().lower() != "windows":
            with contextlib.suppress(Exception):
                os.chmod(path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def load_config(paths: AppPaths) -> AppConfig:
    if not paths.config_path.exists():
        return AppConfig()
    try:
        return AppConfig.from_dict(json.loads(paths.config_path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is invalid JSON: {paths.config_path}: {exc}") from exc


def save_config(paths: AppPaths, cfg: AppConfig) -> None:
    cfg.updated_at = now_iso()
    if not cfg.created_at:
        cfg.created_at = cfg.updated_at
    cfg.script_version = SCRIPT_VERSION
    atomic_write_json(paths.config_path, cfg.to_dict())


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def dry_run_enabled() -> bool:
    return os.environ.get("NEST_REBOOTER_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}


def run_cmd(args: List[str], timeout: int = 8) -> str:
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, timeout=timeout).decode("utf-8", errors="replace")
    except Exception:
        return ""


def current_wifi_ssid() -> Optional[str]:
    system = platform.system().lower()
    if system == "windows":
        out = run_cmd(["netsh", "wlan", "show", "interfaces"])
        for line in out.splitlines():
            if re.match(r"\s*SSID\s*:", line, re.I) and not re.match(r"\s*BSSID\s*:", line, re.I):
                value = line.split(":", 1)[1].strip()
                return value or None
    if system == "darwin":
        airport = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
        out = run_cmd([airport, "-I"])
        for line in out.splitlines():
            if " SSID:" in line or line.strip().startswith("SSID:"):
                value = line.split(":", 1)[1].strip()
                return value or None
    out = run_cmd(["iwgetid", "-r"])
    if out.strip():
        return out.strip()
    out = run_cmd(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    for line in out.splitlines():
        if line.startswith("yes:"):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def get_or_create_android_id(paths: AppPaths) -> str:
    if paths.android_id_path.exists():
        value = paths.android_id_path.read_text(encoding="utf-8").strip().lower()
        if re.fullmatch(r"[0-9a-f]{16}", value):
            return value
    value = "".join(random.SystemRandom().choice("0123456789abcdef") for _ in range(16))
    paths.android_id_path.write_text(value, encoding="utf-8")
    if platform.system().lower() != "windows":
        with contextlib.suppress(Exception):
            os.chmod(paths.android_id_path, 0o600)
    return value


def require_gpsoauth():
    try:
        import gpsoauth  # type: ignore
        return gpsoauth
    except Exception as exc:
        raise DependencyError(
            "Missing dependency: gpsoauth. Install with: py -m pip install gpsoauth"
        ) from exc


def require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except Exception as exc:
        raise DependencyError(
            "Missing dependency: playwright. Install with: py -m pip install playwright && py -m playwright install chromium"
        ) from exc


def browser_get_oauth_cookie(paths: AppPaths, logger: logging.Logger, timeout_seconds: int) -> str:
    sync_playwright = require_playwright()
    paths.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Opening browser for Google EmbeddedSetup login.")
    logger.info("Log in as the Google Home WiFi owner. The browser will close after the oauth_token cookie is detected.")

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(paths.browser_profile_dir),
                headless=False,
                viewport={"width": 1200, "height": 850},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            raise DependencyError("Could not launch Playwright Chromium. Run: py -m playwright install chromium") from exc

        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(EMBEDDED_SETUP_URL, wait_until="domcontentloaded")
            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                cookies = context.cookies(["https://accounts.google.com", "https://google.com"])
                for cookie in cookies:
                    if cookie.get("name") == "oauth_token" and cookie.get("value"):
                        logger.info("oauth_token cookie detected; closing browser.")
                        return str(cookie["value"])
                time.sleep(1)
        finally:
            with contextlib.suppress(Exception):
                context.close()
    raise ApiError("Timed out waiting for oauth_token. Re-run setup or use manual cookie mode.")


def exchange_cookie_for_master_token(email: str, oauth_token: str, android_id: str) -> str:
    gpsoauth = require_gpsoauth()
    response = gpsoauth.exchange_token(email, oauth_token, android_id)
    if not isinstance(response, dict) or "Token" not in response:
        raise ApiError(f"Could not obtain master token. Response keys: {sorted(response.keys()) if isinstance(response, dict) else type(response)}")
    return str(response["Token"])


def get_access_token(cfg: AppConfig) -> str:
    gpsoauth = require_gpsoauth()
    response = gpsoauth.perform_oauth(
        cfg.email,
        cfg.master_token,
        cfg.android_id,
        app=GOOGLE_HOME_APP,
        service=GOOGLE_HOME_SERVICE,
        client_sig=GOOGLE_HOME_CLIENT_SIG,
    )
    if not isinstance(response, dict) or "Auth" not in response:
        raise ApiError(f"Could not obtain access token. Response keys: {sorted(response.keys()) if isinstance(response, dict) else type(response)}")
    return str(response["Auth"])


def http_request(method: str, url: str, token: str, body: Optional[Dict[str, Any]] = None, timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS) -> Tuple[int, str]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": f"GoogleHome/3.0 {APP_NAME}/{SCRIPT_VERSION}",
        "Content-Type": "application/json; charset=utf-8",
    }
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def get_json(endpoint: str, token: str) -> Optional[Any]:
    status, text = http_request("GET", FOYER_BASE + endpoint, token)
    if not (200 <= status < 300):
        return None
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return None


def walk_objects(obj: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_objects(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_objects(item)


def candidate_name(obj: Dict[str, Any]) -> str:
    fields = ("name", "displayName", "systemName", "networkName", "ssid", "wifiName", "label", "title", "groupName", "homeName", "structureName")
    values = [str(obj.get(k, "")).strip() for k in fields if obj.get(k)]
    return " | ".join(v for v in values if v)


def candidate_id(obj: Dict[str, Any]) -> Optional[str]:
    for key in ("systemId", "system_id", "groupId", "group_id", "id", "resourceId"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def discover_system_id(token: str, target_ssid: Optional[str]) -> Tuple[Optional[str], List[Dict[str, str]]]:
    candidates: List[Dict[str, str]] = []
    seen = set()
    for endpoint in DISCOVERY_ENDPOINTS:
        data = get_json(endpoint, token)
        if data is None:
            continue
        for obj in walk_objects(data):
            cid = candidate_id(obj)
            cname = candidate_name(obj)
            relevant = any(key in obj for key in ("accessPoints", "aps", "stations", "systemId", "groupId", "networkName", "ssid"))
            if not cid or not (cname or relevant):
                continue
            key = (cid, cname, endpoint)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"id": cid, "name": cname or "(unnamed candidate)", "source": endpoint})

    if not candidates:
        return None, []

    if target_ssid:
        ssid_norm = target_ssid.casefold().strip()
        exact = [c for c in candidates if c["name"].casefold().strip() == ssid_norm]
        if len(exact) == 1:
            return exact[0]["id"], candidates
        contains = [c for c in candidates if ssid_norm and ssid_norm in c["name"].casefold()]
        if len(contains) == 1:
            return contains[0]["id"], candidates

    if len(candidates) == 1:
        return candidates[0]["id"], candidates
    return None, candidates


def check_internet(timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((CONNECTIVITY_CHECK_HOST, CONNECTIVITY_CHECK_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_internet(logger: logging.Logger, timeout_seconds: int, interval_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if check_internet():
            return True
        time.sleep(interval_seconds)
    logger.warning("Internet connectivity was not detected before timeout.")
    return False


@contextlib.contextmanager
def single_instance_lock(paths: AppPaths):
    if paths.lock_path.exists():
        try:
            age = time.time() - paths.lock_path.stat().st_mtime
            if age < 60 * 60:
                raise NestRebooterError(f"Another instance appears to be running: {paths.lock_path}")
        except OSError:
            raise NestRebooterError(f"Could not inspect lock file: {paths.lock_path}")
    paths.lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            paths.lock_path.unlink()


def choose_candidate_interactively(candidates: List[Dict[str, str]]) -> Optional[str]:
    if not candidates:
        return None
    print("\nDiscovered possible Google/Nest WiFi systems:")
    for i, candidate in enumerate(candidates, 1):
        print(f"  {i}. {candidate['name']} | id={candidate['id']} | source={candidate['source']}")
    choice = input("Select number, or press Enter to type system_id manually: ").strip()
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        return candidates[int(choice) - 1]["id"]
    return None


def command_setup(args: argparse.Namespace, paths: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(paths)
    cfg.android_id = cfg.android_id or get_or_create_android_id(paths)

    email = args.email or input("Google Home owner's email: ").strip()
    if not email:
        raise ConfigError("Email is required.")
    cfg.email = email

    ssid = args.ssid or current_wifi_ssid()
    cfg.ssid_at_setup = ssid
    if ssid:
        logger.info("Current WiFi SSID detected: %s", ssid)
    else:
        logger.warning("Current WiFi SSID could not be detected.")

    if args.manual_cookie:
        print(f"Open {EMBEDDED_SETUP_URL}, log in as WiFi owner, then paste the oauth_token cookie.")
        oauth_token = input("oauth_token: ").strip()
    else:
        oauth_token = browser_get_oauth_cookie(paths, logger, args.browser_timeout)

    logger.info("Exchanging oauth_token for master token.")
    cfg.master_token = exchange_cookie_for_master_token(cfg.email, oauth_token, cfg.android_id)
    logger.info("Master token acquired.")

    token = get_access_token(cfg)
    system_id = args.system_id
    candidates: List[Dict[str, str]] = []
    if not system_id and not args.no_discover:
        logger.info("Attempting Google/Nest WiFi autodiscovery.")
        system_id, candidates = discover_system_id(token, ssid)
        cfg.discovery_candidate_count = len(candidates)
        if system_id:
            logger.info("Autodiscovered system_id: %s", system_id)
        elif candidates:
            system_id = choose_candidate_interactively(candidates)

    if not system_id:
        system_id = input("Enter WiFi network/group system_id: ").strip()
    if not system_id:
        raise ConfigError("system_id is required. Setup was not saved.")

    cfg.system_id = system_id
    save_config(paths, cfg)
    logger.info("Config saved: %s", paths.config_path)
    logger.info("Log file: %s", paths.log_path)
    logger.info("Next: py %s test", paths.script_path.name)
    return 0


def command_test(args: argparse.Namespace, paths: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(paths)
    cfg.validate_for_reboot()
    logger.info("Authenticating without rebooting.")
    token = get_access_token(cfg)
    logger.info("Auth OK.")
    logger.info("Current SSID: %s", current_wifi_ssid() or "(not detected)")
    logger.info("Configured system_id: %s", cfg.system_id)
    if args.discover:
        selected, candidates = discover_system_id(token, current_wifi_ssid())
        logger.info("Discovery selected: %s", selected or "(none)")
        print(json.dumps(candidates, indent=2))
    return 0


def command_reboot(args: argparse.Namespace, paths: AppPaths, logger: logging.Logger) -> int:
    with single_instance_lock(paths):
        cfg = load_config(paths)
        cfg.validate_for_reboot()

        current_ssid = current_wifi_ssid()
        if cfg.verify_current_ssid_on_reboot and cfg.ssid_at_setup and current_ssid and current_ssid != cfg.ssid_at_setup:
            raise ConfigError(f"Safety stop: current SSID '{current_ssid}' does not match setup SSID '{cfg.ssid_at_setup}'.")

        logger.info("Starting scheduled reboot action. Version=%s DryRun=%s", SCRIPT_VERSION, dry_run_enabled())
        logger.info("Config path: %s", paths.config_path)
        logger.info("Log path: %s", paths.log_path)
        logger.info("Current SSID: %s", current_ssid or "(not detected)")

        if dry_run_enabled() or args.dry_run:
            logger.info("Dry run enabled. No Google API reboot call will be sent.")
            return 0

        logger.info("Authenticating.")
        token = get_access_token(cfg)
        logger.info("Auth OK.")

        url = FOYER_BASE + cfg.reboot_endpoint_template.format(system_id=cfg.system_id)
        logger.info("Sending network reboot request to configured system_id.")
        status, text = http_request("POST", url, token, body={})
        cfg.last_reboot_request = now_iso()
        cfg.last_reboot_http_status = status
        save_config(paths, cfg)

        if not (200 <= status < 300):
            raise ApiError(f"Reboot request failed: HTTP {status}: {text[:500]}")

        logger.info("Reboot request accepted. Network should restart shortly.")
        if not cfg.wait_after_reboot:
            return 0

        time.sleep(max(0, int(cfg.initial_reboot_delay_seconds)))
        if wait_for_internet(logger, int(cfg.wait_timeout_seconds), int(cfg.wait_interval_seconds)):
            logger.info("Internet connectivity detected after reboot.")
        return 0


def command_status(args: argparse.Namespace, paths: AppPaths, logger: logging.Logger) -> int:
    cfg = load_config(paths)
    status_payload = {
        "version": SCRIPT_VERSION,
        "script_path": str(paths.script_path),
        "config_path": str(paths.config_path),
        "log_path": str(paths.log_path),
        "configured": all([cfg.email, cfg.master_token, cfg.android_id, cfg.system_id]),
        "email": cfg.email,
        "system_id": cfg.system_id,
        "ssid_at_setup": cfg.ssid_at_setup,
        "current_ssid_now": current_wifi_ssid(),
        "verify_current_ssid_on_reboot": cfg.verify_current_ssid_on_reboot,
        "has_master_token": bool(cfg.master_token),
        "last_reboot_request": cfg.last_reboot_request,
        "internet_now": check_internet(),
    }
    print(json.dumps(status_payload, indent=2))
    return 0


def command_self_test(args: argparse.Namespace, paths: AppPaths, logger: logging.Logger) -> int:
    logger.info("Running self-test.")
    assert paths.script_path.exists(), "script path missing"
    test_cfg = AppConfig(
        email="user@example.com",
        system_id="test-system-id-123",
        android_id="0123456789abcdef",
        master_token="aas_et/test-token-redacted",
        ssid_at_setup=current_wifi_ssid(),
    )
    test_cfg.validate_for_reboot()
    assert redact("Bearer ya29.secret oauth2_4/secret aas_et/secret") == "[REDACTED] [REDACTED] [REDACTED]"
    logger.info("Self-test OK.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Google/Nest WiFi network rebooter")
    parser.add_argument("--verbose", action="store_true", help="Print debug logs to console as well as file")
    parser.add_argument("--dry-run", action="store_true", help="Do not send the reboot request")

    sub = parser.add_subparsers(dest="command")

    p_setup = sub.add_parser("setup", help="One-time browser-assisted setup")
    p_setup.add_argument("--email")
    p_setup.add_argument("--system-id")
    p_setup.add_argument("--ssid")
    p_setup.add_argument("--manual-cookie", action="store_true")
    p_setup.add_argument("--no-discover", action="store_true")
    p_setup.add_argument("--browser-timeout", type=int, default=DEFAULT_BROWSER_TIMEOUT_SECONDS)
    p_setup.set_defaults(func=command_setup)

    p_test = sub.add_parser("test", help="Authenticate without rebooting")
    p_test.add_argument("--discover", action="store_true")
    p_test.set_defaults(func=command_test)

    p_reboot = sub.add_parser("reboot", help="Reboot now")
    p_reboot.set_defaults(func=command_reboot)

    p_status = sub.add_parser("status", help="Show status")
    p_status.set_defaults(func=command_status)

    p_self = sub.add_parser("self-test", help="Run local self-test only")
    p_self.set_defaults(func=command_self_test)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    paths = resolve_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = setup_logging(paths, verbose=getattr(args, "verbose", False))

    # No command means scheduled use: default to reboot, with no Task Scheduler arguments required.
    if not getattr(args, "command", None):
        args.command = "reboot"
        args.func = command_reboot

    try:
        return int(args.func(args, paths, logger))
    except NestRebooterError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 130
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
