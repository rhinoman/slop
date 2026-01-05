"""
Pytest fixtures for SLOP native type checker tests
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Add src to path for development
src_path = Path(__file__).parents[4] / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))


@pytest.fixture(scope="session")
def checker_dir():
    """Path to the checker directory"""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def checker_binary(checker_dir):
    """Build and return path to the native checker binary"""
    binary_path = checker_dir / "slop-checker"

    # Build the checker if it doesn't exist or source is newer
    main_slop = checker_dir / "main.slop"
    if not binary_path.exists() or main_slop.stat().st_mtime > binary_path.stat().st_mtime:
        result = subprocess.run(
            ["uv", "run", "slop", "build"],
            cwd=checker_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"Failed to build checker:\n{result.stderr}\n{result.stdout}")

    if not binary_path.exists():
        pytest.skip("Could not build native checker")

    return binary_path


@pytest.fixture
def tests_dir():
    """Path to the checker tests directory"""
    return Path(__file__).parent


@pytest.fixture
def run_checker(checker_binary):
    """Fixture that returns a function to run the checker on a file"""
    def _run(slop_file: Path) -> tuple[int, str, str]:
        """Run checker and return (exit_code, stdout, stderr)"""
        result = subprocess.run(
            [str(checker_binary), str(slop_file)],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    return _run


@pytest.fixture
def run_python_checker():
    """Fixture that returns a function to run the Python checker on a file"""
    def _run(slop_file: Path) -> tuple[int, str, str]:
        """Run Python checker and return (exit_code, stdout, stderr)"""
        result = subprocess.run(
            ["uv", "run", "slop", "check", str(slop_file)],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    return _run
