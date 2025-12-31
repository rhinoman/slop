"""
Environment Variables Library Tests

Transpiles, compiles, and runs SLOP environment variable tests to verify
the standard library works correctly at runtime.

Uses multi-module compilation to properly resolve imports from env.slop.
"""

import subprocess
import tempfile
from pathlib import Path

import pytest

from slop.resolver import ModuleResolver
from slop.transpiler import transpile_multi_split


class TestEnv:
    """Test environment variable operations via transpiled SLOP code"""

    def test_env_operations(self, c_compiler, runtime_path, os_tests_path):
        """Transpile, compile, and run env_test.slop with env.slop library"""
        slop_path = os_tests_path / "env_test.slop"

        # Set up search paths to find env.slop (parent of tests/ dir)
        lib_os_path = os_tests_path.parent
        search_paths = [lib_os_path]

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

            for mod_name, (header, impl) in results.items():
                # Write header
                header_path = tmpdir_path / f"{mod_name}.h"
                header_path.write_text(header)

                # Write implementation
                impl_path = tmpdir_path / f"{mod_name}.c"
                impl_path.write_text(impl)
                c_files.append(str(impl_path))

            exe_file = tmpdir_path / "env_test"

            # Compile all C files together
            compile_cmd = [
                c_compiler,
                "-O2",
                f"-I{runtime_path}",
                f"-I{tmpdir}",
                "-o",
                str(exe_file),
            ] + c_files

            compile_result = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
            )

            if compile_result.returncode != 0:
                # Print the generated C for debugging
                print("=== Generated C files ===")
                for mod_name, (header, impl) in results.items():
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
                    f"Environment variable tests failed with exit code {run_result.returncode}\n"
                    f"stdout: {run_result.stdout}\n"
                    f"stderr: {run_result.stderr}"
                )
