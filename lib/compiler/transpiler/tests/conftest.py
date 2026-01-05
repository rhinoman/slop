"""
Pytest fixtures for SLOP native transpiler tests
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
def transpiler_dir():
    """Path to the transpiler directory"""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def transpiler_binary(transpiler_dir):
    """Build and return path to the native transpiler binary"""
    binary_path = transpiler_dir / "slop-transpiler"

    # Build the transpiler if it doesn't exist or source is newer
    main_slop = transpiler_dir / "main.slop"
    if not binary_path.exists() or main_slop.stat().st_mtime > binary_path.stat().st_mtime:
        result = subprocess.run(
            ["uv", "run", "slop", "build"],
            cwd=transpiler_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(f"Failed to build transpiler:\n{result.stderr}\n{result.stdout}")

    if not binary_path.exists():
        pytest.skip("Could not build native transpiler")

    return binary_path


@pytest.fixture
def tests_dir():
    """Path to the transpiler tests directory"""
    return Path(__file__).parent


@pytest.fixture
def run_transpiler(transpiler_binary):
    """Fixture that returns a function to run the transpiler on a file"""
    def _run(slop_file: Path) -> tuple[int, str, str]:
        """Run transpiler and return (exit_code, stdout, stderr)"""
        result = subprocess.run(
            [str(transpiler_binary), str(slop_file)],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    return _run


@pytest.fixture
def run_python_transpiler():
    """Fixture that returns a function to run the Python transpiler on a file"""
    def _run(slop_file: Path) -> tuple[int, str, str]:
        """Run Python transpiler and return (exit_code, stdout, stderr)"""
        result = subprocess.run(
            ["uv", "run", "slop", "transpile", str(slop_file)],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr
    return _run


@pytest.fixture(scope="session")
def runtime_dir():
    """Path to the SLOP runtime directory"""
    return Path(__file__).parents[4] / "src" / "slop" / "runtime"


@pytest.fixture
def compile_and_run(runtime_dir):
    """Fixture that returns a function to compile C code and run it"""
    def _run(c_code: str) -> tuple[int, str, str]:
        """Compile C code, run it, and return (exit_code, stdout, stderr)"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            c_file = Path(tmpdir) / "test.c"
            binary = Path(tmpdir) / "test"

            # Write C code to file
            c_file.write_text(c_code)

            # Compile with runtime include path
            compile_result = subprocess.run(
                ["cc", "-o", str(binary), str(c_file), "-I", str(runtime_dir), "-lm"],
                capture_output=True,
                text=True,
            )
            if compile_result.returncode != 0:
                return -1, "", f"Compilation failed:\n{compile_result.stderr}"

            # Run
            run_result = subprocess.run(
                [str(binary)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return run_result.returncode, run_result.stdout, run_result.stderr

    return _run
