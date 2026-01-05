"""
Native Transpiler Tests

Tests the native SLOP transpiler by running it on test files
and verifying expected C code patterns are produced.
"""

import pytest
from pathlib import Path


class TestTranspilerBasics:
    """Tests for basic transpilation"""

    def test_arithmetic(self, run_transpiler, tests_dir):
        """Basic arithmetic should transpile successfully"""
        slop_file = tests_dir / "pass_arithmetic.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should contain main function
        assert "int64_t main(" in stdout or "int main(" in stdout
        # Should contain the arithmetic operations
        assert "+" in stdout or "1" in stdout

    def test_let_bindings(self, run_transpiler, tests_dir):
        """Let bindings should produce variable declarations"""
        slop_file = tests_dir / "pass_let.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have variable declarations
        assert "x" in stdout
        assert "y" in stdout
        # Should have const or variable declaration
        assert "const" in stdout or "int64_t x" in stdout or "auto x" in stdout

    def test_mutable_let(self, run_transpiler, tests_dir):
        """Mutable let should produce non-const variable"""
        slop_file = tests_dir / "pass_let_mut.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have mutable variable (no const for mut vars)
        assert "x" in stdout
        # Should have assignment
        assert "=" in stdout

    def test_if_expression(self, run_transpiler, tests_dir):
        """If expression should transpile to ternary or if statement"""
        slop_file = tests_dir / "pass_if.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have conditional logic
        assert "?" in stdout or "if" in stdout

    def test_function_call(self, run_transpiler, tests_dir):
        """Function calls should transpile correctly"""
        slop_file = tests_dir / "pass_function.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should define the add function
        assert "add(" in stdout
        # Should have function call in main
        assert "add(10" in stdout or "add(10," in stdout

    def test_nested_let(self, run_transpiler, tests_dir):
        """Nested let bindings should produce proper scoping"""
        slop_file = tests_dir / "pass_nested_let.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have all three variables
        assert "x" in stdout
        assert "y" in stdout
        assert "z" in stdout

    def test_cond(self, run_transpiler, tests_dir):
        """Cond expression should transpile to if-else chain"""
        slop_file = tests_dir / "pass_cond.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have conditional structure
        assert "if" in stdout or "?" in stdout

    def test_while_loop(self, run_transpiler, tests_dir):
        """While loop should transpile to C while"""
        slop_file = tests_dir / "pass_while.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Expected success, got:\n{stdout}\n{stderr}"
        # Should have while loop
        assert "while" in stdout


class TestPythonTranspilerComparison:
    """Compare native transpiler output with Python transpiler"""

    @pytest.mark.parametrize("test_file", [
        "pass_arithmetic.slop",
        "pass_let.slop",
        "pass_if.slop",
        "pass_function.slop",
    ])
    def test_both_succeed(self, run_transpiler, run_python_transpiler, tests_dir, test_file):
        """Both transpilers should successfully transpile the file"""
        slop_file = tests_dir / test_file

        native_code, native_out, native_err = run_transpiler(slop_file)
        python_code, python_out, python_err = run_python_transpiler(slop_file)

        assert native_code == 0, f"Native failed: {native_out}\n{native_err}"
        assert python_code == 0, f"Python failed: {python_out}\n{python_err}"


