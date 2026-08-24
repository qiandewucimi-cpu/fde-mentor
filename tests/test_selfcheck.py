from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import venv
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "selfcheck.py"


class SelfcheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="fde-selfcheck-tests-")
        self.root = Path(self.tempdir.name)
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            assert value is TODO
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )
        self.answer = self.write_script(
            "answer.py",
            """
            assert 1 + 1 == 2
            print("ANSWER_OK")
            """,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_script(self, name: str, source: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        return path

    def run_checker(
        self,
        *extra: str,
        pythonioencoding: str = "utf-8",
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-X",
            "utf8",
            str(CHECKER),
            "--practice",
            str(self.practice),
            "--answer",
            str(self.answer),
            *extra,
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = pythonioencoding
        env["PYTHONUTF8"] = "1"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

    def assert_failed_with(self, result: subprocess.CompletedProcess[str], code: str) -> None:
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(code, result.stdout + result.stderr)
        self.assertIn("SELF_CHECK_FAILED", result.stdout)

    def test_happy_path_uses_two_isolated_cwds(self) -> None:
        scenario = self.write_script(
            "场景答案.py",
            """
            assert {"status": "ready"}["status"] == "ready"
            print("SCENARIO_OK")
            """,
        )
        result = self.run_checker("--scenario-answer", str(scenario))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SELF_CHECK_OK files=3 cwd_runs=6", result.stdout)
        self.assertNotIn("__pycache__", "\n".join(str(p) for p in self.root.rglob("*")))

    def test_missing_practice_marker_fails(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            print("not finished")
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "PRACTICE_MARKER_MISSING")

    def test_answer_exception_fails(self) -> None:
        self.answer = self.write_script("answer.py", "raise RuntimeError('boom')\n")
        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_EXIT_NONZERO")

    def test_zero_exit_traceback_text_fails(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            print("PRACTICE_INCOMPLETE remaining=1")
            print("Traceback (most recent call last): hidden failure")
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "PRACTICE_TRACEBACK")

    def test_syntax_error_stops_execution(self) -> None:
        self.practice = self.write_script("practice.py", "if True print('bad')\n")
        result = self.run_checker()
        self.assert_failed_with(result, "SYNTAX_ERROR")
        self.assertNotIn("[PASS] execute", result.stdout)

    def test_one_explicit_cwd_is_configuration_error(self) -> None:
        cwd = self.root / "one-cwd"
        cwd.mkdir()
        result = self.run_checker("--cwd", str(cwd))

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("CONFIG_ERROR", result.stderr)

    def test_duplicate_input_files_are_rejected(self) -> None:
        self.answer = self.practice
        result = self.run_checker()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must be distinct", result.stderr)

    def test_relative_cwd_dependency_is_caught(self) -> None:
        (self.root / "payload.txt").write_text("local-only", encoding="utf-8")
        self.answer = self.write_script(
            "answer.py",
            """
            from pathlib import Path
            assert Path("payload.txt").read_text(encoding="utf-8") == "local-only"
            print("ANSWER_OK")
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_EXIT_NONZERO")

    def test_marker_on_stderr_does_not_pass(self) -> None:
        self.answer = self.write_script(
            "answer.py",
            """
            import sys
            print("ANSWER_OK", file=sys.stderr)
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_MARKER_MISSING")

    def test_practice_without_todo_sentinel_fails(self) -> None:
        self.practice = self.write_script(
            "practice.py", 'print("PRACTICE_INCOMPLETE remaining=1")\n'
        )
        result = self.run_checker()
        self.assert_failed_with(result, "PRACTICE_PLACEHOLDER_MISSING")

    def test_answer_with_unresolved_todo_fails(self) -> None:
        self.answer = self.write_script(
            "answer.py",
            """
            TODO = object()
            value = TODO
            print("ANSWER_OK")
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_PLACEHOLDER_PRESENT")

    def test_answer_marker_must_be_a_complete_line(self) -> None:
        self.answer = self.write_script("answer.py", 'print("NOT_ANSWER_OK")\n')
        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_MARKER_MISSING")

    def test_practice_remaining_count_must_be_positive(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            print("PRACTICE_INCOMPLETE remaining=0")
            """,
        )
        result = self.run_checker()
        self.assert_failed_with(result, "PRACTICE_MARKER_MISSING")

    def test_project_root_compiles_additional_python_files(self) -> None:
        self.write_script("helpers/broken.py", "if True print('bad')\n")
        result = self.run_checker("--project-root", str(self.root))
        self.assert_failed_with(result, "SYNTAX_ERROR")

    @unittest.skipUnless(os.name == "nt", "Windows paths are case-insensitive")
    def test_project_root_excludes_case_variant_virtualenv(self) -> None:
        self.write_script(".VENV/vendored.py", "if True print('bad')\n")

        result = self.run_checker("--project-root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_explicit_unicode_and_comma_cwds_are_supported(self) -> None:
        first = self.root / "目录,一"
        second = self.root / "目录 二"
        first.mkdir()
        second.mkdir()
        result = self.run_checker(
            "--cwd", str(first), "--cwd", str(second)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_output_limit_fails_without_unbounded_capture(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            print("x" * 20000)
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )
        result = self.run_checker("--max-output-bytes", "1024")
        self.assert_failed_with(result, "PRACTICE_OUTPUT_LIMIT")

    def test_timeout_fails_and_returns_promptly(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            import time
            TODO = object()
            value = TODO
            time.sleep(5)
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )
        result = self.run_checker("--timeout", "0.1")
        self.assert_failed_with(result, "PRACTICE_TIMEOUT")

    def test_gbk_parent_environment_does_not_break_unicode_paths(self) -> None:
        scenario = self.write_script(
            "中文场景.py", 'print("SCENARIO_OK")\n'
        )
        result = self.run_checker(
            "--scenario-answer",
            str(scenario),
            pythonioencoding="cp936",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_utf8_child_output_is_safely_decoded(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            import sys
            TODO = object()
            value = TODO
            sys.stdout.buffer.write(b"\\xff\\nPRACTICE_INCOMPLETE remaining=1\\n")
            """,
        )
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_targets_cannot_share_files_through_execution_cwd(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            from pathlib import Path
            TODO = object()
            value = TODO
            Path("seed.txt").write_text("practice", encoding="utf-8")
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )
        self.answer = self.write_script(
            "answer.py",
            """
            from pathlib import Path
            assert Path("seed.txt").read_text(encoding="utf-8") == "practice"
            print("ANSWER_OK")
            """,
        )

        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_EXIT_NONZERO")

    def test_pythonpath_and_sitecustomize_cannot_forge_markers(self) -> None:
        inject = self.root / "inject"
        inject.mkdir()
        (inject / "sitecustomize.py").write_text(
            'print("PRACTICE_INCOMPLETE remaining=1")\nprint("ANSWER_OK")\n',
            encoding="utf-8",
        )
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            value = TODO
            """,
        )
        self.answer = self.write_script("answer.py", "pass\n")

        result = self.run_checker(extra_env={"PYTHONPATH": str(inject)})
        self.assert_failed_with(result, "PRACTICE_MARKER_MISSING")

    def test_parent_sitecustomize_cannot_forge_overall_success(self) -> None:
        inject = self.root / "parent-inject"
        inject.mkdir()
        (inject / "sitecustomize.py").write_text(
            """
            import os
            import sys
            print("SELF_CHECK_OK files=2 cwd_runs=4")
            sys.stdout.flush()
            os._exit(0)
            """,
            encoding="utf-8",
        )
        self.practice = self.root / "missing-practice.py"

        result = self.run_checker(extra_env={"PYTHONPATH": str(inject)})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("CONFIG_ERROR", result.stderr)
        self.assertNotIn("SELF_CHECK_OK", result.stdout)

    def test_unisolated_checker_invocation_is_rejected(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--help"],
            cwd=str(REPO_ROOT),
            env={
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith("PYTHON")
            },
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("isolated Python flags", result.stderr)

    def test_nonzero_exit_is_not_masked_after_success_marker(self) -> None:
        self.answer = self.write_script(
            "answer.py",
            """
            print("ANSWER_OK")
            raise SystemExit(7)
            """,
        )

        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_EXIT_NONZERO")

    def test_nonfinite_timeouts_are_configuration_errors(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                result = self.run_checker(f"--timeout={value}")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("finite number", result.stderr)

    def test_successful_target_descendants_are_terminated(self) -> None:
        sentinel = self.root / "successful-descendant.txt"
        child = (
            "import time; print('READY', flush=True); time.sleep(0.8); "
            f"open({str(sentinel)!r}, 'w', encoding='utf-8').write('escaped')"
        )
        self.practice = self.write_script(
            "practice.py",
            f"""
            import subprocess
            import sys
            TODO = object()
            value = TODO
            descendant = subprocess.Popen(
                [sys.executable, "-c", {child!r}],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert descendant.stdout is not None
            assert descendant.stdout.readline().strip() == "READY"
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )

        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        time.sleep(1.0)
        self.assertFalse(sentinel.exists(), "successful child escaped containment")

    def test_timed_out_target_descendants_are_terminated(self) -> None:
        sentinel = self.root / "timed-out-descendant.txt"
        child = (
            "import time; print('READY', flush=True); time.sleep(1.5); "
            f"open({str(sentinel)!r}, 'w', encoding='utf-8').write('escaped')"
        )
        self.practice = self.write_script(
            "practice.py",
            f"""
            import subprocess
            import sys
            import time
            TODO = object()
            value = TODO
            descendant = subprocess.Popen(
                [sys.executable, "-c", {child!r}],
                stdout=subprocess.PIPE,
                text=True,
            )
            assert descendant.stdout is not None
            assert descendant.stdout.readline().strip() == "READY"
            time.sleep(5)
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )

        result = self.run_checker("--timeout", "1.0")
        self.assert_failed_with(result, "PRACTICE_TIMEOUT")
        time.sleep(1.7)
        self.assertFalse(sentinel.exists(), "timed-out child escaped containment")

    def test_pep263_encoded_project_file_compiles(self) -> None:
        helper = self.root / "legacy_encoding.py"
        helper.write_bytes(b"# -*- coding: cp1252 -*-\nlabel = 'caf\xe9'\n")

        result = self.run_checker("--project-root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_invalid_declared_encoding_fails_compilation(self) -> None:
        helper = self.root / "invalid_encoding.py"
        helper.write_bytes(b"# -*- coding: ascii -*-\nlabel = 'caf\xe9'\n")

        result = self.run_checker("--project-root", str(self.root))
        self.assert_failed_with(result, "SYNTAX_ERROR")

    @unittest.skipUnless(os.name == "nt", "Windows executable policy")
    def test_command_script_cannot_be_used_as_python(self) -> None:
        fake_python = self.root / "python.cmd"
        fake_python.write_text("@exit /b 0\n", encoding="utf-8")

        result = self.run_checker("--python", str(fake_python))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("native .exe", result.stderr)

    def test_practice_todo_only_in_dead_branch_fails(self) -> None:
        self.practice = self.write_script(
            "practice.py",
            """
            TODO = object()
            if False:
                print(TODO)
            print("PRACTICE_INCOMPLETE remaining=1")
            """,
        )

        result = self.run_checker()
        self.assert_failed_with(result, "PRACTICE_PLACEHOLDER_MISSING")

    def test_answer_dynamic_todo_lookup_fails(self) -> None:
        self.answer = self.write_script(
            "answer.py",
            """
            globals()["TODO"] = object()
            print("ANSWER_OK")
            """,
        )

        result = self.run_checker()
        self.assert_failed_with(result, "ANSWER_PLACEHOLDER_PRESENT")

    def test_project_root_snapshot_preserves_local_imports(self) -> None:
        self.write_script("helper.py", "VALUE = 42\n")
        self.answer = self.write_script(
            "answer.py",
            """
            from helper import VALUE
            assert VALUE == 42
            print("ANSWER_OK")
            """,
        )

        result = self.run_checker("--project-root", str(self.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_selected_virtualenv_site_packages_are_available(self) -> None:
        venv_root = self.root / "selected-venv"
        venv.EnvBuilder(with_pip=False).create(venv_root)
        if os.name == "nt":
            venv_python = venv_root / "Scripts" / "python.exe"
            site_packages = venv_root / "Lib" / "site-packages"
        else:
            venv_python = venv_root / "bin" / "python"
            version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            site_packages = venv_root / "lib" / version / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        (site_packages / "fde_venv_probe.py").write_text(
            "VALUE = 'selected-venv'\n", encoding="utf-8"
        )
        self.answer = self.write_script(
            "answer.py",
            """
            from fde_venv_probe import VALUE
            assert VALUE == "selected-venv"
            print("ANSWER_OK")
            """,
        )

        result = self.run_checker("--python", str(venv_python))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
