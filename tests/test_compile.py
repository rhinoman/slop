"""
Compile and Run Tests for SLOP Transpiler

These tests verify that transpiled C code actually compiles
with a real C compiler and executes correctly.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from slop.transpiler import transpile


def has_c_compiler():
    """Check if a C compiler is available"""
    return shutil.which("cc") is not None or shutil.which("gcc") is not None


@pytest.fixture
def c_compiler():
    """Get the C compiler command, or skip if none available"""
    if shutil.which("cc"):
        return "cc"
    elif shutil.which("gcc"):
        return "gcc"
    else:
        pytest.skip("No C compiler available")


@pytest.fixture
def runtime_path():
    """Path to the SLOP runtime header"""
    return Path(__file__).parent.parent / "src" / "slop" / "runtime"


class TestCompileAndRun:
    """Test that transpiled C code compiles and runs correctly"""

    def test_runtime_test_compiles_and_runs(self, c_compiler, runtime_path):
        """Transpile, compile, and run runtime_test.slop"""
        # Read SLOP source
        slop_path = Path(__file__).parent / "runtime_test.slop"
        source = slop_path.read_text()

        # Transpile to C
        c_code = transpile(source)

        # Write to temp file and compile
        with tempfile.TemporaryDirectory() as tmpdir:
            c_file = Path(tmpdir) / "test.c"
            exe_file = Path(tmpdir) / "test"
            c_file.write_text(c_code)

            # Compile
            compile_result = subprocess.run(
                [c_compiler, "-O2", f"-I{runtime_path}", "-o", str(exe_file), str(c_file)],
                capture_output=True,
                text=True,
            )

            if compile_result.returncode != 0:
                # Print the generated C for debugging
                print("=== Generated C code ===")
                for i, line in enumerate(c_code.split("\n"), 1):
                    print(f"{i:4}: {line}")
                print("=== Compiler errors ===")
                print(compile_result.stderr)
                pytest.fail(f"Compilation failed with exit code {compile_result.returncode}")

            # Run
            run_result = subprocess.run(
                [str(exe_file)],
                capture_output=True,
                text=True,
            )

            if run_result.returncode != 0:
                pytest.fail(
                    f"Runtime test failed with exit code {run_result.returncode}\n"
                    f"stdout: {run_result.stdout}\n"
                    f"stderr: {run_result.stderr}"
                )

    def test_hello_world_compiles_and_runs(self, c_compiler, runtime_path):
        """Transpile, compile, and run hello.slop example"""
        # Read SLOP source
        slop_path = Path(__file__).parent.parent / "examples" / "hello.slop"
        source = slop_path.read_text()

        # Transpile to C
        c_code = transpile(source)

        # Write to temp file and compile
        with tempfile.TemporaryDirectory() as tmpdir:
            c_file = Path(tmpdir) / "hello.c"
            exe_file = Path(tmpdir) / "hello"
            c_file.write_text(c_code)

            # Compile
            compile_result = subprocess.run(
                [c_compiler, "-O2", f"-I{runtime_path}", "-o", str(exe_file), str(c_file)],
                capture_output=True,
                text=True,
            )

            if compile_result.returncode != 0:
                print("=== Generated C code ===")
                for i, line in enumerate(c_code.split("\n"), 1):
                    print(f"{i:4}: {line}")
                print("=== Compiler errors ===")
                print(compile_result.stderr)
                pytest.fail(f"Compilation failed with exit code {compile_result.returncode}")

            # Run
            run_result = subprocess.run(
                [str(exe_file)],
                capture_output=True,
                text=True,
            )

            # hello.slop should return 0 and print "Hello, World!"
            assert run_result.returncode == 0
            assert "Hello" in run_result.stdout
