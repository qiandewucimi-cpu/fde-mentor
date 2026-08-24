#!/usr/bin/env python3
"""Validate trusted Python exercise artifacts in isolated working directories.

The checker compiles each input without writing bytecode, then executes the
unfinished practice, answer, and optional scenario answer from two distinct
temporary directories. It is intentionally strict about exit codes, success
markers, tracebacks, and timeouts.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


CONFIG_ERROR = 2
CHECK_FAILED = 1
TRACEBACK_MARKER = "Traceback (most recent call last)"
ISOLATED_INVOCATION = "python -I -S -B -X utf8 scripts/selfcheck.py"
COMPILE_SNIPPET = (
    "import sys, tokenize; "
    "p = sys.argv[1]; "
    "f = tokenize.open(p); source = f.read(); f.close(); "
    "compile(source, p, 'exec')"
)
RUN_SNIPPET = r"""
import os
import runpy
import sys
import sysconfig
from pathlib import Path

target = Path(sys.argv[1]).resolve()
sys.argv = [str(target)]

executable = Path(sys.executable)
venv_root = executable.parent.parent
venv_config = venv_root / "pyvenv.cfg"
site_paths = []
if venv_config.is_file():
    if os.name == "nt":
        site_paths.append(venv_root / "Lib" / "site-packages")
    else:
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site_paths.append(venv_root / "lib" / version / "site-packages")
    config = venv_config.read_text(encoding="utf-8", errors="replace")
    include_system = any(
        line.partition("=")[0].strip().lower() == "include-system-site-packages"
        and line.partition("=")[2].strip().lower() == "true"
        for line in config.splitlines()
    )
    if include_system:
        base_paths = sysconfig.get_paths(
            vars={"base": sys.base_prefix, "platbase": sys.base_exec_prefix}
        )
        site_paths.extend(
            Path(value)
            for value in (base_paths.get("purelib"), base_paths.get("platlib"))
            if value
        )
else:
    paths = sysconfig.get_paths()
    site_paths.extend(
        Path(value)
        for value in (paths.get("purelib"), paths.get("platlib"))
        if value
    )

for site_path in site_paths:
    value = str(site_path)
    if site_path.is_dir() and value not in sys.path:
        sys.path.append(value)
sys.path.insert(0, str(target.parent))
runpy.run_path(str(target), run_name="__main__")
"""
ISOLATION_WRAPPER_SNIPPET = (
    "import subprocess, sys; "
    "token = sys.stdin.buffer.read(1); "
    "token == b'1' or sys.exit(125); "
    "completed = subprocess.run(sys.argv[1:], stdin=subprocess.DEVNULL, "
    "check=False); "
    "raise SystemExit(completed.returncode)"
)
AST_CONTRACT_SNIPPET = r"""
import ast
import json
import sys
import tokenize

path = sys.argv[1]
stream = tokenize.open(path)
source = stream.read()
stream.close()
tree = ast.parse(source, filename=path)

class ContractVisitor(ast.NodeVisitor):
    def __init__(self):
        self.sentinel = 0
        self.todo_names = 0
        self.todo_strings = 0

    def visit_Assign(self, node):
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "TODO"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "object"
            and not node.value.args
            and not node.value.keywords
        ):
            self.sentinel += 1
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id == "TODO":
            self.todo_names += 1

    def visit_Constant(self, node):
        if node.value == "TODO":
            self.todo_strings += 1


class ReachableLoadVisitor(ast.NodeVisitor):
    def __init__(self):
        self.loads = 0

    def visit_Name(self, node):
        if node.id == "TODO" and isinstance(node.ctx, ast.Load):
            self.loads += 1

    def visit_If(self, node):
        if isinstance(node.test, ast.Constant):
            branch = node.body if bool(node.test.value) else node.orelse
            for child in branch:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_While(self, node):
        if isinstance(node.test, ast.Constant) and not bool(node.test.value):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

visitor = ContractVisitor()
visitor.visit(tree)
reachable = ReachableLoadVisitor()
reachable.visit(tree)
print(json.dumps({
    "sentinel": visitor.sentinel,
    "loads": reachable.loads,
    "todo_names": visitor.todo_names,
    "todo_strings": visitor.todo_strings,
}, separators=(",", ":")))
"""


@dataclass(frozen=True)
class Target:
    role: str
    path: Path
    expected_stdout: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    output_limited: bool = False
    launch_error: str | None = None


def configure_stdio() -> None:
    """Keep Chinese paths and child output printable on Windows consoles."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def has_required_launcher_flags() -> bool:
    """Reject launch modes where Python hooks can run before this script."""
    return bool(
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.dont_write_bytecode
        and sys.flags.utf8_mode
    )


