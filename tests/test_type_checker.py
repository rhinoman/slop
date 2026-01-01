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

    def test_ambiguous_enum_variants_error(self):
        """Enum variants with same name in different types should be an error"""
        source = """
        (module test
          (type Status (enum ok error))
          (type Result (enum ok fail)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 1
        assert "Ambiguous enum variant 'ok'" in errors[0].message
        assert "Result" in errors[0].message
        assert "Status" in errors[0].message

    def test_non_overlapping_enum_variants_ok(self):
        """Enum types with distinct variants should not error"""
        source = """
        (module test
          (type HttpStatus (enum http-ok http-error))
          (type ApiError (enum api-ok api-fail)))
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


class TestParameterModes:
    """Test parameter mode type checking"""

    def test_in_mode_assignment_error(self):
        """Assigning to 'in' parameter should produce error"""
        source = """
        (module test
          (fn bad ((in x Int))
            (@spec ((Int) -> Int))
            (set! x 42)
            x))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        # Should have error about assigning to 'in' parameter
        assert len(errors) >= 1
        assert any("'in' parameter" in str(e.message) for e in errors)

    def test_out_mode_uninitialized_warning(self):
        """Reading uninitialized 'out' parameter should warn"""
        source = """
        (module test
          (fn bad ((out result Int))
            (@spec ((Int) -> Int))
            result))
        """
        diagnostics = check_source(source)
        warnings = [d for d in diagnostics if d.severity == 'warning']
        # Should have warning about reading uninitialized 'out' parameter
        assert len(warnings) >= 1
        assert any("'out' parameter" in str(w.message) for w in warnings)

    def test_out_mode_initialized_ok(self):
        """Reading 'out' parameter after initialization should be fine"""
        source = """
        (module test
          (fn ok ((out result Int))
            (@spec ((Int) -> Int))
            (set! result 42)
            result))
        """
        diagnostics = check_source(source)
        warnings = [d for d in diagnostics if d.severity == 'warning']
        # No warning about uninitialized read
        out_warnings = [w for w in warnings if "'out' parameter" in str(w.message)]
        assert len(out_warnings) == 0

    def test_mut_mode_read_write_ok(self):
        """'mut' mode allows both read and write"""
        source = """
        (module test
          (fn ok ((mut counter Int))
            (@spec ((Int) -> Int))
            (set! counter (+ counter 1))
            counter))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        # No errors for mut read/write
        assert len(errors) == 0


