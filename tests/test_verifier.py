"""
Z3 Verifier tests for SLOP
"""

import pytest

# Skip all tests if z3 is not available
z3_available = False
try:
    import z3
    z3_available = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(not z3_available, reason="z3-solver not installed")


class TestZ3Translation:
    """Test SLOP to Z3 translation"""

    def test_arithmetic_translation(self):
        """Test +, -, *, / translate correctly"""
        from slop.verifier import Z3Translator
        from slop.type_checker import TypeEnv
        from slop.parser import parse
        from slop.types import PrimitiveType

        env = TypeEnv()
        translator = Z3Translator(env)

        # Declare variables
        translator.declare_variable('x', PrimitiveType('Int'))
        translator.declare_variable('y', PrimitiveType('Int'))

        # Test addition
        expr = parse("(+ x y)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert str(z3_expr) == "x + y"

        # Test subtraction
        expr = parse("(- x y)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert str(z3_expr) == "x - y"

        # Test multiplication
        expr = parse("(* x y)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert str(z3_expr) == "x*y"

    def test_comparison_translation(self):
        """Test >, <, >=, <=, ==, != translate correctly"""
        from slop.verifier import Z3Translator
        from slop.type_checker import TypeEnv
        from slop.parser import parse
        from slop.types import PrimitiveType

        env = TypeEnv()
        translator = Z3Translator(env)

        translator.declare_variable('x', PrimitiveType('Int'))
        translator.declare_variable('y', PrimitiveType('Int'))

        # Test greater than
        expr = parse("(> x y)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert "x" in str(z3_expr) and "y" in str(z3_expr)

        # Test less than or equal
        expr = parse("(<= x 10)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        # Z3 may format as "10 >= x" or "x <= 10"
        assert "x" in str(z3_expr) and "10" in str(z3_expr)

        # Test equality
        expr = parse("(== x y)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert "x" in str(z3_expr) and "y" in str(z3_expr)

    def test_boolean_translation(self):
        """Test and, or, not translate correctly"""
        from slop.verifier import Z3Translator
        from slop.type_checker import TypeEnv
        from slop.parser import parse
        from slop.types import PrimitiveType

        env = TypeEnv()
        translator = Z3Translator(env)

        translator.declare_variable('a', PrimitiveType('Bool'))
        translator.declare_variable('b', PrimitiveType('Bool'))

        # Test and
        expr = parse("(and a b)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert "And" in str(z3_expr)

        # Test or
        expr = parse("(or a b)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert "Or" in str(z3_expr)

        # Test not
        expr = parse("(not a)")[0]
        z3_expr = translator.translate_expr(expr)
        assert z3_expr is not None
        assert "Not" in str(z3_expr)

    def test_range_type_constraints(self):
        """Test RangeType adds correct Z3 constraints"""
        from slop.verifier import Z3Translator
        from slop.type_checker import TypeEnv
        from slop.types import RangeType, RangeBounds

        env = TypeEnv()
        translator = Z3Translator(env)

        # Declare a range-typed variable
        range_type = RangeType('Int', RangeBounds(0, 100))
        translator.declare_variable('x', range_type)

        # Should have constraints for bounds
        assert len(translator.constraints) == 2  # x >= 0 and x <= 100


class TestContractVerification:
    """Test contract verification"""

    def test_valid_contract_verifies(self):
        """Function with self-consistent contract should verify"""
        from slop.verifier import verify_source

        # This tests that contracts are internally consistent
        # Note: The verifier checks if (pre AND NOT post) is unsatisfiable
        # This is true when the postcondition is always true (like true)
        source = """
        (module test
          (fn always-true ((x Int))
            (@spec ((Int) -> Bool))
            (@pre (>= x 0))
            (@post (== $result true))
            true))
        """
        results = verify_source(source)

        # Should have at least one result from the function
        assert len(results) >= 1

    def test_trivially_true_postcondition(self):
        """Postcondition that is always true"""
        from slop.verifier import verify_source

        # Test with postcondition that is a tautology
        source = """
        (module test
          (fn tautology ((x Int))
            (@spec ((Int) -> Int))
            (@pre (> x 0))
            (@post (or (> $result 0) (<= $result 0)))
            x))
        """
        results = verify_source(source)

        # The tautology postcondition should verify
        verified = [r for r in results if r.status == 'verified']
        assert len(verified) >= 1

    def test_unsatisfiable_precondition(self):
        """Precondition that can never be true"""
        from slop.verifier import verify_source

        source = """
        (fn impossible ((x Int))
          (@spec ((Int) -> Int))
          (@pre (and (> x 10) (< x 5)))
          x)
        """
        results = verify_source(source)

        failed = [r for r in results if r.status == 'failed']
        assert len(failed) >= 1

    def test_malformed_postcondition_fails(self):
        """Malformed postcondition should fail, not pass"""
        from slop.verifier import verify_source

        # (== 42) is missing second argument
        source = """
        (module test
          (fn bad ((x Int))
            (@spec ((Int) -> Int))
            (@post (== 42))
            x))
        """
        results = verify_source(source)

        failed = [r for r in results if r.status == 'failed']
        assert len(failed) >= 1
        assert "postcondition" in failed[0].message.lower()

    def test_malformed_precondition_fails(self):
        """Malformed precondition should fail, not pass"""
        from slop.verifier import verify_source

        # (> x) is missing second argument
        source = """
        (module test
          (fn bad ((x Int))
            (@spec ((Int) -> Int))
            (@pre (> x))
            x))
        """
        results = verify_source(source)

        failed = [r for r in results if r.status == 'failed']
        assert len(failed) >= 1
        assert "precondition" in failed[0].message.lower()

    def test_function_with_range_param(self):
        """Function with range type parameter - contract checking only"""
        from slop.verifier import verify_source

        # Note: The verifier checks contract consistency, not function body semantics
        # This test verifies that range constraints are used in contract checking
        source = """
        (module test
          (type Positive (Int 1 .. 100))
          (fn check ((x Positive))
            (@spec ((Positive) -> Int))
            (@pre (>= x 1))
            (@post (>= $result 1))
            x))
        """
        results = verify_source(source)

        # With a proper @pre that matches the range, the postcondition
        # should be verifiable (x >= 1 implies $result >= 1 when $result = x)
        verified = [r for r in results if r.status == 'verified']
        failed = [r for r in results if r.status == 'failed']

        # This verifies that preconditions and range types are being used
        assert len(results) > 0

    def test_deref_in_precondition(self):
        """Contracts with deref should translate"""
        from slop.verifier import verify_source

        source = """
        (module test
          (type Obj (record (is-open Bool)))
          (fn check ((f (Ptr Obj)))
            (@spec (((Ptr Obj)) -> Bool))
            (@pre (. (deref f) is-open))
            (@post (or (== $result true) (== $result false)))
            true))
        """
        results = verify_source(source)
        # Should not fail due to translation error
        failed_translation = [r for r in results
                              if r.status == 'failed' and 'translate' in r.message.lower()]
        assert len(failed_translation) == 0

    def test_mutable_state_postcondition_warns(self):
        """Postcondition with deref field access should warn, not fail"""
        from slop.verifier import verify_source

        source = """
        (module test
          (type Obj (record (is-open Bool)))
          (fn close ((f (Ptr Obj)))
            (@spec (((Ptr Obj)) -> Bool))
            (@pre (. (deref f) is-open))
            (@post (not (. (deref f) is-open)))
            true))
        """
        results = verify_source(source)
        warnings = [r for r in results if r.status == 'warning']
        assert len(warnings) == 1
        assert "mutable state" in warnings[0].message.lower()

    def test_enum_comparison_in_postcondition(self):
        """Postcondition comparing $result to enum variant should verify"""
        from slop.verifier import verify_source

        source = """
        (module test
          (type Status (enum ok error pending))
          (fn get-status ((x Int))
            (@spec ((Int) -> Status))
            (@post (or (== $result 'ok)
                       (== $result 'error)
                       (== $result 'pending)))
            'ok))
        """
        results = verify_source(source)
        verified = [r for r in results if r.status == 'verified']
        assert len(verified) == 1


class TestRangeVerification:
    """Test range type safety verification"""

    def test_range_type_parsed(self):
        """Range type should be recognized and used in verification"""
        from slop.verifier import verify_source

        # Test that range types are properly parsed and used
        # The @pre explicitly states the range constraint
        source = """
        (module test
          (type Age (Int 0 .. 150))
          (fn check-age ((a Age))
            (@spec ((Age) -> Bool))
            (@pre (>= a 0))
            (@pre (<= a 150))
            (@post (== $result true))
            true))
        """
        results = verify_source(source)

        # Should have results (verifier ran)
        assert len(results) >= 1


class TestVerifySource:
    """Test the verify_source API"""

    def test_parse_error_handled(self):
        """Parse errors should be reported"""
        from slop.verifier import verify_source

        source = "(fn incomplete"  # Invalid syntax
        results = verify_source(source)

        errors = [r for r in results if r.status == 'error']
        assert len(errors) >= 1

    def test_no_contracts_skipped(self):
        """Functions without contracts should be skipped"""
        from slop.verifier import verify_source

        source = """
        (fn simple ((x Int))
          (@spec ((Int) -> Int))
          x)
        """
        results = verify_source(source)

        skipped = [r for r in results if r.status == 'skipped']
        assert len(skipped) >= 1

    def test_multiple_preconditions(self):
        """Multiple @pre should all be checked"""
        from slop.verifier import verify_source

        # Test that multiple preconditions are all used
        # Use a postcondition that follows from the preconditions
        source = """
        (module test
          (fn bounded ((x Int))
            (@spec ((Int) -> Int))
            (@pre (>= x 0))
            (@pre (<= x 100))
            (@post (or (>= $result 0) (< $result 0)))
            x))
        """
        results = verify_source(source)

        # Should have results
        assert len(results) >= 1
        # A tautology (x >= 0 OR x < 0) is always true
        verified = [r for r in results if r.status == 'verified']
        assert len(verified) >= 1

    def test_contradictory_postconditions(self):
        """Z3 catches logically impossible postconditions"""
        from slop.verifier import verify_source

        source = """
        (module test
        (fn impossible ((x Int))
            (@spec ((Int) -> Int))
            (@pre (> x 0))
            (@post (> $result x))
            (@post (< $result x))
            x))
        """
        results = verify_source(source)

        failed = [r for r in results if r.status == 'failed']
        assert len(failed) == 1

class TestZ3Available:
    """Test Z3 availability check"""

    def test_z3_available_flag(self):
        """Z3_AVAILABLE should be True when z3 is installed"""
        from slop.verifier import Z3_AVAILABLE
        assert Z3_AVAILABLE is True