def positive_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError(
            "timeout must be a finite number greater than zero"
        )
    return timeout


def positive_integer(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compile and execute trusted FDE exercise files from isolated "
            "working directories."
        )
    )
    parser.add_argument("--practice", required=True, help="Unfinished practice file")
    parser.add_argument("--answer", required=True, help="Completed answer file")
    parser.add_argument(
        "--scenario-answer",
        action="append",
        default=[],
        help="Optional Python scenario answer; repeat for multiple files",
    )
    parser.add_argument(
        "--project-root",
        help=(
            "Optional delivery code directory; all Python files below it are "
            "compiled and copied into each execution snapshot, while only "
            "explicit targets are executed. Without it, each snapshot contains "
            "only the target file."
        ),
    )
    parser.add_argument(
        "--cwd",
        action="append",
        default=[],
        help=(
            "Explicit scratch parent directory; repeat at least twice. A fresh "
            "working directory is created below each parent for every target. "
            "Omit to use two isolated temporary parents."
        ),
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable or command name (default: current interpreter)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=30.0,
        help="Per-process timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=1_000_000,
        help="Combined child output limit per process (default: 1000000)",
    )
    parser.add_argument(
        "--practice-expect",
        default="PRACTICE_INCOMPLETE",
        help=(
            "Required practice status token; stdout must contain the complete "
            "line '<token> remaining=<positive integer>'"
        ),
    )
    parser.add_argument(
        "--answer-expect",
        default="ANSWER_OK",
        help="Required complete status line in answer stdout",
    )
    parser.add_argument(
        "--scenario-expect",
        default="SCENARIO_OK",
        help="Required complete status line in scenario-answer stdout",
    )
    parser.add_argument(
        "--show-output",
        action="store_true",
        help=(
            "Include child stdout/stderr in failures. Output may contain local "
            "paths or exercise data, so enable only for trusted local debugging."
        ),
    )
    return parser.parse_args(argv)


def resolve_python(value: str) -> Path:
    candidate = Path(value).expanduser()
    has_path_hint = candidate.is_absolute() or any(
        separator in value for separator in (os.sep, os.altsep) if separator
    )
    if has_path_hint:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError("Python executable path is not a file")
        result = resolved
    else:
        located = shutil.which(value)
        if not located:
            raise ValueError(f"Python executable was not found: {Path(value).name}")
        result = Path(located).resolve(strict=True)

    if os.name == "nt" and result.suffix.lower() != ".exe":
        raise ValueError("Python executable must be a native .exe on Windows")
    if os.name != "nt" and not os.access(result, os.X_OK):
        raise ValueError("Python executable is not executable")
    return result


def resolve_file(value: str, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} does not exist: {Path(value).name}") from exc
    if not path.is_file():
        raise ValueError(f"{label} is not a file: {path.name}")
    return path


def resolve_cwds(values: Sequence[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                f"Working directory does not exist: {Path(value).name}"
            ) from exc
        if not path.is_dir():
            raise ValueError(
                f"Working directory is not a directory: {path.name}"
            )
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            resolved.append(path)
    if len(resolved) < 2:
        raise ValueError(
            "Provide at least two distinct --cwd values, or omit --cwd to use "
            "two isolated temporary directories."
        )
    return resolved


def resolve_project_root(value: str | None) -> Path | None:
    if value is None:
        return None
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(
            f"Project root does not exist: {Path(value).name}"
        ) from exc
    if not path.is_dir():
        raise ValueError(f"Project root is not a directory: {path.name}")
    return path


def child_environment() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
    }
    return env


