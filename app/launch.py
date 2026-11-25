from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any

from app.http.runtime import (
    APP_ID,
    HOST,
    PROTOCOL,
    Settings,
    get_mcp_token,
    instance_lock_is_held,
    read_instance_identity,
    remove_instance_identity,
    settings,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 45.0


def _identity_is_shixu(identity: dict[str, Any], config: Settings) -> bool:
    return (
        identity.get("app_id") == APP_ID
        and identity.get("protocol") == PROTOCOL
        and identity.get("host") == HOST
        and identity.get("port") == config.port
        and isinstance(identity.get("pid"), int)
        and isinstance(identity.get("instance_id"), str)
        and isinstance(identity.get("executable"), str)
    )


def _port_is_open(config: Settings) -> bool:
    try:
        with socket.create_connection((HOST, config.port), timeout=0.25):
            return True
    except OSError:
        return False


def _probe_health(config: Settings, token: str, marker: dict[str, Any]) -> dict[str, Any] | None:
    connection = http.client.HTTPConnection(HOST, config.port, timeout=1.0)
    try:
        connection.request(
            "GET",
            "/healthz",
            headers={"Host": config.host_header, "Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        connection.close()

    if not isinstance(body, dict):
        raise RuntimeError("The service health response is not an object")
    if any(body.get(key) != marker.get(key) for key in ("app_id", "protocol", "host", "port", "pid", "instance_id", "executable")):
        raise RuntimeError("The active service identity does not match its locally held instance marker")
    if response.status >= 500 and body.get("startup_error"):
        raise RuntimeError(f"Shixu startup failed: {body['startup_error']}")
    return body


def _child_environment() -> dict[str, str]:
    inherited = ("SYSTEMROOT", "WINDIR", "PATH", "LOCALAPPDATA", "APPDATA", "USERPROFILE", "TEMP", "TMP")
    env = {key: os.environ[key] for key in inherited if key in os.environ}
    for key in ("SHIXU_DATA_DIR", "SHIXU_PORT"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _start_helper(config: Settings, module: str, *args: str, env: dict[str, str] | None) -> subprocess.Popen[bytes]:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = config.log_path.open("ab")
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(
            [sys.executable, "-X", "utf8", "-B", "-m", module, *args],
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            startupinfo=startup_info,
            creationflags=flags,
        )
    finally:
        log_stream.close()


def ensure_service_ready(timeout: float = STARTUP_TIMEOUT) -> dict[str, Any]:
    config = settings()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    unknown_port_deadline: float | None = None
    identity_wait_deadline: float | None = None
    helper: subprocess.Popen[bytes] | None = None

    while time.monotonic() < deadline:
        if instance_lock_is_held(config):
            try:
                marker = read_instance_identity(config)
            except (OSError, json.JSONDecodeError) as exc:
                if identity_wait_deadline is None:
                    identity_wait_deadline = time.monotonic() + 2.0
                elif time.monotonic() >= identity_wait_deadline:
                    raise RuntimeError("A process holds the Shixu instance lock but its identity marker is unreadable") from exc
                time.sleep(0.25)
                continue
            if not _identity_is_shixu(marker, config):
                raise RuntimeError("A process holds the Shixu instance lock with an unknown identity")
            token = get_mcp_token(config, create=False)
            health = _probe_health(config, token, marker)
            if health is not None and health.get("ready") is True:
                return {
                    "url": f"{config.origin}/",
                    "base_url": config.origin,
                    "port": config.port,
                    "instance_id": marker["instance_id"],
                    "pid": marker["pid"],
                    "token": token,
                }
            if health is not None and health.get("startup_error"):
                raise RuntimeError(f"Shixu startup failed: {health['startup_error']}")
        elif helper is None:
            identity_wait_deadline = None
            if _port_is_open(config):
                if unknown_port_deadline is None:
                    unknown_port_deadline = time.monotonic() + 1.0
                elif time.monotonic() >= unknown_port_deadline:
                    raise RuntimeError(f"Port {config.port} is occupied by a process without a Shixu instance identity")
            else:
                helper = _start_helper(config, "app.http.server", env=_child_environment())
        elif helper.poll() is not None:
            identity_wait_deadline = None
            if _port_is_open(config):
                if unknown_port_deadline is None:
                    unknown_port_deadline = time.monotonic() + 1.0
                elif time.monotonic() >= unknown_port_deadline:
                    raise RuntimeError(f"Port {config.port} is occupied by a process without a Shixu instance identity")
            else:
                raise RuntimeError(f"The Shixu helper exited during startup; see {config.log_path}")
        time.sleep(0.25)

    raise TimeoutError(f"Shixu did not become ready within {timeout:g} seconds; see {config.log_path}")


def stop_service(timeout: float = 10.0, *, expected_pid: int | None = None) -> bool:
    config = settings()
    if not instance_lock_is_held(config):
        return False
    marker = read_instance_identity(config)
    if not _identity_is_shixu(marker, config):
        raise RuntimeError("The active instance lock does not identify this Shixu service")
    if expected_pid is not None and marker["pid"] != expected_pid:
        return False
    token = get_mcp_token(config, create=False)
    health = _probe_health(config, token, marker)
    if health is None or health.get("ready") is not True:
        raise RuntimeError("The Shixu service is not ready to stop safely")
    if marker["pid"] == os.getpid():
        raise RuntimeError("The service launcher cannot stop its own process")
    connection = http.client.HTTPConnection(HOST, config.port, timeout=2.0)
    try:
        connection.request(
            "POST",
            "/_internal/stop",
            headers={"Host": config.host_header, "Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()
    if response.status != 200 or not isinstance(body, dict) or body.get("result", {}).get("stopping") is not True:
        raise RuntimeError("The verified Shixu service did not accept the stop request")
    deadline = time.monotonic() + timeout
    while instance_lock_is_held(config) and time.monotonic() < deadline:
        time.sleep(0.25)
    if instance_lock_is_held(config):
        raise TimeoutError(f"Shixu process {marker['pid']} did not stop within {timeout:g} seconds")
    remove_instance_identity(config, str(marker["instance_id"]))
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Start or reuse the local Shixu service")
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--open-browser", action="store_true", help="Open the local maintenance page when ready")
    commands.add_argument("--stop", action="store_true", help="Stop this user's verified Shixu service instance")
    args = parser.parse_args(argv)
    if args.stop:
        try:
            stopped = stop_service()
        except Exception as exc:
            print(f"Shixu could not stop: {exc}", file=sys.stderr)
            return 1
        print("Shixu service stopped." if stopped else "Shixu service is not running.")
        return 0
    try:
        endpoint = ensure_service_ready()
        if args.open_browser:
            _start_helper(settings(), "app.tray", str(endpoint["pid"]), env=None)
    except Exception as exc:
        print(f"Shixu could not start: {exc}", file=sys.stderr)
        return 1
    print(f"Shixu ready at {endpoint['url']} (pid {endpoint['pid']}, instance {endpoint['instance_id']})")
    if args.open_browser:
        webbrowser.open(endpoint["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
