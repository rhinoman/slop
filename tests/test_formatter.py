"""Tests for SLOP formatter."""
import pytest
from slop.formatter import format_source, format_expr, inline
from slop.parser import parse


class TestInline:
    """Test inline rendering."""

    def test_simple_expr(self):
        ast = parse("(+ x y)")
        assert inline(ast[0]) == "(+ x y)"

    def test_nested_expr(self):
        ast = parse("(+ (* a b) c)")
        assert inline(ast[0]) == "(+ (* a b) c)"

    def test_string(self):
        ast = parse('(print "hello")')
        assert inline(ast[0]) == '(print "hello")'


class TestFormatFn:
    """Test function formatting."""

    def test_simple_fn(self):
        source = '(fn foo ((x Int)) (@intent "test") body)'
        result = format_source(source)
        assert "(fn foo ((x Int))" in result
        assert '(@intent "test")' in result

    def test_fn_multiline(self):
        source = '(fn foo ((x Int) (y String)) (@intent "test") (@spec ((Int String) -> Bool)) (some-body x y))'
        result = format_source(source)
        lines = result.strip().split('\n')
        assert lines[0] == "(fn foo ((x Int) (y String))"
        assert any('@intent' in line for line in lines)
        assert any('@spec' in line for line in lines)


class TestFormatModule:
    """Test module formatting."""

    def test_module_structure(self):
        source = '''
        (module test
          (export (foo 1))
          (type Bar Int)
          (fn foo ((x Int)) x))
        '''
        result = format_source(source)
        lines = result.strip().split('\n')
        assert lines[0] == "(module test"
        assert any("(export" in line for line in lines)
        assert any("(type" in line for line in lines)
        assert any("(fn foo" in line for line in lines)


class TestFormatType:
    """Test type definition formatting."""

    def test_inline_enum(self):
        source = "(type Status (enum pending active done))"
        result = format_source(source)
        assert "(type Status (enum pending active done))" in result

    def test_record_multiline(self):
        source = "(type User (record (name String) (age Int) (email String)))"
        result = format_source(source)
        lines = result.strip().split('\n')
        # Record should be multi-line if fields are complex
        assert any("(name String)" in line for line in lines)


class TestFormatLet:
    """Test let binding formatting."""

    def test_simple_let(self):
        source = "(let ((x 1)) x)"
        result = format_source(source)
        assert "(let ((x 1))" in result

    def test_multiple_bindings(self):
        source = "(let ((x 1) (y 2) (z 3)) (+ x y z))"
        result = format_source(source)
        # Multiple bindings should be aligned
        assert "(let ((" in result


class TestFormatControlFlow:
    """Test control flow formatting."""

    def test_short_if_inline(self):
        source = "(if cond a b)"
        result = format_source(source)
        assert result.strip() == "(if cond a b)"

    def test_long_if_multiline(self):
        source = "(if (some-long-condition x y z) (then-expression with many args) (else-expression also long))"
        result = format_source(source)
        lines = result.strip().split('\n')
        assert len(lines) > 1  # Should be multi-line

    def test_match(self):
        source = "(match x (Foo a) (Bar b))"
        result = format_source(source)
        assert "(match x" in result


class TestFormatGeneric:
    """Test generic expression formatting."""

    def test_short_inline(self):
        source = "(+ 1 2)"
        result = format_source(source)
        assert result.strip() == "(+ 1 2)"

    def test_long_wraps(self):
        source = "(some-func arg1 arg2 arg3 (nested-call with args) (another-nested-call with more args))"
        result = format_source(source)
        # Long expressions should wrap
        assert result.strip().startswith("(some-func")


class TestFormatFfi:
    """Test FFI formatting."""

    def test_ffi_block(self):
        source = '''(ffi "stdio.h" (printf ((fmt String)) Int) (puts ((s String)) Int))'''
        result = format_source(source)
        lines = result.strip().split('\n')
        assert '(ffi "stdio.h"' in lines[0]

    def test_ffi_struct(self):
        source = "(ffi-struct header.h Point (x Int) (y Int))"
        result = format_source(source)
        assert "ffi-struct" in result


class TestFormatHole:
    """Test hole expression formatting."""

    def test_hole_multiline(self):
        source = '(hole Int "compute something" :complexity tier-2 :must-use (x y))'
        result = format_source(source)
        lines = result.strip().split('\n')
        assert lines[0] == "(hole Int"
        assert any(":complexity" in line for line in lines)
        assert any(":must-use" in line for line in lines)


class TestRoundTrip:
    """Test that formatting is idempotent."""

    def test_idempotent(self):
        source = '''
        (module test
          (export (main 0))
          (fn main ()
            (@intent "entry point")
            (@spec (() -> Int))
            0))
        '''
        first = format_source(source)
        second = format_source(first)
        assert first == second
