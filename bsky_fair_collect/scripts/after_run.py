from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AfterRunConfig:
    out_dir: Path
    interval_s: float
    python_exe: str
    backfill_mode: str  # auto|always|never
    postprocess: bool
    postprocess_dest_dir: Path | None
    postprocess_overwrite: bool
    postprocess_zip: bool
    log_level: str


def _append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    try:
        print(line.rstrip(), flush=True)
    except BrokenPipeError:
        pass


def _read_finished_at(out_dir: Path) -> str | None:
    db_path = out_dir / "state" / "state.db"
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='finished_at_utc'").fetchone()
        if row is None:
            return None
        v = row[0]
        v_str = str(v) if v is not None else ""
        return v_str if v_str else None
    finally:
        conn.close()


def _validation_ok(out_dir: Path) -> bool:
    path = out_dir / "csv" / "validation_report.csv"
    try:
        text = path.read_text("utf-8", errors="replace")
    except FileNotFoundError:
        return False
    return "FAIL" not in text


def _needs_backfill(out_dir: Path) -> bool:
    db_path = out_dir / "state" / "state.db"
    if not db_path.exists():
        return False
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM feed_panel
            WHERE provider_bucket IS NULL OR provider_bucket='unknown' OR service_did IS NULL OR display_name IS NULL
            """
        ).fetchone()
        return bool(row[0]) if row is not None else False
    finally:
        conn.close()


def _run_cmd(log_path: Path, *, cwd: Path, cmd: list[str]) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n# cmd: {' '.join(cmd)}\n")
        f.flush()
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=f, start_new_session=True, text=True)
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"command failed exit={ret}: {' '.join(cmd)}")


def wait_and_postprocess(cfg: AfterRunConfig) -> None:
    out_dir = cfg.out_dir
    log_path = out_dir / "logs" / "post_run.log"
    pid_path = out_dir / "post_run_pid.txt"
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")

    _append_log(
        log_path,
        (
            f"post_run_start pid={os.getpid()} interval_s={cfg.interval_s} "
            f"backfill_mode={cfg.backfill_mode} postprocess={int(cfg.postprocess)}"
        ),
    )

    while True:
        finished_at = _read_finished_at(out_dir)
        ok = _validation_ok(out_dir)
        if finished_at and ok:
            _append_log(log_path, f"run_complete finished_at_utc={finished_at}")
            break
        time.sleep(cfg.interval_s)

    project_root = Path(__file__).resolve().parents[1]

    do_backfill = False
    if cfg.backfill_mode == "always":
        do_backfill = True
    elif cfg.backfill_mode == "auto":
        do_backfill = _needs_backfill(out_dir)

    if do_backfill:
        _append_log(log_path, "post_run_step name=backfill start")
        _run_cmd(
            log_path,
            cwd=project_root,
            cmd=[
                cfg.python_exe,
                "-m",
                "bsky_fair_collect",
                "backfill",
                "--out-dir",
                str(out_dir),
                "--log-level",
                cfg.log_level,
            ],
        )
        _append_log(log_path, "post_run_step name=backfill done")
    else:
        _append_log(log_path, "post_run_step name=backfill skipped")

    if cfg.postprocess:
        _append_log(log_path, "post_run_step name=postprocess start")
        cmd = [
            cfg.python_exe,
            "-m",
            "bsky_fair_collect",
            "postprocess",
            "--out-dir",
            str(out_dir),
            "--log-level",
            cfg.log_level,
        ]
        if cfg.postprocess_dest_dir is not None:
            cmd.extend(["--dest-dir", str(cfg.postprocess_dest_dir)])
        if cfg.postprocess_overwrite:
            cmd.append("--overwrite")
        if cfg.postprocess_zip:
            cmd.append("--zip")
        _run_cmd(log_path, cwd=project_root, cmd=cmd)
        _append_log(log_path, "post_run_step name=postprocess done")

    _append_log(log_path, "post_run_done")


def _parse_args(argv: list[str]) -> AfterRunConfig:
    p = argparse.ArgumentParser(prog="after_run.py")
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--interval-seconds", type=float, default=60.0)
    p.add_argument("--python", default="python3.11", help="Python executable used to run post-run steps.")
    p.add_argument("--backfill", default="auto", choices=["auto", "always", "never"])
    p.add_argument("--postprocess", action="store_true", help="Run postprocess after completion.")
    p.add_argument("--postprocess-dest-dir", type=Path, default=None)
    p.add_argument("--postprocess-overwrite", action="store_true")
    p.add_argument("--postprocess-zip", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)
    out_dir = Path(args.out_dir).expanduser().resolve()
    return AfterRunConfig(
        out_dir=out_dir,
        interval_s=float(args.interval_seconds),
        python_exe=str(args.python),
        backfill_mode=str(args.backfill),
        postprocess=bool(args.postprocess),
        postprocess_dest_dir=(Path(args.postprocess_dest_dir) if args.postprocess_dest_dir is not None else None),
        postprocess_overwrite=bool(args.postprocess_overwrite),
        postprocess_zip=bool(args.postprocess_zip),
        log_level=str(args.log_level),
    )


def main(argv: list[str] | None = None) -> None:
    cfg = _parse_args(sys.argv[1:] if argv is None else argv)
    wait_and_postprocess(cfg)


if __name__ == "__main__":
    main()
