from __future__ import annotations

import errno
import json
import msvcrt
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


APP_ID = "shixu"
PROTOCOL = "shixu.local/1"
HOST = "127.0.0.1"
DEFAULT_PORT = 43127
DEFAULT_DATA_DIR = Path(r"I:\data\Shixu")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    port: int

    @property
    def database_path(self) -> Path:
        return self.data_dir / "shixu.sqlite3"

    @property
    def token_path(self) -> Path:
        return self.data_dir / f"mcp-{self.port}.token"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / f"instance-{self.port}.lock"

    @property
    def identity_path(self) -> Path:
        return self.data_dir / f"instance-{self.port}.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "service.log"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def host_header(self) -> str:
        return f"{HOST}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.host_header}"


def settings() -> Settings:
    configured_dir = os.environ.get("SHIXU_DATA_DIR")
    data_dir = Path(configured_dir) if configured_dir else DEFAULT_DATA_DIR
    if not data_dir.is_absolute():
        raise ValueError("SHIXU_DATA_DIR must be an absolute path")

    port = int(os.environ.get("SHIXU_PORT", str(DEFAULT_PORT)))
    if port < 1 or port > 65535:
        raise ValueError("SHIXU_PORT must be between 1 and 65535")
    return Settings(data_dir=data_dir, port=port)


def get_mcp_token(config: Settings, *, create: bool) -> str:
    if create and not config.token_path.exists():
        config.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = config.token_path.with_suffix(".token.tmp")
        temporary.write_text(secrets.token_urlsafe(32), encoding="ascii")
        os.replace(temporary, config.token_path)
    token = config.token_path.read_text(encoding="ascii").strip()
    if not token:
        raise RuntimeError("The local MCP token file is empty")
    return token


def acquire_instance_lock(config: Settings) -> BinaryIO | None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    lock_file = config.lock_path.open("a+b")
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        lock_file.close()
        if exc.winerror == 33 or exc.errno in (errno.EACCES, errno.EDEADLK):
            return None
        raise
    return lock_file


def release_instance_lock(lock_file: BinaryIO) -> None:
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    lock_file.close()


def instance_lock_is_held(config: Settings) -> bool:
    if not config.lock_path.exists():
        return False
    lock_file = config.lock_path.open("r+b")
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        lock_file.close()
        if exc.winerror == 33 or exc.errno in (errno.EACCES, errno.EDEADLK):
            return True
        raise
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    lock_file.close()
    return False


def read_instance_identity(config: Settings) -> dict[str, object]:
    return json.loads(config.identity_path.read_text(encoding="utf-8"))


def write_instance_identity(config: Settings, identity: dict[str, object]) -> None:
    temporary = config.identity_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(identity), encoding="utf-8")
    os.replace(temporary, config.identity_path)


def remove_instance_identity(config: Settings, instance_id: str) -> None:
    if not config.identity_path.exists():
        return
    identity = read_instance_identity(config)
    if identity.get("instance_id") == instance_id:
        config.identity_path.unlink()
