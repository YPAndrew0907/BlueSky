from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MonitorConfig:
    out_dir: Path
    interval_s: float
    python_exe: str
    auth_mode: str
    rps: float | None
    max_retries: int | None
    log_level: str
    stale_seconds: float
    restart_cooldown_s: float


@dataclass(frozen=True)
class RunState:
    run_id: str | None
    started_at_utc: str | None
    finished_at_utc: str | None
    feed_generators: int
    actor_processed: int
    starterpacks: int
    starterpack_feeds: int
    starterpack_actor_processed: int
    popular_feeds: int
    feed_panel: int
    feed_snapshot_status: int
    snapshots_success: int
    feed_items: int
    posts: int
    post_labels: int
    authors: int
    last_error: str | None
    validation_fails: int


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PID exists but we cannot signal it; treat as alive.
        return True
    # Treat zombie processes as not alive. This happens when the monitor is the parent process and
    # doesn't reap a finished child.
    try:
        stat = subprocess.check_output(["ps", "-p", str(pid), "-o", "stat="], text=True).strip()
        if stat.startswith("Z"):
            return False
    except Exception:
        # Best-effort only; fall back to the os.kill check.
        pass
    return True


def _terminate_pid(pid: int, *, timeout_s: float = 15.0) -> bool:
    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return False
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.5)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text("utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid) + "\n", encoding="utf-8")


def _connect_state_db(out_dir: Path) -> sqlite3.Connection:
    db_path = out_dir / "state" / "state.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"]) if row is not None else 0


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    v = row["value"]
    return str(v) if v is not None else None


def _read_run_state(out_dir: Path) -> RunState:
    conn = _connect_state_db(out_dir)
    try:
        run_id = _get_meta(conn, "run_id")
        started_at = _get_meta(conn, "started_at_utc")
        finished_at = _get_meta(conn, "finished_at_utc")

        feed_snapshot_status = _count(conn, "feed_snapshot_status")
        snapshots_success_row = conn.execute("SELECT COUNT(*) AS n FROM feed_snapshot_status WHERE success = 1").fetchone()
        snapshots_success = int(snapshots_success_row["n"]) if snapshots_success_row is not None else 0

        last_error_row = conn.execute("SELECT stage, key, error_type, http_status, when_utc FROM errors ORDER BY id DESC LIMIT 1").fetchone()
        last_error = None
        if last_error_row is not None:
            last_error = (
                f"{last_error_row['when_utc']} stage={last_error_row['stage']} key={last_error_row['key']} "
                f"type={last_error_row['error_type']} status={last_error_row['http_status']}"
            )

        fails_row = conn.execute("SELECT COUNT(*) AS n FROM validations WHERE status = 'FAIL'").fetchone()
        validation_fails = int(fails_row["n"]) if fails_row is not None else 0

        return RunState(
            run_id=run_id,
            started_at_utc=started_at,
            finished_at_utc=finished_at,
            feed_generators=_count(conn, "feed_generators"),
            actor_processed=_count(conn, "actor_processed"),
            starterpacks=_count(conn, "starterpacks"),
            starterpack_feeds=_count(conn, "starterpack_feeds"),
            starterpack_actor_processed=_count(conn, "starterpack_actor_processed"),
            popular_feeds=_count(conn, "popular_feeds"),
            feed_panel=_count(conn, "feed_panel"),
            feed_snapshot_status=feed_snapshot_status,
            snapshots_success=snapshots_success,
            feed_items=_count(conn, "feed_items"),
            posts=_count(conn, "posts"),
            post_labels=_count(conn, "post_labels"),
            authors=_count(conn, "authors"),
            last_error=last_error,
            validation_fails=validation_fails,
        )
    finally:
        conn.close()


def _deliverables_complete(out_dir: Path) -> bool:
    csv_dir = out_dir / "csv"
    required = [
        "run_metadata.csv",
        "run_summary.csv",
        "errors.csv",
        "feed_generators_index.csv",
        "starterpacks.csv",
        "starterpack_feeds.csv",
        "discovery_feed_inclusions.csv",
        "popular_feeds.csv",
        "feed_panel.csv",
        "feed_snapshot_status.csv",
        "feed_items.csv.gz",
        "posts.csv.gz",
        "post_labels.csv.gz",
        "authors.csv.gz",
        "provider_stats.csv",
        "validation_report.csv",
        "data_dictionary.csv",
        "manifest.csv",
        "http_stats.csv",
    ]
    for name in required:
        p = csv_dir / name
        if not p.exists():
            return False
        try:
            if p.stat().st_size <= 0:
                return False
        except FileNotFoundError:
            return False
    # Validation must be all PASS.
    try:
        text = (csv_dir / "validation_report.csv").read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return False
    return "FAIL" not in text


def _format_state_line(s: RunState) -> str:
    return (
        f"run_id={s.run_id} finished_at={s.finished_at_utc or ''} "
        f"index actors={s.actor_processed} feeds={s.feed_generators} "
        f"starterpacks actors={s.starterpack_actor_processed} packs={s.starterpacks} feed_slots={s.starterpack_feeds} "
        f"popular={s.popular_feeds} panel={s.feed_panel} "
        f"snapshots={s.snapshots_success}/{s.feed_snapshot_status} items={s.feed_items} posts={s.posts} "
        f"authors={s.authors} fails={s.validation_fails}"
    )


