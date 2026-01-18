"""
Math Library Tests

Transpiles, compiles, and runs SLOP math operation tests to verify
the standard library works correctly at runtime.

Uses multi-module compilation to properly resolve imports from math.slop.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from slop.resolver import ModuleResolver
from slop.transpiler import transpile_multi_split


class TestMath:
    """Test math operations via transpiled SLOP code"""

    def test_math_operations(self, c_compiler, runtime_path, math_tests_path):
        """Transpile, compile, and run math_test.slop with math.slop library"""
        slop_path = math_tests_path / "math_test.slop"

        # Set up search paths to find math.slop (parent of tests/ dir)
        lib_math_path = math_tests_path.parent
        search_paths = [lib_math_path]

        # Build dependency graph
        resolver = ModuleResolver(search_paths)
        try:
            graph = resolver.build_dependency_graph(slop_path)
            order = resolver.topological_sort(graph)
        except Exception as e:
            pytest.fail(f"Module resolution failed: {e}")

        # Validate imports
        errors = resolver.validate_imports(graph)
        if errors:
            pytest.fail(f"Import validation failed: {errors}")

        # Transpile all modules
        try:
            results = transpile_multi_split(graph.modules, order)
        except Exception as e:
            pytest.fail(f"Transpilation failed: {e}")

        # Write to temp directory and compile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            c_files = []

            for mod_name, result_tuple in results.items():
                header, impl = result_tuple[0], result_tuple[1]
                # Write header (prefixed with slop_ to match transpiler includes)
                c_mod_name = mod_name.replace('-', '_')
                header_path = tmpdir_path / f"slop_{c_mod_name}.h"
                header_path.write_text(header)

                # Write implementation
                impl_path = tmpdir_path / f"slop_{c_mod_name}.c"
                impl_path.write_text(impl)
                c_files.append(str(impl_path))

            exe_file = tmpdir_path / "math_test"

            # Compile all C files together (link with math library)
            compile_cmd = [
                c_compiler,
                "-O2",
                f"-I{runtime_path}",
                f"-I{tmpdir}",
                "-o",
                str(exe_file),
            ] + c_files + ["-lm"]  # Link math library

            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
            )

            if compile_result.returncode != 0:
                # Print the generated C for debugging
                print("=== Generated C files ===")
                for mod_name, result_tuple in results.items():
                    header, impl = result_tuple[0], result_tuple[1]
                    print(f"\n--- {mod_name}.h ---")
                    for i, line in enumerate(header.split("\n"), 1):
                        print(f"{i:4}: {line}")
                    print(f"\n--- {mod_name}.c ---")
                    for i, line in enumerate(impl.split("\n"), 1):
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
                    f"Math tests failed with exit code {run_result.returncode}\n"
                    f"stdout: {run_result.stdout}\n"
                    f"stderr: {run_result.stderr}"
                )
