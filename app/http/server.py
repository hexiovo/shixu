from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sys
import threading
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from app.http.runtime import (
    APP_ID,
    HOST,
    PROTOCOL,
    Settings,
    acquire_instance_lock,
    get_mcp_token,
    release_instance_lock,
    remove_instance_identity,
    settings,
    write_instance_identity,
)


logger = logging.getLogger("shixu.http")
MAX_REQUEST_BYTES = 1_048_576
SESSION_COOKIE = "shixu_session"
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
STATIC_FILES = {
    "/": (Path(__file__).resolve().parents[1] / "ui" / "today.html", "text/html; charset=utf-8"),
    "/records": (Path(__file__).resolve().parents[1] / "ui" / "index.html", "text/html; charset=utf-8"),
    "/app.js": (Path(__file__).resolve().parents[1] / "ui" / "app.js", "text/javascript; charset=utf-8"),
    "/shared.js": (Path(__file__).resolve().parents[1] / "ui" / "shared.js", "text/javascript; charset=utf-8"),
    "/today.js": (Path(__file__).resolve().parents[1] / "ui" / "today.js", "text/javascript; charset=utf-8"),
    "/today.css": (Path(__file__).resolve().parents[1] / "ui" / "today.css", "text/css; charset=utf-8"),
    "/local-tools.js": (Path(__file__).resolve().parents[1] / "ui" / "local-tools.js", "text/javascript; charset=utf-8"),
    "/app.css": (Path(__file__).resolve().parents[1] / "ui" / "app.css", "text/css; charset=utf-8"),
    "/favicon.ico": (Path(__file__).resolve().parents[1] / "ui" / "favicon.ico", "image/x-icon"),
    "/shixu-icon-64.png": (Path(__file__).resolve().parents[1] / "ui" / "shixu-icon-64.png", "image/png"),
}


@dataclass
class ServiceState:
    config: Settings
    instance_id: str
    pid: int
    executable: str
    mcp_token: str
    session_token: str
    allowed_actions: set[str]
    service: Any = None
    ready: bool = False
    startup_error: str | None = None


class ShixuHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    request_queue_size = 64

    def __init__(self, config: Settings) -> None:
        super().__init__((HOST, config.port), ShixuRequestHandler)
        self.config = config
        self.state: ServiceState | None = None