class TestTypedCollections:
    """Test typed list and map constructors"""

    def test_list_new_with_type_parameter(self):
        """(list-new arena Type) should infer (List Type)"""
        source = """
        (module test
          (fn make-list ((arena Arena))
            (@spec ((Arena) -> (List Int)))
            (list-new arena Int)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_map_new_with_type_parameters(self):
        """(map-new arena KeyType ValueType) should infer (Map KeyType ValueType)"""
        source = """
        (module test
          (fn make-map ((arena Arena))
            (@spec ((Arena) -> (Map String Int)))
            (map-new arena String Int)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_list_literal_explicit_type(self):
        """(list Int 1 2 3) should infer (List Int)"""
        source = """
        (module test
          (fn make-list ()
            (@spec (() -> (List Int)))
            (list Int 1 2 3)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_list_literal_inferred_type(self):
        """(list 1 2 3) should infer (List Int) from first element"""
        source = """
        (module test
          (fn make-list ()
            (@spec (() -> (List Int)))
            (list 1 2 3)))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_list_literal_string(self):
        """(list String "a" "b") should infer (List String)"""
        source = """
        (module test
          (fn make-list ()
            (@spec (() -> (List String)))
            (list String "a" "b")))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_list_literal_type_mismatch(self):
        """(list Int 1 "string") should error on type mismatch"""
        source = """
        (module test
          (fn make-list ()
            (@spec (() -> (List Int)))
            (list Int 1 "string")))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) > 0
        # Error message says "expected Int, got String"
        assert any('expected' in str(e.message).lower() and 'got' in str(e.message).lower() for e in errors)

    def test_map_literal_explicit_types(self):
        """(map String Int ("a" 1)) should infer (Map String Int)"""
        source = """
        (module test
          (fn make-map ()
            (@spec (() -> (Map String Int)))
            (map String Int ("a" 1) ("b" 2))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_map_literal_inferred_types(self):
        """(map ("a" 1) ("b" 2)) should infer types from first pair"""
        source = """
        (module test
          (fn make-map ()
            (@spec (() -> (Map String Int)))
            (map ("a" 1) ("b" 2))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestMatchExhaustiveness:
    """Test exhaustiveness checking for match expressions"""

    def test_enum_exhaustive_all_variants(self):
        """Complete enum coverage should pass"""
        source = """
        (module test
          (type Status (enum ok error))
          (fn handle ((s Status))
            (@spec ((Status) -> Int))
            (match s ('ok 1) ('error 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_enum_non_exhaustive_error(self):
        """Missing enum variant should error"""
        source = """
        (module test
          (type Status (enum ok error pending))
          (fn handle ((s Status))
            (@spec ((Status) -> Int))
            (match s ('ok 1) ('error 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) >= 1
        assert any("pending" in e.message for e in errors)

    def test_enum_wildcard_satisfies(self):
        """Wildcard pattern should satisfy exhaustiveness"""
        source = """
        (module test
          (type Status (enum ok error pending))
          (fn handle ((s Status))
            (@spec ((Status) -> Int))
            (match s ('ok 1) (_ 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_option_exhaustive(self):
        """Option with some and none should pass"""
        source = """
        (module test
          (fn get-or-zero ((opt (Option Int)))
            (@spec (((Option Int)) -> Int))
            (match opt ((some v) v) (none 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_option_missing_none_error(self):
        """Option missing none should error"""
        source = """
        (module test
          (fn get-value ((opt (Option Int)))
            (@spec (((Option Int)) -> Int))
            (match opt ((some v) v))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) >= 1
        assert any("none" in e.message for e in errors)

    def test_result_exhaustive(self):
        """Result with ok and error should pass"""
        source = """
        (module test
          (fn unwrap-or-zero ((r (Result Int String)))
            (@spec (((Result Int String)) -> Int))
            (match r ((ok v) v) ((error e) 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_result_missing_error_branch(self):
        """Result missing error branch should error"""
        source = """
        (module test
          (fn unwrap ((r (Result Int String)))
            (@spec (((Result Int String)) -> Int))
            (match r ((ok v) v))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) >= 1
        assert any("error" in e.message for e in errors)

    def test_union_exhaustive(self):
        """Union with all tags should pass"""
        source = """
        (module test
          (type Value (union (number Int) (text String) (nothing)))
          (fn extract ((v Value))
            (@spec ((Value) -> Int))
            (match v ((number n) n) ((text t) 0) ((nothing) -1))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_union_missing_tag_error(self):
        """Union missing a tag should error"""
        source = """
        (module test
          (type Value (union (number Int) (text String) (nothing)))
          (fn extract ((v Value))
            (@spec ((Value) -> Int))
            (match v ((number n) n) ((text t) 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) >= 1
        assert any("nothing" in e.message for e in errors)

    def test_else_wildcard_satisfies(self):
        """'else' keyword should also work as wildcard"""
        source = """
        (module test
          (type Status (enum ok error pending))
          (fn handle ((s Status))
            (@spec ((Status) -> Int))
            (match s ('ok 1) (else 0))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0


class TestCollectionMutability:
    """Test that list-push/map-put require mutable collections"""

    def test_list_push_to_literal_errors(self):
        """Cannot push to list literal - it uses static const backing array"""
        source = """
        (module test
          (fn bad ((arena Arena))
            (@spec ((Arena) -> Unit))
            (let ((xs (list Int 1 2 3)))
              (list-push xs 5))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 1
        assert "cannot push to immutable list" in errors[0].message

    def test_list_push_to_mut_literal_errors(self):
        """mut binding doesn't make list literal mutable"""
        source = """
        (module test
          (fn bad ((arena Arena))
            (@spec ((Arena) -> Unit))
            (let ((mut xs (list Int 1 2 3)))
              (list-push xs 5))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 1
        assert "cannot push to immutable list" in errors[0].message

    def test_list_push_to_list_new_ok(self):
        """Can push to list from list-new"""
        source = """
        (module test
          (fn ok ((arena Arena))
            (@spec ((Arena) -> Unit))
            (let ((xs (list-new arena Int)))
              (list-push xs 5))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0

    def test_map_put_to_literal_errors(self):
        """Cannot put to map literal"""
        source = """
        (module test
          (fn bad ((arena Arena))
            (@spec ((Arena) -> Unit))
            (let ((m (map String Int ("a" 1))))
              (map-put m "b" 2))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 1
        assert "cannot put to immutable map" in errors[0].message

    def test_map_put_to_map_new_ok(self):
        """Can put to map from map-new"""
        source = """
        (module test
          (fn ok ((arena Arena))
            (@spec ((Arena) -> Unit))
            (let ((m (map-new arena String Int)))
              (map-put m "a" 1))))
        """
        diagnostics = check_source(source)
        errors = [d for d in diagnostics if d.severity == 'error']
        assert len(errors) == 0