class WindowsJob:
    """A non-inheritable Job Object that kills every member when closed."""

    KILL_ON_JOB_CLOSE = 0x00002000
    EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.UINT,
        ]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = handle
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = self.KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            self.EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        handle = self._wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        if not self._kernel32.AssignProcessToJobObject(self._handle, handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def terminate(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        handle = getattr(self, "_handle", None)
        if handle:
            self._kernel32.CloseHandle(handle)
            self._handle = None


def finish_contained_process(
    process: subprocess.Popen[bytes], job: WindowsJob | None
) -> None:
    """Terminate any remaining descendants on success, failure, or timeout."""
    if os.name == "nt":
        if job is not None:
            job.terminate()
            job.close()
        elif process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def read_limited(stream: object, limit: int) -> str:
    stream.seek(0)
    data = stream.read(limit)
    return data.decode("utf-8", errors="replace")


def run_command(
    command: Sequence[str], cwd: Path, timeout: float, max_output_bytes: int
) -> CommandResult:
    wrapped_command = [
        command[0],
        "-I",
        "-S",
        "-B",
        "-X",
        "utf8",
        "-c",
        ISOLATION_WRAPPER_SNIPPET,
        *command,
    ]
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if os.name == "nt"
        else 0
    )
    start_new_session = os.name != "nt"
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            process = subprocess.Popen(
                wrapped_command,
                cwd=str(cwd),
                env=child_environment(),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError as exc:
            return CommandResult(
                returncode=None,
                stdout="",
                stderr="",
                launch_error=str(exc),
            )

        job: WindowsJob | None = None
        if os.name == "nt":
            try:
                job = WindowsJob()
                job.assign(process)
            except OSError:
                if job is not None:
                    job.close()
                process.kill()
                process.wait()
                return CommandResult(
                    returncode=None,
                    stdout="",
                    stderr="",
                    launch_error="process isolation is unavailable",
                )

        try:
            if process.stdin is None:
                raise OSError("isolation handshake stdin is unavailable")
            process.stdin.write(b"1")
            process.stdin.close()
            process.stdin = None
        except OSError:
            finish_contained_process(process, job)
            return CommandResult(
                returncode=None,
                stdout="",
                stderr="",
                launch_error="process isolation handshake failed",
            )

        started = time.monotonic()
        timed_out = False
        output_limited = False
        while process.poll() is None:
            if time.monotonic() - started > timeout:
                timed_out = True
                break
            output_size = (
                os.fstat(stdout_file.fileno()).st_size
                + os.fstat(stderr_file.fileno()).st_size
            )
            if output_size > max_output_bytes:
                output_limited = True
                break
            time.sleep(0.02)

        finish_contained_process(process, job)
        returncode = process.returncode
        final_output_size = (
            os.fstat(stdout_file.fileno()).st_size
            + os.fstat(stderr_file.fileno()).st_size
        )
        if final_output_size > max_output_bytes:
            output_limited = True
        stdout = read_limited(stdout_file, max_output_bytes)
        remaining = max(0, max_output_bytes - len(stdout.encode("utf-8")))
        stderr = read_limited(stderr_file, remaining)
        return CommandResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_limited=output_limited,
        )


def excerpt(stdout: str, stderr: str, limit: int = 1200) -> str:
    combined = "\n".join(
        section.rstrip() for section in (stdout, stderr) if section.strip()
    )
    if not combined:
        return "(no output)"
    if len(combined) > limit:
        return combined[:limit] + "\n... output truncated ..."
    return combined


def status_key(role: str) -> str:
    return role.upper().replace("-", "_")


def marker_present(target: Target, stdout: str) -> bool:
    """Require a complete status line, not an accidental substring match."""
    lines = [line.strip() for line in stdout.splitlines()]
    if target.role == "practice":
        pattern = re.compile(
            rf"{re.escape(target.expected_stdout)} remaining=([1-9][0-9]*)"
        )
        return any(pattern.fullmatch(line) for line in lines)
    return target.expected_stdout in lines


def python_invocation(python: Path, snippet: str, *arguments: str) -> list[str]:
    return [
        str(python),
        "-I",
        "-S",
        "-B",
        "-X",
        "utf8",
        "-c",
        snippet,
        *arguments,
    ]


def check_placeholder_contract(
    python: Path,
    targets: Sequence[Target],
    cwd: Path,
    timeout: float,
    max_output_bytes: int,
    show_output: bool,
) -> list[str]:
    failures: list[str] = []
    for target in targets:
        result = run_command(
            python_invocation(
                python, AST_CONTRACT_SNIPPET, str(target.path)
            ),
            cwd,
            timeout,
            max_output_bytes,
        )
        key = status_key(target.role)
        if result.timed_out:
            code = f"{key}_CONTRACT_TIMEOUT role={target.role}"
        elif result.output_limited:
            code = f"{key}_CONTRACT_OUTPUT_LIMIT role={target.role}"
        elif result.launch_error:
            code = (
                f"{key}_CONTRACT_LAUNCH_ERROR role={target.role}: "
                f"{result.launch_error}"
            )
        elif result.returncode != 0:
            code = f"{key}_CONTRACT_ERROR role={target.role}"
            if show_output:
                code += "\n" + excerpt(result.stdout, result.stderr)
        else:
            try:
                lines = [line for line in result.stdout.splitlines() if line.strip()]
                contract = json.loads(lines[-1])
            except (IndexError, json.JSONDecodeError, TypeError):
                code = f"{key}_CONTRACT_RESULT_INVALID role={target.role}"
            else:
                if target.role == "practice" and not (
                    contract.get("sentinel") == 1
                    and isinstance(contract.get("loads"), int)
                    and contract["loads"] >= 1
                ):
                    code = "PRACTICE_PLACEHOLDER_MISSING role=practice"
                elif target.role != "practice" and (
                    contract.get("todo_names", 0) > 0
                    or contract.get("todo_strings", 0) > 0
                ):
                    code = f"{key}_PLACEHOLDER_PRESENT role={target.role}"
                else:
                    print(f"[PASS] contract role={target.role}")
                    continue
        print(f"[FAIL] {code}")
        failures.append(code)
    return failures


def discover_project_files(root: Path | None, targets: Sequence[Target]) -> list[Path]:
    paths = {target.path for target in targets}
    if root is None:
        return sorted(paths, key=lambda path: os.path.normcase(str(path)))
    excluded = {
        os.path.normcase(name)
        for name in {
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
        }
    }
    for path in root.rglob("*.py"):
        relative_parts = path.relative_to(root).parts
        if path.is_file() and not any(
            os.path.normcase(part) in excluded for part in relative_parts
        ):
            paths.add(path.resolve(strict=True))
    return sorted(paths, key=lambda path: os.path.normcase(str(path)))


def compile_targets(
    python: Path,
    paths: Sequence[Path],
    cwd: Path,
    timeout: float,
    max_output_bytes: int,
    show_output: bool,
) -> list[str]:
    failures: list[str] = []
    for path in paths:
        result = run_command(
            python_invocation(python, COMPILE_SNIPPET, str(path)),
            cwd,
            timeout,
            max_output_bytes,
        )
        if result.timed_out:
            code = f"SYNTAX_TIMEOUT file={path.name}"
        elif result.output_limited:
            code = f"SYNTAX_OUTPUT_LIMIT file={path.name}"
        elif result.launch_error:
            code = f"SYNTAX_LAUNCH_ERROR file={path.name}: {result.launch_error}"
        elif result.returncode != 0:
            code = f"SYNTAX_ERROR file={path.name}"
            if show_output:
                code += "\n" + excerpt(result.stdout, result.stderr)
        else:
            print(f"[PASS] syntax file={path.name}")
            continue
        print(f"[FAIL] {code}")
        failures.append(code)
    return failures


def stage_target(
    target: Target, project_root: Path | None, stage_root: Path
) -> Path:
    bundle = stage_root / "bundle"
    if project_root is None:
        bundle.mkdir()
        staged_target = bundle / target.path.name
        shutil.copy2(target.path, staged_target)
        return staged_target

    source_root = project_root
    relative_target = target.path.relative_to(source_root)
    try:
        bundle.relative_to(source_root)
    except ValueError:
        pass
    else:
        raise ValueError("staging directory must be outside project root")
    shutil.copytree(
        source_root,
        bundle,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git",
            ".hg",
            ".svn",
            ".venv",
            "venv",
            "__pycache__",
            "node_modules",
            "*.pyc",
            "*.pyo",
        ),
    )
    return bundle / relative_target


