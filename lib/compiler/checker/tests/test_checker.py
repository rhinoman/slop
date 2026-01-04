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
