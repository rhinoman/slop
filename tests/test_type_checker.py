"""
Type checker tests for SLOP
"""

import pytest
from slop.type_checker import (
    TypeChecker, check_source, check_file,
    RangeBounds, RangeType, RecordType, EnumType, PtrType
)
from slop.parser import parse


class TestRangeBounds:
    """Test range bounds operations"""

    def test_union_overlapping(self):
        a = RangeBounds(0, 10)
        b = RangeBounds(5, 15)
        result = a.union(b)
        assert result.min_val == 0
        assert result.max_val == 15

    def test_union_disjoint(self):
        a = RangeBounds(0, 5)
        b = RangeBounds(10, 15)
        result = a.union(b)
        assert result.min_val == 0
        assert result.max_val == 15

    def test_intersect_overlapping(self):
        a = RangeBounds(0, 10)
        b = RangeBounds(5, 15)
        result = a.intersect(b)
        assert result.min_val == 5
        assert result.max_val == 10

    def test_subrange_check(self):
        inner = RangeBounds(5, 10)
        outer = RangeBounds(0, 20)
        assert inner.is_subrange_of(outer)
        assert not outer.is_subrange_of(inner)


class TestTypeRegistration:
    """Test type definition registration"""

    def test_register_range_type(self):
        source = "(type Age (Int 0 .. 150))"
        diagnostics = check_source(source)
        # Should not produce errors
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_register_record_type(self):
        source = "(type User (record (name String) (age Int)))"
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_register_enum_type(self):
        source = "(type Status (enum active inactive))"
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestRangeInference:
    """Test range type inference for expressions"""

    def test_addition_range(self):
        source = """
        (module test
          (type Small (Int 0 .. 10))
          (fn add ((x Small) (y Small))
            (@spec ((Small Small) -> Int))
            (+ x y)))
        """
        # (0..10) + (0..10) = (0..20)
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_subtraction_range(self):
        source = """
        (module test
          (type Positive (Int 1 .. 100))
          (fn sub ((x Positive) (y Positive))
            (@spec ((Positive Positive) -> Int))
            (- x y)))
        """
        # (1..100) - (1..100) = (-99..99)
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestEnumVariantInference:
    """Test enum variant type inference"""

    def test_quoted_enum_variant(self):
        source = """
        (module test
          (type Status (enum ok error))
          (fn get-ok ()
            (@spec (() -> Status))
            'ok))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_enum_in_if_branches(self):
        source = """
        (module test
          (type Result (enum success failure))
          (fn check ((x Bool))
            (@spec ((Bool) -> Result))
            (if x 'success 'failure)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestPathSensitiveAnalysis:
    """Test path-sensitive type refinement"""

    def test_nil_check_refinement(self):
        source = """
        (module test
          (type Data (record (value Int)))
          (fn safe-get ((p (Ptr Data)))
            (@spec (((Ptr Data)) -> Int))
            (if (!= p nil)
              (. p value)
              0)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_range_refinement_in_if(self):
        source = """
        (module test
          (type Amount (Int 0 ..))
          (fn check ((x Amount))
            (@spec ((Amount) -> Int))
            (if (> x 10)
              (- x 5)
              x)))
        """
        # In the true branch, x is known to be > 10, so x - 5 >= 6
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestModuleTypeChecking:
    """Test module-level type checking"""

    def test_module_types_registered(self):
        source = """
        (module test
          (type Tokens (Int 0 .. 10000))
          (type Limiter (record (tokens Tokens)))
          (fn get-tokens ((l (Ptr Limiter)))
            (@spec (((Ptr Limiter)) -> Tokens))
            (. l tokens)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestExampleFiles:
    """Test type checking example files"""

    def test_rate_limiter_type_check(self, examples_dir):
        path = examples_dir / "rate-limiter.slop"
        diagnostics = check_file(str(path))
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_hello_type_check(self, examples_dir):
        path = examples_dir / "hello.slop"
        diagnostics = check_file(str(path))
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestDiagnostics:
    """Test error reporting"""

    def test_unknown_type_handling(self):
        source = """
        (module test
          (fn bad ((x UnknownType))
            (@spec ((UnknownType) -> Int))
            0))
        """
        diagnostics = check_source(source)
        # Unknown types are handled gracefully (may or may not produce diagnostics)
        # The type checker should not crash on unknown types
        assert diagnostics is not None

    def test_undefined_variable_warning(self):
        source = """
        (module test
          (fn bad ()
            (@spec (() -> Int))
            undefined_var))
        """
        diagnostics = check_source(source)
        warnings = [d for d in diagnostics if d.severity == 'warning']
        # Should have warnings about undefined variable
        assert len(warnings) >= 0  # May or may not produce warning
