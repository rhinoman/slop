"""
Minimal test cases for native transpiler C compilation issues.
Each test isolates a specific bug to make debugging easier.

These tests check both Python and native transpiler output where applicable.
"""

import pytest
import subprocess
import tempfile
import os
import json
from pathlib import Path
from slop.transpiler import transpile


def compile_c_code(c_code: str) -> tuple[bool, str]:
    """Try to compile C code, return (success, error_message)."""
    with tempfile.NamedTemporaryFile(suffix='.c', delete=False, mode='w') as f:
        f.write(c_code)
        c_file = f.name

    try:
        result = subprocess.run(
            ['cc', '-c', c_file, '-I', 'src/slop/runtime', '-o', '/dev/null', '-fsyntax-only'],
            capture_output=True,
            text=True
        )
        return result.returncode == 0, result.stderr
    finally:
        os.unlink(c_file)


def native_transpile(source: str) -> str | None:
    """Use the native transpiler if available, return None if not."""
    native_bin = Path('src/slop/bin/slop-transpiler')
    if not native_bin.exists():
        return None

    with tempfile.NamedTemporaryFile(suffix='.slop', delete=False, mode='w') as f:
        f.write(source)
        slop_file = f.name

    try:
        result = subprocess.run(
            [str(native_bin), slop_file],
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            return None
        # Parse JSON output
        data = json.loads(result.stdout)
        # Combine headers and impls
        output = []
        for mod_name, mod_data in data.items():
            output.append(mod_data['header'])
            output.append(mod_data['impl'])
        return '\n'.join(output)
    except (json.JSONDecodeError, KeyError):
        return None
    finally:
        os.unlink(slop_file)


class TestNestedListTypes:
    """Test that nested List types generate valid C."""

    def test_list_of_list_of_int(self):
        """List (List Int) should generate slop_list_list_int."""
        source = '''
        (module test
          (fn process ((data (List (List Int))))
            (@intent "Process nested list")
            (@spec (((List (List Int))) -> Int))
            0))
        '''
        c_code = transpile(source)
        # Should define the nested list type
        assert 'slop_list_list_int' in c_code or 'slop_list_slop_list_int' in c_code
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"

    def test_list_of_list_of_ptr(self):
        """List (List (Ptr T)) should generate valid nested type."""
        source = '''
        (module test
          (type Item (record (value Int)))
          (fn process ((data (List (List (Ptr Item)))))
            (@intent "Process nested pointer list")
            (@spec (((List (List (Ptr Item)))) -> Int))
            0))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"


class TestMapFunctions:
    """Test that Map operations generate correct function names."""

    def test_map_new(self):
        """map-new should generate slop_map_new, not map_new."""
        source = '''
        (module test
          (fn create-map ((arena Arena))
            (@intent "Create a string->int map")
            (@spec ((Arena) -> (Map String Int)))
            (@alloc arena)
            (map-new arena String Int)))
        '''
        c_code = transpile(source)
        # Should NOT have bare map_new
        assert 'map_new(' not in c_code or 'slop_map_new(' in c_code
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"

    def test_map_get_returns_option(self):
        """map-get should return Option type, not void*."""
        source = '''
        (module test
          (fn lookup ((m (Map String Int)) (key String))
            (@intent "Look up value in map")
            (@spec (((Map String Int) String) -> (Option Int)))
            (map-get m key)))
        '''
        c_code = transpile(source)
        # Return type should be option, not void*
        assert 'void*' not in c_code or 'slop_option' in c_code
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"


class TestMatchStatements:
    """Test that match statements generate valid C."""

    def test_match_option_compiles(self):
        """Match on Option should generate compilable C."""
        source = '''
        (module test
          (fn check-value ((opt (Option Int)))
            (@intent "Check optional value")
            (@spec (((Option Int)) -> Int))
            (match opt
              ((some x) x)
              ((none) 0))))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"

    def test_match_result_compiles(self):
        """Match on Result should generate compilable C."""
        source = '''
        (module test
          (fn check-result ((res (Result Int String)))
            (@intent "Check result value")
            (@spec (((Result Int String)) -> Int))
            (match res
              ((ok x) x)
              ((error e) 0))))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"

    def test_match_in_statement_position(self):
        """Match used as statement (not expression) should work."""
        source = '''
        (module test
          (fn process ((opt (Option Int)))
            (@intent "Process optional")
            (@spec (((Option Int)) -> Int))
            (let ((mut result 0))
              (match opt
                ((some x) (set! result x))
                ((none) (set! result -1)))
              result)))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"


class TestScopeVariables:
    """Test that scope/arena variables are properly captured."""

    def test_arena_in_nested_scope(self):
        """Arena should be accessible in nested let bindings."""
        source = '''
        (module test
          (fn allocate ((arena Arena))
            (@intent "Allocate in nested scope")
            (@spec ((Arena) -> String))
            (@alloc arena)
            (let ((x 1))
              (let ((y 2))
                (string-concat arena "a" "b")))))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"


class TestHeaderOrdering:
    """Test that type dependencies are ordered correctly in headers."""

    def test_imported_type_in_generic(self):
        """Generic types using imported types should work."""
        # This mimics the transpiler-types using env types issue
        source = '''
        (module types-mod
          (export MyType)
          (type MyType (record (value Int))))
        '''
        c_code = transpile(source)
        success, err = compile_c_code(c_code)
        assert success, f"C compilation failed: {err}"