def _append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    try:
        print(line.rstrip(), flush=True)
    except BrokenPipeError:
        pass


def _start_run(cfg: MonitorConfig) -> subprocess.Popen[bytes]:
    cmd: list[str] = [
        cfg.python_exe,
        "-m",
        "bsky_fair_collect",
        "run-all",
        "--out-dir",
        str(cfg.out_dir),
        "--auth-mode",
        cfg.auth_mode,
        "--log-level",
        cfg.log_level,
    ]
    if cfg.rps is not None:
        cmd.extend(["--rps", str(cfg.rps)])
    if cfg.max_retries is not None:
        cmd.extend(["--max-retries", str(cfg.max_retries)])

    project_root = Path(__file__).resolve().parents[1]
    return subprocess.Popen(
        cmd,
        cwd=project_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def monitor_forever(cfg: MonitorConfig) -> None:
    out_dir = cfg.out_dir
    pid_path = out_dir / "pid.txt"
    monitor_pid_path = out_dir / "monitor_pid.txt"
    monitor_log = out_dir / "logs" / "monitor.log"

    _write_pid(monitor_pid_path, os.getpid())
    _append_log(monitor_log, f"monitor_start pid={os.getpid()} interval_s={cfg.interval_s}")

    proc: subprocess.Popen[bytes] | None = None
    last_progress_at = time.time()
    last_progress_key: tuple[object, ...] | None = None
    last_restart_at = 0.0

    while True:
        try:
            if _deliverables_complete(out_dir):
                _append_log(monitor_log, "deliverables_complete status=done")
                return

            if proc is not None and proc.poll() is not None:
                proc = None

            pid = proc.pid if proc is not None else _read_pid(pid_path)
            alive = (pid is not None) and _pid_alive(pid)

            now = time.time()
            can_restart = (now - last_restart_at) >= cfg.restart_cooldown_s

            if not alive:
                if can_restart:
                    _append_log(monitor_log, f"run_not_alive pid={pid} action=restart")
                    proc = _start_run(cfg)
                    _write_pid(pid_path, int(proc.pid))
                    last_restart_at = time.time()
                    _append_log(monitor_log, f"run_restarted pid={int(proc.pid)}")
                else:
                    _append_log(
                        monitor_log,
                        f"run_not_alive pid={pid} action=skip_restart reason=cooldown remaining_s={cfg.restart_cooldown_s - (now - last_restart_at):.1f}",
                    )

            try:
                s = _read_run_state(out_dir)
                line = _format_state_line(s)
                _append_log(monitor_log, line)

                progress_key = (
                    s.finished_at_utc,
                    s.actor_processed,
                    s.feed_generators,
                    s.starterpack_actor_processed,
                    s.starterpacks,
                    s.starterpack_feeds,
                    s.popular_feeds,
                    s.feed_panel,
                    s.feed_snapshot_status,
                    s.snapshots_success,
                    s.feed_items,
                    s.posts,
                    s.post_labels,
                    s.authors,
                )
                if last_progress_key is None or progress_key != last_progress_key:
                    last_progress_key = progress_key
                    last_progress_at = time.time()

                stale_s = time.time() - last_progress_at
                if alive and stale_s >= cfg.stale_seconds and can_restart:
                    _append_log(
                        monitor_log,
                        f"run_stale pid={pid} stale_s={stale_s:.0f} action=restart",
                    )
                    if pid is not None:
                        _terminate_pid(pid)
                    proc = _start_run(cfg)
                    _write_pid(pid_path, int(proc.pid))
                    last_restart_at = time.time()
                    last_progress_at = last_restart_at
                    _append_log(monitor_log, f"run_restarted pid={int(proc.pid)} reason=stale")
            except Exception as err:  # noqa: BLE001
                _append_log(monitor_log, f"monitor_error err={err!r}")
        except Exception as err:  # noqa: BLE001
            _append_log(monitor_log, f"monitor_loop_error err={err!r}")

        time.sleep(cfg.interval_s)


def _parse_args(argv: list[str]) -> MonitorConfig:
    p = argparse.ArgumentParser(prog="monitor_run.py")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--interval-seconds", type=float, default=30.0)
    p.add_argument("--python", default="python3.11", help="Python executable used to (re)start the run.")
    p.add_argument("--auth-mode", default="unauth", choices=["unauth", "auth", "both"])
    p.add_argument("--rps", type=float, default=None)
    p.add_argument("--max-retries", type=int, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--stale-seconds", type=float, default=900.0, help="Restart the run if no counters change for this long.")
    p.add_argument("--restart-cooldown-seconds", type=float, default=60.0, help="Minimum time between restarts.")
    args = p.parse_args(argv)
    return MonitorConfig(
        out_dir=args.out_dir,
        interval_s=float(args.interval_seconds),
        python_exe=str(args.python),
        auth_mode=str(args.auth_mode),
        rps=float(args.rps) if args.rps is not None else None,
        max_retries=int(args.max_retries) if args.max_retries is not None else None,
        log_level=str(args.log_level),
        stale_seconds=float(args.stale_seconds),
        restart_cooldown_s=float(args.restart_cooldown_seconds),
    )


def main(argv: list[str] | None = None) -> None:
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)
    monitor_forever(cfg)


if __name__ == "__main__":
    main()