def execute_targets(
    python: Path,
    targets: Sequence[Target],
    scratch_parents: Sequence[Path],
    staging_parent: Path,
    project_root: Path | None,
    timeout: float,
    max_output_bytes: int,
    show_output: bool,
) -> list[str]:
    failures: list[str] = []
    for target in targets:
        key = status_key(target.role)
        for cwd_index, scratch_parent in enumerate(scratch_parents, start=1):
            prefix = f"role={target.role} cwd_index={cwd_index}"
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"fde-{target.role}-", dir=str(scratch_parent)
                ) as run_temp, tempfile.TemporaryDirectory(
                    prefix=f"fde-stage-{target.role}-", dir=str(staging_parent)
                ) as stage_temp:
                    run_root = Path(run_temp)
                    execution_cwd = run_root / "cwd"
                    execution_cwd.mkdir()
                    staged_target = stage_target(
                        target, project_root, Path(stage_temp)
                    )
                    result = run_command(
                        python_invocation(
                            python, RUN_SNIPPET, str(staged_target)
                        ),
                        execution_cwd,
                        timeout,
                        max_output_bytes,
                    )
            except (OSError, ValueError) as exc:
                code = f"{key}_ISOLATION_ERROR {prefix}: {type(exc).__name__}"
                print(f"[FAIL] {code}")
                failures.append(code)
                continue
            if result.timed_out:
                code = f"{key}_TIMEOUT {prefix}"
            elif result.output_limited:
                code = f"{key}_OUTPUT_LIMIT {prefix}"
            elif result.launch_error:
                code = f"{key}_LAUNCH_ERROR {prefix}: {result.launch_error}"
            elif result.returncode != 0:
                code = f"{key}_EXIT_NONZERO {prefix} rc={result.returncode}"
                if show_output:
                    code += "\n" + excerpt(result.stdout, result.stderr)
            elif TRACEBACK_MARKER in result.stdout or TRACEBACK_MARKER in result.stderr:
                code = f"{key}_TRACEBACK {prefix}"
                if show_output:
                    code += "\n" + excerpt(result.stdout, result.stderr)
            elif not marker_present(target, result.stdout):
                code = (
                    f"{key}_MARKER_MISSING {prefix} "
                    f"expected_stdout={target.expected_stdout!r}"
                )
                if show_output:
                    code += "\n" + excerpt(result.stdout, result.stderr)
            else:
                print(
                    f"[PASS] execute {prefix} "
                    f"marker={target.expected_stdout!r}"
                )
                continue
            print(f"[FAIL] {code}")
            failures.append(code)
    return failures


