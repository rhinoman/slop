"""
Thread Library Tests

Tests that the Chan and Thread types are properly supported by the SLOP
type system and transpiler. Verifies that generated C code compiles correctly.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from slop.transpiler import Transpiler


class TestThreadTypes:
    """Test Chan and Thread type support"""

    def test_chan_type_parsing(self, thread_tests_path):
        """Verify that (Chan T) type syntax parses correctly"""
        from slop.parser import parse

        code = "(fn test ((ch (Ptr (Chan Int)))) (@spec (((Ptr (Chan Int))) -> Unit)) ch)"
        ast = parse(code)
        assert ast is not None

    def test_thread_type_parsing(self, thread_tests_path):
        """Verify that (Thread T) type syntax parses correctly"""
        from slop.parser import parse

        code = "(fn test ((th (Ptr (Thread Int)))) (@spec (((Ptr (Thread Int))) -> Unit)) th)"
        ast = parse(code)
        assert ast is not None

    def test_chan_type_checking(self, thread_tests_path):
        """Verify that (Chan T) type is recognized by type checker"""
        from slop.parser import parse
        from slop.type_checker import TypeChecker

        code = """
        (fn make-chan ((arena Arena))
          (@spec ((Arena) -> (Ptr (Chan Int))))
          (cast (Ptr (Chan Int)) nil))
        """
        ast = parse(code)
        checker = TypeChecker()
        diagnostics = checker.check_module(ast)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0, f"Type errors: {errors}"

    def test_thread_type_checking(self, thread_tests_path):
        """Verify that (Thread T) type is recognized by type checker"""
        from slop.parser import parse
        from slop.type_checker import TypeChecker

        code = """
        (fn make-thread ((arena Arena))
          (@spec ((Arena) -> (Ptr (Thread Int))))
          (cast (Ptr (Thread Int)) nil))
        """
        ast = parse(code)
        checker = TypeChecker()
        diagnostics = checker.check_module(ast)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0, f"Type errors: {errors}"

    def test_chan_transpilation(self, thread_tests_path):
        """Verify that (Chan T) generates correct channel struct"""
        from slop.parser import parse

        code = """
        (fn make-chan ((arena Arena))
          (@spec ((Arena) -> (Ptr (Chan Int))))
          (cast (Ptr (Chan Int)) nil))
        """
        ast = parse(code)
        transpiler = Transpiler()
        c_code = transpiler.transpile(ast)

        # Now emits direct struct definition instead of SLOP_CHAN_DEFINE macro
        assert "typedef struct slop_chan_int" in c_code
        assert "slop_chan_int*" in c_code

    def test_thread_transpilation(self, thread_tests_path):
        """Verify that (Thread T) generates correct thread struct and trampoline"""
        from slop.parser import parse

        code = """
        (fn make-thread ((arena Arena))
          (@spec ((Arena) -> (Ptr (Thread Int))))
          (cast (Ptr (Thread Int)) nil))
        """
        ast = parse(code)
        transpiler = Transpiler()
        c_code = transpiler.transpile(ast)

        # Now emits direct struct definition instead of SLOP_THREAD_DEFINE macro
        assert "typedef struct slop_thread_int" in c_code
        assert "slop_thread_int_entry" in c_code  # Trampoline function
        assert "slop_thread_int*" in c_code

    def test_thread_test_compiles(self, c_compiler, runtime_path, thread_lib_path, thread_tests_path):
        """Transpile, compile, and run thread_test.slop"""
        slop_path = thread_tests_path / "thread_test.slop"

        # Transpile
        from slop.parser import parse

        code = slop_path.read_text()
        ast = parse(code)
        transpiler = Transpiler()
        c_code = transpiler.transpile(ast)

        # Write to temp directory and compile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Write C file
            c_file = tmpdir_path / "thread_test.c"
            c_file.write_text(c_code)

            # No longer need slop_thread.h - struct definitions are now emitted
            # directly by the transpiler

            exe_file = tmpdir_path / "thread_test"

            # Compile with pthread and include paths
            compile_cmd = [
                c_compiler,
                "-O2",
                f"-I{runtime_path}",
                "-pthread",
                "-o",
                str(exe_file),
                str(c_file),
            ]

            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
            )

            if compile_result.returncode != 0:
                # Print the generated C for debugging
                print("=== Generated C ===")
                for i, line in enumerate(c_code.split("\n"), 1):
                    print(f"{i:4}: {line}")
                print("=== Compiler errors ===")
                print(compile_result.stderr)
                pytest.fail(
                    f"Compilation failed with exit code {compile_result.returncode}"
                )

            # Run
            run_result = subprocess.run(
                [str(exe_file)],
                capture_output=True,
                text=True,
            )

            if run_result.returncode != 0:
                pytest.fail(
                    f"Thread test failed with exit code {run_result.returncode}\n"
                    f"stdout: {run_result.stdout}\n"
                    f"stderr: {run_result.stderr}"
                )