class ShixuRequestHandler(BaseHTTPRequestHandler):
    server: ShixuHTTPServer
    server_version = "Shixu"
    sys_version = ""

    def _headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(self._response_body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _send(self, status: int, body: bytes, content_type: str, *, cookie: str | None = None) -> None:
        self._response_body = body
        self.send_response(status)
        self._headers(content_type)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _valid_host(self) -> bool:
        return self.headers.get("Host") == self.server.config.host_header

    def _internal_authorized(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        return (
            authorization.startswith(prefix)
            and self.server.state is not None
            and hmac.compare_digest(authorization[len(prefix) :], self.server.state.mcp_token)
        )

    def _valid_browser_session(self) -> bool:
        if self.server.state is None:
            return False
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        morsel = cookies.get(SESSION_COOKIE)
        return morsel is not None and hmac.compare_digest(morsel.value, self.server.state.session_token)

    def _read_json(self) -> dict[str, Any] | None:
        if self.headers.get_content_type() != "application/json":
            self._error(415, "unsupported_media_type", "Content-Type must be application/json")
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._error(411, "length_required", "A valid Content-Length is required")
            return None
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._error(413, "request_too_large", "The request body is too large")
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_json", "The request body must be valid UTF-8 JSON")
            return None
        if not isinstance(value, dict):
            self._error(400, "invalid_request", "The JSON request must be an object")
            return None
        return value

    def _action_request(self, *, mcp_internal: bool) -> None:
        if not self._valid_host():
            self._error(421, "invalid_host", "Requests must use the local Shixu address")
            return
        if not self.server.state or not self.server.state.ready:
            self._error(503, "service_not_ready", "The Shixu service is not ready")
            return
        if mcp_internal:
            if not self._internal_authorized():
                self._error(401, "unauthorized", "A local MCP token is required")
                return
        else:
            if self.headers.get("Origin") != self.server.config.origin:
                self._error(403, "invalid_origin", "The request must come from the local Shixu page")
                return
            if self.headers.get("Sec-Fetch-Site") != "same-origin":
                self._error(403, "invalid_fetch_site", "The request must be same-origin")
                return
            if not self._valid_browser_session():
                self._error(403, "invalid_session", "Reload the local Shixu page to start a session")
                return

        request = self._read_json()
        if request is None:
            return
        action = request.get("action")
        payload = request.get("payload")
        if not isinstance(action, str) or not isinstance(payload, dict):
            self._error(400, "invalid_request", "Expected action and object payload fields")
            return
        if not mcp_internal and action == "local_panel_status" and not payload:
            log_path = self.server.config.log_path
            try:
                with log_path.open("rb") as source:
                    end = source.seek(0, os.SEEK_END)
                    source.seek(max(0, end - 32_768))
                    output = source.read(32_768).decode("utf-8", errors="replace")
                lines = output.splitlines()
                if end > 32_768:
                    lines = lines[1:]
            except FileNotFoundError:
                lines = []
            except OSError:
                self._error(500, "log_unavailable", "The local service log could not be read")
                return
            self._json(200, {"ok": True, "result": {
                "ready": True, "port": self.server.config.port, "lines": lines[-50:],
            }})
            return
        if action not in self.server.state.allowed_actions:
            self._error(404, "unknown_action", "The requested business action is not available")
            return

        try:
            result = self.server.state.service.dispatch(action, payload)
        except Exception:
            logger.exception("Business action failed unexpectedly: %s", action)
            self._error(500, "internal_error", "The action failed; see the local service log")
            return
        self._json(200, result)

    def do_GET(self) -> None:
        if not self._valid_host():
            self._error(421, "invalid_host", "Requests must use the local Shixu address")
            return
        if self.path == "/healthz":
            if not self._internal_authorized():
                self._error(401, "unauthorized", "A local MCP token is required")
                return
            state = self.server.state
            if state is None:
                self._error(503, "service_starting", "The Shixu service is starting")
                return
            status = 503 if state.startup_error else 200
            self._json(
                status,
                {
                    "app_id": APP_ID,
                    "protocol": PROTOCOL,
                    "host": HOST,
                    "port": state.config.port,
                    "pid": state.pid,
                    "instance_id": state.instance_id,
                    "executable": state.executable,
                    "ready": state.ready,
                    "startup_error": state.startup_error,
                },
            )
            return
        state = self.server.state
        if state is None or not state.ready:
            self._error(503, "service_not_ready", "The Shixu service is not ready")
            return
        static = STATIC_FILES.get(self.path)
        if static is None:
            self._error(404, "not_found", "The requested page was not found")
            return
        file_path, content_type = static
        try:
            body = file_path.read_bytes()
        except FileNotFoundError:
            self._error(404, "not_found", "The requested page was not found")
            return
        cookie = None
        if self.path in {"/", "/records"}:
            cookie = f"{SESSION_COOKIE}={state.session_token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
        self._send(200, body, content_type, cookie=cookie)

    def do_POST(self) -> None:
        if self.path == "/api/action":
            self._action_request(mcp_internal=False)
            return
        if self.path == "/_internal/mcp":
            self._action_request(mcp_internal=True)
            return
        if self.path == "/_internal/stop":
            if not self._valid_host():
                self._error(421, "invalid_host", "Requests must use the local Shixu address")
                return
            if not self._internal_authorized():
                self._error(401, "unauthorized", "A local MCP token is required")
                return
            if self.server.state is None or not self.server.state.ready:
                self._error(503, "service_not_ready", "The Shixu service is not ready")
                return
            self._json(200, {"ok": True, "result": {"stopping": True, "pid": self.server.state.pid}})
            self.server.shutdown()
            return
        self._error(404, "not_found", "The requested endpoint was not found")

    def log_message(self, format: str, *args: object) -> None:
        logger.info("%s %s", self.client_address[0], format % args)


def run_server() -> None:
    config = settings()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    server = ShixuHTTPServer(config)
    lock_file = acquire_instance_lock(config)
    if lock_file is None:
        server.server_close()
        raise RuntimeError("Another Shixu instance already holds this data directory and port")

    instance_id = uuid.uuid4().hex
    mcp_token = get_mcp_token(config, create=True)
    server.state = ServiceState(
        config=config,
        instance_id=instance_id,
        pid=os.getpid(),
        executable=sys.executable,
        mcp_token=mcp_token,
        session_token=secrets.token_urlsafe(32),
        allowed_actions=set(),
    )
    identity = {
        "app_id": APP_ID,
        "protocol": PROTOCOL,
        "host": HOST,
        "port": config.port,
        "pid": os.getpid(),
        "instance_id": instance_id,
        "executable": sys.executable,
    }
    write_instance_identity(config, identity)

    serve_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    serve_thread.start()
    stop_reminders = threading.Event()
    reminder_thread = None
    try:
        from app.services import BusinessService, TOOL_SCHEMAS

        server.state.allowed_actions = {schema["name"] for schema in TOOL_SCHEMAS}
        server.state.service = BusinessService(config.database_path)

        def schedule_reminders() -> None:
            while not stop_reminders.is_set():
                try:
                    server.state.service.tick_meal_reminders()
                except Exception:
                    logger.exception("Meal reminder scheduling failed")
                stop_reminders.wait(20)

        reminder_thread = threading.Thread(target=schedule_reminders, name="shixu-meal-reminders", daemon=True)
        reminder_thread.start()
        server.state.ready = True
        logger.info("Shixu ready at http://%s:%s/ (pid %s, instance %s)", HOST, config.port, os.getpid(), instance_id)
        serve_thread.join()
    except Exception as exc:
        server.state.startup_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Shixu service startup failed")
        server.shutdown()
        serve_thread.join()
        raise
    finally:
        server.state.ready = False
        server.server_close()
        stop_reminders.set()
        if reminder_thread is not None:
            reminder_thread.join()
        if server.state.service is not None:
            server.state.service.store.close()
        remove_instance_identity(config, instance_id)
        release_instance_lock(lock_file)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    run_server()
