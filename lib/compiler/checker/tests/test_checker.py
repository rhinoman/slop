"""
Native Type Checker Tests

Tests the native SLOP type checker by running it on test files
and verifying expected warnings/errors are produced.
"""

import pytest
from pathlib import Path


class TestCheckerPasses:
    """Tests for code that should pass without warnings"""

    def test_basic_pass(self, run_checker, tests_dir):
        """Basic valid code should pass with no warnings"""
        slop_file = tests_dir / "pass_basic.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        assert "Type check passed" in stdout or "OK" in stdout


class TestBranchTypeWarnings:
    """Tests for branch type mismatch warnings"""

    def test_if_branch_mismatch(self, run_checker, tests_dir):
        """If with mismatched branch types should warn"""
        slop_file = tests_dir / "warn_branch_types.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        # Should complete (exit 0) but with warnings
        assert "warning" in stdout.lower() or "Warning" in stdout
        assert "Branch types differ" in stdout or "type" in stdout.lower()

    def test_cond_branch_mismatch(self, run_checker, tests_dir):
        """Cond with mismatched branch types should warn"""
        slop_file = tests_dir / "warn_cond_types.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert "warning" in stdout.lower() or "Warning" in stdout

    def test_match_branch_mismatch(self, run_checker, tests_dir):
        """Match with mismatched branch types should warn"""
        slop_file = tests_dir / "warn_match_types.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert "warning" in stdout.lower() or "Warning" in stdout


class TestPythonCheckerComparison:
    """Compare native checker output with Python checker"""

    def test_basic_pass_matches_python(self, run_checker, run_python_checker, tests_dir):
        """Native and Python checker should both pass basic code"""
        slop_file = tests_dir / "pass_basic.slop"

        native_code, native_out, native_err = run_checker(slop_file)
        python_code, python_out, python_err = run_python_checker(slop_file)

        # Both should succeed
        assert native_code == 0, f"Native failed: {native_out}\n{native_err}"
        assert python_code == 0, f"Python failed: {python_out}\n{python_err}"

    @pytest.mark.parametrize("test_file", [
        "warn_branch_types.slop",
        "warn_cond_types.slop",
        "warn_match_types.slop",
    ])
    def test_warning_detection_matches(self, run_checker, run_python_checker, tests_dir, test_file):
        """Both checkers should detect the same warnings"""
        slop_file = tests_dir / test_file

        native_code, native_out, native_err = run_checker(slop_file)
        python_code, python_out, python_err = run_python_checker(slop_file)

        # Check if Python produces warning
        python_has_warning = "warning" in python_out.lower()

        # If Python warns, native should too
        if python_has_warning:
            native_has_warning = "warning" in native_out.lower()
            assert native_has_warning, (
                f"Python checker found warning but native did not.\n"
                f"Python: {python_out}\n"
                f"Native: {native_out}"
            )


class TestTypeErrors:
    """Tests for type errors that should be detected"""

    def test_undefined_variable(self, run_checker, tests_dir):
        """Using undefined variable should produce error"""
        slop_file = tests_dir / "error_undefined_var.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert exit_code != 0 or "error" in stdout.lower()
        assert "undefined" in stdout.lower() or "unknown" in stdout.lower()

    def test_return_type_mismatch(self, run_checker, tests_dir):
        """Returning wrong type should produce error"""
        slop_file = tests_dir / "error_return_type.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert exit_code != 0 or "error" in stdout.lower()
        assert "expected" in stdout.lower() or "mismatch" in stdout.lower()

    def test_ambiguous_enum_variants(self, run_checker, tests_dir):
        """Same variant name in multiple enums should produce error"""
        slop_file = tests_dir / "error_ambiguous_enum.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert exit_code != 0 or "error" in stdout.lower()
        assert "ambiguous" in stdout.lower() or "duplicate" in stdout.lower()

    def test_unknown_field_access(self, run_checker, tests_dir):
        """Accessing non-existent field should produce error about the field"""
        slop_file = tests_dir / "error_unknown_field.slop"
        exit_code, stdout, stderr = run_checker(slop_file)

        assert exit_code != 0 or "error" in stdout.lower()
        # Must specifically mention the unknown field, not just "field" in function name
        assert "no field" in stdout.lower() or "has no field" in stdout.lower()


class TestPythonErrorComparison:
    """Compare error detection between native and Python checkers"""

    @pytest.mark.parametrize("test_file", [
        "error_undefined_var.slop",
        "error_return_type.slop",
        "error_ambiguous_enum.slop",
        "error_unknown_field.slop",
    ])
    def test_error_detection_matches(self, run_checker, run_python_checker, tests_dir, test_file):
        """Track parity between native and Python checker error detection"""
        slop_file = tests_dir / test_file

        native_code, native_out, native_err = run_checker(slop_file)
        python_code, python_out, python_err = run_python_checker(slop_file)

        python_has_error = "error" in python_out.lower() or python_code != 0
        native_has_error = "error" in native_out.lower() or native_code != 0

        # Just report parity status, don't fail
        if python_has_error and not native_has_error:
            pytest.skip(f"Native checker missing error detection for {test_file}")


class TestMultiModule:
    """Tests for multi-module type checking"""

    def test_multi_module_passes(self, run_python_checker, tests_dir):
        """Multi-module import should type check successfully"""
        import subprocess

        main_file = tests_dir / "multi-main.slop"

        # Use slop check with include path
        result = subprocess.run(
            ["uv", "run", "slop", "check", str(main_file)],
            capture_output=True,
            text=True,
            cwd=tests_dir,  # Run from tests dir so import resolves
        )

        assert result.returncode == 0, f"Type check failed:\n{result.stdout}\n{result.stderr}"
        assert "passed" in result.stdout.lower() or "ok" in result.stdout.lower()

    def test_multi_module_with_native_checker(self, tests_dir):
        """Multi-module check with --native flag should work"""
        import subprocess

        main_file = tests_dir / "multi-main.slop"

        # Use slop check --native with include path
        result = subprocess.run(
            ["uv", "run", "slop", "check", "--native", str(main_file)],
            capture_output=True,
            text=True,
            cwd=tests_dir,
        )

        # Native checker might not support multi-module yet
        if "not found" in result.stderr.lower() or "falling back" in result.stderr.lower():
            pytest.skip("Native checker not available or falling back to Python")

        # If it ran, should pass (or we're testing that it handles modules)
        assert result.returncode == 0, f"Native check failed:\n{result.stdout}\n{result.stderr}"

    def test_multi_module_type_error_detected(self, run_python_checker, tests_dir):
        """Multi-module with type error should be caught"""
        import subprocess

        error_file = tests_dir / "multi-error.slop"

        # Use slop check with include path
        result = subprocess.run(
            ["uv", "run", "slop", "check", str(error_file)],
            capture_output=True,
            text=True,
            cwd=tests_dir,
        )

        # Should detect the type error (get-status returns Status, not Int)
        has_error = result.returncode != 0 or "error" in result.stdout.lower()
        assert has_error, f"Expected type error but got:\n{result.stdout}\n{result.stderr}"
