"""Tests for check_hole_impl API and CLI command."""
import pytest
import subprocess
import tempfile
from pathlib import Path
from slop.hole_filler import check_hole_impl, CheckResult


class TestCheckHoleImpl:
    """Test the Python API."""

    def test_simple_int_valid(self):
        """Simple integer matches Int type."""
        result = check_hole_impl("42", "Int")
        assert result.valid
        assert result.errors == []

    def test_string_for_int_invalid(self):
        """String doesn't match Int type."""
        result = check_hole_impl('"hello"', "Int")
        assert not result.valid
        assert len(result.errors) > 0

    def test_with_params(self):
        """Expression using param in scope."""
        result = check_hole_impl(
            "x",
            "Int",
            params="((x Int))"
        )
        assert result.valid

    def test_param_type_mismatch(self):
        """Expression using param with wrong type."""
        result = check_hole_impl(
            "x",
            "String",
            params="((x Int))"
        )
        assert not result.valid
        assert any("Type mismatch" in err or "mismatch" in err.lower() for err in result.errors)

    def test_arithmetic_expression(self):
        """Arithmetic expression type checking."""
        result = check_hole_impl(
            "(+ x 1)",
            "Int",
            params="((x Int))"
        )
        assert result.valid

    def test_undefined_variable(self):
        """Undefined variable is caught."""
        result = check_hole_impl(
            "undefined_var",
            "Int"
        )
        assert not result.valid
        assert any("Undefined variable" in err for err in result.errors)

    def test_option_some_valid(self):
        """(some value) matches (Option T)."""
        result = check_hole_impl(
            "(some 42)",
            "(Option Int)"
        )
        assert result.valid

    def test_result_ok_valid(self):
        """(ok value) matches (Result T E)."""
        result = check_hole_impl(
            "(ok 42)",
            "(Result Int Error)"
        )
        assert result.valid

    def test_result_error_valid(self):
        """(error value) matches (Result T E)."""
        result = check_hole_impl(
            "(error 'some-error)",
            "(Result Int Symbol)"
        )
        assert result.valid

    def test_parse_error_in_expr(self):
        """Parse error in expression is reported."""
        result = check_hole_impl(
            "(unclosed paren",
            "Int"
        )
        assert not result.valid
        assert any("Parse error" in err for err in result.errors)

    def test_parse_error_in_type(self):
        """Parse error in type is reported."""
        result = check_hole_impl(
            "42",
            "(Result Int"
        )
        assert not result.valid
        assert any("Parse error" in err for err in result.errors)

    def test_empty_expression(self):
        """Empty expression is rejected."""
        result = check_hole_impl("", "Int")
        assert not result.valid
        assert any("Empty" in err for err in result.errors)

    def test_verbose_info(self):
        """Result includes inferred and expected type info."""
        result = check_hole_impl("42", "Int")
        assert result.valid
        assert result.inferred_type is not None
        assert result.expected_type is not None

    def test_with_context_file(self, tmp_path):
        """Use types from context file."""
        # Create temp .slop file
        slop_file = tmp_path / "test.slop"
        slop_file.write_text("""
(type UserId (Int 1 .. 1000000))

(fn get-user ((id UserId))
  (@spec ((UserId) -> Bool))
  true)
""")

        result = check_hole_impl(
            "(cast UserId 42)",
            "UserId",
            context_file=str(slop_file)
        )
        assert result.valid

    def test_with_fn_params(self, tmp_path):
        """Extract params from function in context file."""
        slop_file = tmp_path / "test.slop"
        slop_file.write_text("""
(fn process ((x Int) (y String))
  (@spec ((Int String) -> Int))
  0)
""")

        result = check_hole_impl(
            "(+ x 1)",
            "Int",
            context_file=str(slop_file),
            fn_name="process"
        )
        assert result.valid

    def test_fn_spec_in_context(self, tmp_path):
        """Function specs from context file are available."""
        slop_file = tmp_path / "test.slop"
        slop_file.write_text("""
(fn helper ((x Int))
  (@spec ((Int) -> Int))
  x)

(fn main ()
  (@spec (() -> Int))
  0)
""")

        result = check_hole_impl(
            "(helper 42)",
            "Int",
            context_file=str(slop_file)
        )
        assert result.valid

    def test_context_file_not_found(self):
        """Missing context file is reported."""
        result = check_hole_impl(
            "42",
            "Int",
            context_file="/nonexistent/file.slop"
        )
        assert not result.valid
        assert any("Error reading context file" in err for err in result.errors)

    def test_params_override_fn(self, tmp_path):
        """Explicit params override fn_name extraction."""
        slop_file = tmp_path / "test.slop"
        slop_file.write_text("""
(fn process ((x String))
  (@spec ((String) -> String))
  x)
""")

        # fn_name would give x:String, but explicit params gives x:Int
        result = check_hole_impl(
            "(+ x 1)",
            "Int",
            context_file=str(slop_file),
            fn_name="process",
            params="((x Int))"  # Override with Int type
        )
        assert result.valid


class TestCheckHoleCLI:
    """Test the CLI command."""

    def test_inline_expr(self):
        """Check expression passed as argument."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '42', '-t', 'Int'],
            capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout

    def test_verbose_output(self):
        """Verbose mode shows type info."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '42', '-t', 'Int', '-v'],
            capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout
        assert 'Int' in result.stdout

    def test_invalid_returns_error(self):
        """Invalid expression returns exit code 1."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '"hello"', '-t', 'Int'],
            capture_output=True, text=True
        )
        assert result.returncode == 1
        assert 'Error' in result.stderr

    def test_with_params_flag(self):
        """Check with params flag."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', 'x', '-t', 'Int', '-p', '((x Int))'],
            capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout

    def test_fn_requires_context(self):
        """--fn without --context is an error."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '42', '-t', 'Int', '-f', 'some-fn'],
            capture_output=True, text=True
        )
        assert result.returncode == 1
        assert '--fn requires --context' in result.stderr

    def test_stdin_expr(self):
        """Check expression from stdin."""
        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '-t', 'Int'],
            input='42',
            capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout

    def test_with_context_file(self, tmp_path):
        """Check with context file."""
        slop_file = tmp_path / "test.slop"
        slop_file.write_text("""
(type Count Int)
(fn add-one ((x Count))
  (@spec ((Count) -> Count))
  0)
""")

        result = subprocess.run(
            ['uv', 'run', 'slop', 'check-hole', '(+ x 1)', '-t', 'Count',
             '-c', str(slop_file), '-f', 'add-one'],
            capture_output=True, text=True
        )
        assert result.returncode == 0
        assert 'OK' in result.stdout