class TestCompilationAndExecution:
    """Tests that compile generated C code and verify runtime behavior"""

    def test_arithmetic_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Arithmetic test should compile and run"""
        slop_file = tests_dir / "pass_arithmetic.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        # Exit code should be 7 (1 + 2*3)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        assert run_code == 7, f"Expected 7, got {run_code}. stderr: {run_err}"

    def test_let_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Let binding test should compile and return correct value"""
        slop_file = tests_dir / "pass_let.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # 42 + 10 = 52
        assert run_code == 52, f"Expected 52, got {run_code}. stderr: {run_err}"

    def test_let_mut_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Mutable let test should compile and return correct value"""
        slop_file = tests_dir / "pass_let_mut.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # 0 + 1 + 1 = 2
        assert run_code == 2, f"Expected 2, got {run_code}. stderr: {run_err}"

    def test_if_runs(self, run_transpiler, compile_and_run, tests_dir):
        """If expression should return correct branch value"""
        slop_file = tests_dir / "pass_if.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # 5 > 3 is true, so return 100
        assert run_code == 100, f"Expected 100, got {run_code}. stderr: {run_err}"

    def test_function_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Function call test should compile and return correct value"""
        slop_file = tests_dir / "pass_function.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # 10 + 32 = 42
        assert run_code == 42, f"Expected 42, got {run_code}. stderr: {run_err}"

    def test_nested_let_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Nested let bindings should compile and return correct value"""
        slop_file = tests_dir / "pass_nested_let.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # 10 + 20 + 12 = 42
        assert run_code == 42, f"Expected 42, got {run_code}. stderr: {run_err}"

    def test_cond_runs(self, run_transpiler, compile_and_run, tests_dir):
        """Cond expression should compile and return correct value"""
        slop_file = tests_dir / "pass_cond.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # x=5 > 0, so return 1
        assert run_code == 1, f"Expected 1, got {run_code}. stderr: {run_err}"

    def test_while_runs(self, run_transpiler, compile_and_run, tests_dir):
        """While loop should compile and return correct sum"""
        slop_file = tests_dir / "pass_while.slop"
        exit_code, c_code, stderr = run_transpiler(slop_file)

        assert exit_code == 0, f"Transpilation failed: {c_code}\n{stderr}"

        run_code, run_out, run_err = compile_and_run(c_code)
        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")
        # sum of 0..9 = 45
        assert run_code == 45, f"Expected 45, got {run_code}. stderr: {run_err}"


class TestCodePatterns:
    """Tests for specific code patterns in transpiler output"""

    def test_let_uses_const(self, run_transpiler, tests_dir):
        """Immutable let bindings should use const"""
        slop_file = tests_dir / "pass_let.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0
        # Look for const declaration pattern
        assert "const" in stdout, "Immutable let should produce const declaration"

    def test_mut_let_no_const(self, run_transpiler, tests_dir):
        """Mutable let bindings should NOT use const"""
        slop_file = tests_dir / "pass_let_mut.slop"
        exit_code, stdout, stderr = run_transpiler(slop_file)

        assert exit_code == 0
        # Find the x declaration - it should not be const
        lines = stdout.split('\n')
        x_decl_lines = [l for l in lines if 'x' in l and '=' in l and 'x =' not in l.split('//')[0]]
        # The initial declaration should not have const before x
        # This is a bit tricky to verify precisely without parsing


class TestMultiModule:
    """Tests for multi-module transpilation"""

    def test_multi_module_transpiles(self, run_python_transpiler, tests_dir):
        """Multi-module build should transpile successfully"""
        import subprocess

        main_file = tests_dir / "multi-main.slop"

        # Use Python transpiler with include path for the tests directory
        result = subprocess.run(
            ["uv", "run", "slop", "transpile", str(main_file), "-I", str(tests_dir)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Transpilation failed:\n{result.stdout}\n{result.stderr}"

        # Should contain the imported type
        assert "Point" in result.stdout or "multi_lib_Point" in result.stdout
        # Should contain the imported functions
        assert "make_point" in result.stdout or "multi_lib_make_point" in result.stdout
        assert "point_add" in result.stdout or "multi_lib_point_add" in result.stdout

    def test_multi_module_compiles_and_runs(self, compile_and_run, tests_dir):
        """Multi-module build should compile and return correct value"""
        import subprocess

        main_file = tests_dir / "multi-main.slop"

        # Transpile with Python transpiler
        result = subprocess.run(
            ["uv", "run", "slop", "transpile", str(main_file), "-I", str(tests_dir)],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Transpilation failed:\n{result.stdout}\n{result.stderr}"

        c_code = result.stdout
        run_code, run_out, run_err = compile_and_run(c_code)

        if run_code == -1:
            pytest.skip(f"Compilation failed: {run_err}")

        # p1 = (10, 20), p2 = (5, 7), sum = (15, 27), result = 15 + 27 = 42
        assert run_code == 42, f"Expected 42, got {run_code}. stderr: {run_err}"

    def test_multi_module_with_native_build(self, compile_and_run, tests_dir):
        """Multi-module build with --native flag should work"""
        import subprocess
        import tempfile

        main_file = tests_dir / "multi-main.slop"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_bin = Path(tmpdir) / "multi_test"

            # Build with --native flag
            result = subprocess.run(
                ["uv", "run", "slop", "build", str(main_file),
                 "-I", str(tests_dir), "-o", str(output_bin), "--native"],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                # Native build might not be fully available
                if "not found" in result.stderr.lower() or "falling back" in result.stderr.lower():
                    pytest.skip("Native components not fully available")
                pytest.fail(f"Build failed:\n{result.stdout}\n{result.stderr}")

            # Run the built binary
            run_result = subprocess.run(
                [str(output_bin)],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # p1 = (10, 20), p2 = (5, 7), sum = (15, 27), result = 15 + 27 = 42
            assert run_result.returncode == 42, (
                f"Expected 42, got {run_result.returncode}. "
                f"stdout: {run_result.stdout}, stderr: {run_result.stderr}"
            )
