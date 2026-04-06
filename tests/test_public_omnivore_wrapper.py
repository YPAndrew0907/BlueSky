from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


def _write_shadow_package(root: Path) -> Path:
    package_dir = root / "bsky_collector_v2"
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "__main__.py").write_text(
        textwrap.dedent(
            """\
            from __future__ import annotations

            import os
            import sys
            from pathlib import Path


            def _append_line(path_raw: str, line: str) -> None:
                path = Path(path_raw)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\\n")


            args_log = os.environ.get("FAKE_PY_ARGS_LOG", "")
            if args_log:
                _append_line(args_log, " ".join(sys.argv[1:]))

            stdout_line = os.environ.get("FAKE_PY_STDOUT", "")
            if stdout_line:
                print(stdout_line)

            stderr_line = os.environ.get("FAKE_PY_STDERR", "")
            if stderr_line:
                print(stderr_line, file=sys.stderr)

            raise SystemExit(int(os.environ.get("FAKE_PY_EXIT_CODE", "0")))
            """
        ),
        encoding="utf-8",
    )
    return package_dir


def _resolve_bash() -> str:
    sh_path = shutil.which("sh")
    if sh_path:
        bash_candidate = str(Path(sh_path).with_name("bash.exe"))
        if Path(bash_candidate).exists():
            return bash_candidate
        return sh_path
    bash_path = shutil.which("bash")
    if bash_path:
        return bash_path
    raise RuntimeError("no shell found for collector_public_omnivore_daemon.sh test")


def _run_wrapper(tmp_path: Path, *, exit_code: int) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    repo_root = Path(__file__).resolve().parents[1]
    shadow_root = tmp_path / "shadow_root"
    _write_shadow_package(shadow_root)
    out_base = tmp_path / "data_v2_full"
    args_log = tmp_path / "fake_python_args.log"

    env = os.environ.copy()
    env.update(
        {
            "ROOT": str(shadow_root),
            "OUT_BASE": str(out_base),
            "PYTHON_BIN": sys.executable.replace("\\", "/"),
            "RUN_ONCE": "1",
            "INTERVAL_PUBLIC_OMNIBUS_S": "0",
            "LOOP_SLEEP_S": "0",
            "FAKE_PY_EXIT_CODE": str(exit_code),
            "FAKE_PY_STDERR": f"fake exit {exit_code}",
            "FAKE_PY_ARGS_LOG": str(args_log),
        }
    )

    proc = subprocess.run(
        [_resolve_bash(), str(repo_root / "scripts" / "collector_public_omnivore_daemon.sh")],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    daemon_log = out_base / "logs" / "manual_runs" / "collector_public_omnivore_daemon.log"
    cycle_logs = sorted((out_base / "logs" / "manual_runs").glob("public_omnivore_*.log"))
    assert len(cycle_logs) == 1
    return proc, daemon_log, cycle_logs[0], args_log


def test_public_omnivore_wrapper_propagates_failed_cycle_exit_code(tmp_path: Path) -> None:
    proc, daemon_log, cycle_log, args_log = _run_wrapper(tmp_path, exit_code=42)

    assert proc.returncode == 42
    daemon_text = daemon_log.read_text(encoding="utf-8")
    assert "starting public omnivore cycle" in daemon_text
    assert "public omnivore cycle failed exit_code=42" in daemon_text
    assert "completed public omnivore cycle" not in daemon_text

    cycle_text = cycle_log.read_text(encoding="utf-8")
    assert "COMMAND:" in cycle_text
    assert "fake exit 42" in cycle_text
    args_text = args_log.read_text(encoding="utf-8")
    assert "collect-public-omnibus" in args_text
    assert "--rq1-stage core" in args_text


def test_public_omnivore_wrapper_logs_completed_cycle_on_success(tmp_path: Path) -> None:
    proc, daemon_log, cycle_log, args_log = _run_wrapper(tmp_path, exit_code=0)

    assert proc.returncode == 0
    daemon_text = daemon_log.read_text(encoding="utf-8")
    assert "starting public omnivore cycle" in daemon_text
    assert "completed public omnivore cycle" in daemon_text
    assert "public omnivore cycle failed" not in daemon_text

    cycle_text = cycle_log.read_text(encoding="utf-8")
    assert "COMMAND:" in cycle_text
    assert "fake exit 0" in cycle_text
    args_text = args_log.read_text(encoding="utf-8")
    assert "collect-public-omnibus" in args_text
    assert "--rq1-stage core" in args_text
