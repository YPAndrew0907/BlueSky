from __future__ import annotations

import os
import sqlite3
import socket
import threading
import time
from multiprocessing import Process
from pathlib import Path

import pytest

import bsky_collector_v2.state_writer as state_writer_module
from bsky_collector_v2.state import ControlState, RemoteControlState
from bsky_collector_v2.state_writer import StateWriterConfig, run_state_writer
from bsky_collector_v2.time_utils import format_utc, now_utc
from bsky_collector_v2.types import FeedUri, PostUri


def _wait_for_socket(path: Path, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"state-writer socket not created: {path}")


def _pick_free_tcp_port() -> int:
    # Best-effort: reserve a port at the OS level then release it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_tcp(host: str, port: int, *, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"state-writer tcp not reachable: {host}:{port}")


def test_state_writer_proxy_roundtrip(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("unix domain sockets are not reliable across Windows environments; use TCP transport test")

    db_path = tmp_path / "control" / "control_state.db"
    sock_path = Path(f"/tmp/bsky_state_writer_test_{os.getpid()}_{time.time_ns()}.sock")
    if sock_path.exists():
        sock_path.unlink()

    proc = Process(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, socket_path=sock_path)},
        daemon=True,
    )
    proc.start()
    _wait_for_socket(sock_path)

    old_socket_env = os.environ.get("BSKY_STATE_WRITER_SOCKET")
    os.environ["BSKY_STATE_WRITER_SOCKET"] = str(sock_path)

    try:
        ts = format_utc(now_utc())
        with ControlState.open(db_path) as state:
            state.start_run(run_id="run_state_writer_test", job_name="unit", started_at_utc=ts, params={"k": 1})
            state.upsert_post_registry_many(
                post_uris=[
                    PostUri("at://did:plc:a/app.bsky.feed.post/1"),
                    PostUri("at://did:plc:a/app.bsky.feed.post/1"),
                ],
                seen_at_utc=ts,
            )
            state.upsert_feed_catalog(
                feed_uri=FeedUri("at://did:plc:a/app.bsky.feed.generator/main"),
                creator_did="did:plc:a",
                service_did="did:web:example.com",
                provider_domain="example.com",
                like_count_last=7,
                discovered_from=["test_state_writer"],
                seen_at_utc=ts,
            )
            state.commit()

            rows = list(state.iter_feed_catalog())
            assert rows
            assert rows[0]["provider_domain"] == "example.com"

        with ControlState.open_local(db_path) as local:
            run = local.conn.execute("SELECT job_name, success FROM runs WHERE run_id=?", ("run_state_writer_test",)).fetchone()
            assert run is not None
            assert str(run["job_name"]) == "unit"

            post_row = local.conn.execute(
                "SELECT seen_count FROM post_registry WHERE post_uri=?",
                ("at://did:plc:a/app.bsky.feed.post/1",),
            ).fetchone()
            assert post_row is not None
            assert int(post_row["seen_count"]) == 2
    finally:
        if old_socket_env is None:
            os.environ.pop("BSKY_STATE_WRITER_SOCKET", None)
        else:
            os.environ["BSKY_STATE_WRITER_SOCKET"] = old_socket_env

        try:
            RemoteControlState(path=db_path, socket_path=sock_path)._rpc("shutdown")
        except Exception:  # noqa: BLE001
            pass
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


def test_state_writer_proxy_roundtrip_tcp(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    host = "127.0.0.1"
    port = _pick_free_tcp_port()

    proc = Process(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port)},
        daemon=True,
    )
    proc.start()
    _wait_for_tcp(host, port)

    old_socket_env = os.environ.get("BSKY_STATE_WRITER_SOCKET")
    os.environ["BSKY_STATE_WRITER_SOCKET"] = f"tcp://{host}:{port}"

    try:
        ts = format_utc(now_utc())
        with ControlState.open(db_path) as state:
            state.start_run(run_id="run_state_writer_test_tcp", job_name="unit", started_at_utc=ts, params={"k": 1})
            state.upsert_post_registry_many(
                post_uris=[
                    PostUri("at://did:plc:a/app.bsky.feed.post/1"),
                    PostUri("at://did:plc:a/app.bsky.feed.post/1"),
                ],
                seen_at_utc=ts,
            )
            state.upsert_feed_catalog(
                feed_uri=FeedUri("at://did:plc:a/app.bsky.feed.generator/main"),
                creator_did="did:plc:a",
                service_did="did:web:example.com",
                provider_domain="example.com",
                like_count_last=7,
                discovered_from=["test_state_writer_tcp"],
                seen_at_utc=ts,
            )
            state.commit()

            rows = list(state.iter_feed_catalog())
            assert rows
            assert rows[0]["provider_domain"] == "example.com"

        with ControlState.open_local(db_path) as local:
            run = local.conn.execute(
                "SELECT job_name, success FROM runs WHERE run_id=?",
                ("run_state_writer_test_tcp",),
            ).fetchone()
            assert run is not None
            assert str(run["job_name"]) == "unit"

            post_row = local.conn.execute(
                "SELECT seen_count FROM post_registry WHERE post_uri=?",
                ("at://did:plc:a/app.bsky.feed.post/1",),
            ).fetchone()
            assert post_row is not None
            assert int(post_row["seen_count"]) == 2
    finally:
        if old_socket_env is None:
            os.environ.pop("BSKY_STATE_WRITER_SOCKET", None)
        else:
            os.environ["BSKY_STATE_WRITER_SOCKET"] = old_socket_env

        try:
            RemoteControlState(path=db_path, tcp_host=host, tcp_port=port)._rpc("shutdown")
        except Exception:  # noqa: BLE001
            pass
        proc.join(timeout=5)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)