def build_targets(args: argparse.Namespace) -> list[Target]:
    for label, marker in (
        ("practice", args.practice_expect),
        ("answer", args.answer_expect),
        ("scenario", args.scenario_expect),
    ):
        if not marker:
            raise ValueError(f"{label} expected marker cannot be empty")

    targets = [
        Target(
            "practice",
            resolve_file(args.practice, "Practice file"),
            args.practice_expect,
        ),
        Target(
            "answer",
            resolve_file(args.answer, "Answer file"),
            args.answer_expect,
        ),
    ]
    for index, value in enumerate(args.scenario_answer, start=1):
        targets.append(
            Target(
                f"scenario-{index}",
                resolve_file(value, f"Scenario answer {index}"),
                args.scenario_expect,
            )
        )

    keys = [os.path.normcase(str(target.path)) for target in targets]
    if len(keys) != len(set(keys)):
        raise ValueError("Practice, answer, and scenario files must be distinct")
    return targets


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio()
    if not has_required_launcher_flags():
        print(
            "CONFIG_ERROR: launch this checker with isolated Python flags: "
            f"{ISOLATED_INVOCATION}",
            file=sys.stderr,
        )
        return CONFIG_ERROR
    args = parse_args(argv)
    try:
        python = resolve_python(args.python)
        targets = build_targets(args)
        project_root = resolve_project_root(args.project_root)
        if project_root is not None:
            for target in targets:
                try:
                    target.path.relative_to(project_root)
                except ValueError as exc:
                    raise ValueError(
                        f"{target.role} file must be inside project root"
                    ) from exc
        compile_paths = discover_project_files(project_root, targets)
        explicit_scratch_parents = resolve_cwds(args.cwd) if args.cwd else None
    except (OSError, ValueError) as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return CONFIG_ERROR

    with tempfile.TemporaryDirectory(prefix="fde-selfcheck-") as temp_root:
        isolated_root = Path(temp_root)
        compile_cwd = isolated_root / "compile"
        compile_cwd.mkdir()
        staging_parent = isolated_root / "staging"
        staging_parent.mkdir()
        if explicit_scratch_parents is None:
            scratch_parents = [
                isolated_root / "scratch-a",
                isolated_root / "scratch-b",
            ]
            for scratch_parent in scratch_parents:
                scratch_parent.mkdir()
        else:
            scratch_parents = explicit_scratch_parents

        failures = compile_targets(
            python,
            compile_paths,
            compile_cwd,
            args.timeout,
            args.max_output_bytes,
            args.show_output,
        )
        if not failures:
            failures.extend(
                check_placeholder_contract(
                    python,
                    targets,
                    compile_cwd,
                    args.timeout,
                    args.max_output_bytes,
                    args.show_output,
                )
            )
        if not failures:
            failures.extend(
                execute_targets(
                    python,
                    targets,
                    scratch_parents,
                    staging_parent,
                    project_root,
                    args.timeout,
                    args.max_output_bytes,
                    args.show_output,
                )
            )

    if failures:
        print(f"SELF_CHECK_FAILED failures={len(failures)}")
        return CHECK_FAILED

    print(
        f"SELF_CHECK_OK files={len(targets)} "
        f"cwd_runs={len(targets) * len(scratch_parents)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
