from __future__ import annotations

import json
import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bsky_collector_v2.state import ControlState, dispatch_state_rpc

logger = logging.getLogger("bsky_collector_v2.state_writer")


def _acquire_instance_lock(*, db_path: Path) -> Any:
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+b")  # noqa: SIM115
    try:
        if os.name == "nt":
            import msvcrt

            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as err:
                raise RuntimeError(f"state-writer already running (lock={lock_path})") from err
        else:
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as err:
                raise RuntimeError(f"state-writer already running (lock={lock_path})") from err

        try:
            f.seek(0)
            f.truncate(0)
            f.write(str(os.getpid()).encode("ascii", "ignore"))
            f.flush()
        except OSError:
            pass
        return f
    except Exception:
        f.close()
        raise


def _recv_json_line(conn: socket.socket) -> dict[str, Any] | None:
    buf = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    if not buf:
        return None
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        obj = json.loads(line.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def _send_json_line(conn: socket.socket, obj: dict[str, Any]) -> None:
    payload = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(payload)


@dataclass(frozen=True)
class StateWriterConfig:
    db_path: Path
    socket_path: Path | None = None
    tcp_host: str | None = None
    tcp_port: int | None = None


def run_state_writer(*, cfg: StateWriterConfig) -> None:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    use_unix_socket = cfg.socket_path is not None
    use_tcp = cfg.tcp_host is not None or cfg.tcp_port is not None
    if use_unix_socket == use_tcp:
        raise ValueError("state-writer config must set exactly one of socket_path or tcp_host/tcp_port")

    instance_lock = _acquire_instance_lock(db_path=cfg.db_path)
    server: socket.socket | None = None
    listen_desc = ""
    try:
        if use_unix_socket:
            assert cfg.socket_path is not None
            cfg.socket_path.parent.mkdir(parents=True, exist_ok=True)
            if cfg.socket_path.exists():
                cfg.socket_path.unlink()

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(cfg.socket_path))
            try:
                # Best-effort; chmod is not supported consistently across platforms.
                os.chmod(cfg.socket_path, 0o600)
            except OSError:
                pass
            server.listen(128)
            listen_desc = f"unix:{cfg.socket_path}"
        else:
            host = str(cfg.tcp_host or "127.0.0.1").strip() or "127.0.0.1"
            if cfg.tcp_port is None:
                raise ValueError("state-writer tcp_port is required when using TCP")
            port = int(cfg.tcp_port)
            if port < 0 or port > 65535:
                raise ValueError(f"invalid tcp_port: {port}")

            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except OSError:
                    pass
            else:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((host, port))
            server.listen(128)
            listen_host, listen_port = server.getsockname()[:2]
            listen_desc = f"tcp://{listen_host}:{listen_port}"

        logger.info("state-writer listening addr=%s db=%s", listen_desc, str(cfg.db_path))
        with ControlState.open_local(cfg.db_path) as state:
            while True:
                conn, _addr = server.accept()
                with conn:
                    req = _recv_json_line(conn)
                    if req is None:
                        _send_json_line(
                            conn, {"ok": False, "error_type": "ValueError", "error": "invalid request"}
                        )
                        continue

                    method = str(req.get("method", ""))
                    args = req.get("args", [])
                    kwargs = req.get("kwargs", {})
                    if not isinstance(args, list):
                        args = []
                    if not isinstance(kwargs, dict):
                        kwargs = {}

                    resp = dispatch_state_rpc(state, method=method, args=args, kwargs=kwargs)
                    _send_json_line(conn, resp)
                    if method == "shutdown" and bool(resp.get("ok")):
                        logger.info("state-writer shutdown requested")
                        return
    finally:
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        try:
            instance_lock.close()
        except OSError:
            pass
        if use_unix_socket and cfg.socket_path is not None:
            try:
                if cfg.socket_path.exists():
                    cfg.socket_path.unlink()
            except OSError:
                pass