def test_state_writer_instance_lock_prevents_dupes(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    host = "127.0.0.1"
    port_1 = _pick_free_tcp_port()

    proc_1 = Process(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port_1)},
        daemon=True,
    )
    proc_1.start()
    _wait_for_tcp(host, port_1)

    port_2 = _pick_free_tcp_port()
    proc_2 = Process(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port_2)},
        daemon=True,
    )
    proc_2.start()
    proc_2.join(timeout=2)

    assert not proc_2.is_alive()
    assert proc_2.exitcode not in (None, 0)
    assert proc_1.is_alive()

    try:
        RemoteControlState(path=db_path, tcp_host=host, tcp_port=port_1)._rpc("shutdown")
    except Exception:  # noqa: BLE001
        pass
    proc_1.join(timeout=5)
    if proc_1.is_alive():
        proc_1.terminate()
        proc_1.join(timeout=5)


def test_state_writer_survives_dispatch_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    host = "127.0.0.1"
    port = _pick_free_tcp_port()

    original_dispatch = state_writer_module.dispatch_state_rpc
    failure_counter = {"count": 0}

    def flaky_dispatch(state, *, method, args, kwargs):  # noqa: ANN001
        if method != "shutdown" and failure_counter["count"] == 0:
            failure_counter["count"] += 1
            raise sqlite3.OperationalError("simulated disk I/O error")
        return original_dispatch(state, method=method, args=args, kwargs=kwargs)

    monkeypatch.setattr(state_writer_module, "dispatch_state_rpc", flaky_dispatch)

    thread = threading.Thread(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port)},
        daemon=True,
    )
    thread.start()
    _wait_for_tcp(host, port)

    remote = RemoteControlState(path=db_path, tcp_host=host, tcp_port=port)
    ts = format_utc(now_utc())

    with pytest.raises(RuntimeError, match="simulated disk I/O error"):
        remote.start_run(run_id="first_failure", job_name="unit", started_at_utc=ts, params={"k": 1})

    remote.start_run(run_id="after_failure", job_name="unit", started_at_utc=ts, params={"k": 2})
    remote.commit()

    remote._rpc("shutdown")
    thread.join(timeout=5)
    assert not thread.is_alive()

    with ControlState.open_local(db_path) as local:
        run = local.conn.execute(
            "SELECT job_name FROM runs WHERE run_id=?",
            ("after_failure",),
        ).fetchone()
        assert run is not None
        assert str(run["job_name"]) == "unit"


def test_state_writer_serializes_selected_post_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    ts = format_utc(now_utc())
    with ControlState.open_local(db_path) as local:
        local.upsert_post_registry_many(
            post_uris=[
                PostUri("at://did:plc:a/app.bsky.feed.post/1"),
                PostUri("at://did:plc:a/app.bsky.feed.post/2"),
            ],
            seen_at_utc=ts,
        )
        local.commit()

    host = "127.0.0.1"
    port = _pick_free_tcp_port()
    thread = threading.Thread(
        target=run_state_writer,
        kwargs={"cfg": StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port)},
        daemon=True,
    )
    thread.start()
    _wait_for_tcp(host, port)

    remote = RemoteControlState(path=db_path, tcp_host=host, tcp_port=port)
    rows = remote.select_posts_to_backfill_rows(limit=10)
    assert rows == [
        {"post_uri": "at://did:plc:a/app.bsky.feed.post/1", "first_seen_utc": ts},
        {"post_uri": "at://did:plc:a/app.bsky.feed.post/2", "first_seen_utc": ts},
    ]
    assert remote._rpc("ping") == {"status": "ok"}

    remote._rpc("shutdown")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_state_writer_survives_unserializable_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    host = "127.0.0.1"
    port = _pick_free_tcp_port()
    thread_errors: list[Exception] = []

    def _runner() -> None:
        try:
            run_state_writer(cfg=StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port))
        except Exception as err:  # noqa: BLE001
            thread_errors.append(err)

    def bad_dispatch(_state, *, method, args, kwargs):  # noqa: ANN001
        if method == "shutdown":
            return {"ok": True, "result": {"shutdown": True}}
        return {"ok": True, "result": object()}

    monkeypatch.setattr(state_writer_module, "dispatch_state_rpc", bad_dispatch)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    _wait_for_tcp(host, port)

    remote = RemoteControlState(path=db_path, tcp_host=host, tcp_port=port)
    with pytest.raises(RuntimeError, match="not JSON serializable"):
        remote._rpc("ping")

    assert thread.is_alive()
    assert not thread_errors

    remote._rpc("shutdown")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not thread_errors


def test_state_writer_survives_client_disconnect_during_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "control" / "control_state.db"
    host = "127.0.0.1"
    port = _pick_free_tcp_port()
    thread_errors: list[Exception] = []

    def _runner() -> None:
        try:
            run_state_writer(cfg=StateWriterConfig(db_path=db_path, tcp_host=host, tcp_port=port))
        except Exception as err:  # noqa: BLE001
            thread_errors.append(err)

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    _wait_for_tcp(host, port)

    remote = RemoteControlState(path=db_path, tcp_host=host, tcp_port=port)
    assert remote._rpc("ping") == {"status": "ok"}

    real_send = state_writer_module._send_json_line
    fail_once = {"pending": True}

    def _flaky_send(conn: socket.socket, obj: dict[str, object]) -> None:
        if fail_once["pending"]:
            fail_once["pending"] = False
            raise ConnectionResetError(10054, "simulated client reset")
        real_send(conn, obj)

    monkeypatch.setattr(state_writer_module, "_send_json_line", _flaky_send)

    with pytest.raises((RuntimeError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
        remote._rpc("ping")

    assert thread.is_alive()
    assert remote._rpc("ping") == {"status": "ok"}
    assert not thread_errors

    remote._rpc("shutdown")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not thread_errors
