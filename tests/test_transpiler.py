"""
Transpiler tests for SLOP
"""

import pytest
from slop.transpiler import transpile, Transpiler
from slop.parser import parse


class TestTypeDefinitions:
    """Test type transpilation"""

    def test_range_type(self):
        source = "(type Age (Int 0 .. 150))"
        c_code = transpile(source)
        assert "typedef" in c_code
        assert "Age" in c_code
        assert "Age_new" in c_code
        assert "SLOP_PRE" in c_code

    def test_record_type(self):
        source = "(type User (record (name String) (age Int)))"
        c_code = transpile(source)
        # Records use struct Name { ... } syntax (with forward typedef in modules)
        assert "struct User" in c_code
        assert "User" in c_code
        assert "name" in c_code
        assert "age" in c_code

    def test_enum_type(self):
        source = "(type Status (enum active inactive))"
        c_code = transpile(source)
        assert "typedef enum" in c_code
        assert "Status_active" in c_code
        assert "Status_inactive" in c_code


class TestFunctionTranspilation:
    """Test function transpilation"""

    def test_simple_function(self):
        source = """
        (fn add ((x Int) (y Int))
          (@intent "Add two numbers")
          (@spec ((Int Int) -> Int))
          (+ x y))
        """
        c_code = transpile(source)
        assert "int64_t add" in c_code
        assert "(x + y)" in c_code

    def test_function_with_precondition(self):
        source = """
        (fn safe-div ((x Int) (y Int))
          (@intent "Divide safely")
          (@spec ((Int Int) -> Int))
          (@pre (!= y 0))
          (/ x y))
        """
        c_code = transpile(source)
        assert "SLOP_PRE" in c_code
        assert "(y != 0)" in c_code


class TestFieldAccess:
    """Test field access transpilation"""

    def test_pointer_field_access(self):
        source = """
        (type Point (record (x Int) (y Int)))
        (fn get-x ((p (Ptr Point)))
          (@spec (((Ptr Point)) -> Int))
          (. p x))
        """
        c_code = transpile(source)
        # Should use -> for pointer access
        assert "p->x" in c_code

    def test_value_field_access(self):
        source = """
        (type Point (record (x Int) (y Int)))
        (fn get-x ((p Point))
          (@spec ((Point) -> Int))
          (. p x))
        """
        c_code = transpile(source)
        # Should use . for value access
        assert "(p).x" in c_code

    def test_let_pointer_field_access(self):
        source = """
        (type Data (record (value Int)))
        (fn test ((arena Arena))
          (@spec ((Arena) -> Int))
          (let ((d (arena-alloc arena (sizeof Data))))
            (. d value)))
        """
        c_code = transpile(source)
        # d is assigned from arena-alloc, so it's a pointer
        assert "d->value" in c_code


class TestControlFlow:
    """Test control flow transpilation"""

    def test_if_expression(self):
        source = """
        (fn max ((x Int) (y Int))
          (@spec ((Int Int) -> Int))
          (if (> x y) x y))
        """
        c_code = transpile(source)
        assert "?" in c_code or "if" in c_code

    def test_when_statement(self):
        source = """
        (fn check ((x Int))
          (@spec ((Int) -> Unit))
          (when (> x 0)
            (println "positive")))
        """
        c_code = transpile(source)
        assert "if" in c_code

    def test_let_binding(self):
        source = """
        (fn test ()
          (@spec (() -> Int))
          (let ((x 1) (y 2))
            (+ x y)))
        """
        c_code = transpile(source)
        assert "__auto_type x = 1" in c_code
        assert "__auto_type y = 2" in c_code


class TestEnumValues:
    """Test enum value transpilation"""

    def test_quoted_enum(self):
        source = """
        (type Status (enum ok error))
        (fn get-status ()
          (@spec (() -> Status))
          'ok)
        """
        c_code = transpile(source)
        assert "Status_ok" in c_code

    def test_enum_in_condition(self):
        source = """
        (type Result (enum success failure))
        (fn check ((r Result))
          (@spec ((Result) -> Bool))
          (== r 'success))
        """
        c_code = transpile(source)
        assert "Result_success" in c_code


class TestExamples:
    """Test transpilation of example files"""

    def test_transpile_rate_limiter(self, rate_limiter_source):
        c_code = transpile(rate_limiter_source)
        assert "Limiter" in c_code
        assert "AcquireResult" in c_code
        assert "limiter_new" in c_code
        assert "acquire" in c_code
        # Check pointer access is correct
        assert "limiter->tokens" in c_code
        assert "limiter->refill_rate" in c_code

    def test_transpile_hello(self, hello_source):
        c_code = transpile(hello_source)
        assert "main" in c_code
        assert "Hello" in c_code or "printf" in c_code


class TestComprehensiveTranspilation:
    """Integration test exercising all transpiler features"""

    def test_comprehensive_transpilation(self, comprehensive_source):
        """Verify all major features transpile correctly"""
        c_code = transpile(comprehensive_source)

        # Union type generates tagged union
        assert "uint8_t tag;" in c_code
        assert "union {" in c_code
        assert "Value_number_TAG" in c_code

        # For loop
        assert "for (int64_t i = 0; i < 10; i++)" in c_code

        # While loop
        assert "while ((n > 0))" in c_code

        # Enum comparison (cond with equality checks)
        assert "(s == GameState_waiting)" in c_code
        assert "(s == GameState_playing)" in c_code

        # Match on union generates switch
        assert "switch (" in c_code

        # Match on union with binding
        assert "__auto_type n = " in c_code  # binding from (number n)

        # Result constructors
        assert ".tag = 0, .data.ok =" in c_code  # ok
        assert ".tag = 1, .data.err =" in c_code  # error

        # Early return with ?
        assert "if (_tmp.tag != 0) return _tmp" in c_code

        # Array indexing
        assert "scores[i]" in c_code

        # Bitwise operators
        assert "(a & b)" in c_code
        assert " | " in c_code  # or operator
        assert "(b << 2)" in c_code
        assert " ^ " in c_code  # xor operator

        # cond generates if/else if chain
        assert "} else if (" in c_code

        # with-arena
        assert "slop_arena_new(1024)" in c_code
        assert "slop_arena_free" in c_code

        # addr operator
        assert "(&x)" in c_code

        # cast operator
        assert "((uint8_t)(n))" in c_code

        # c-inline escape
        assert "return 42;" in c_code

        # Postcondition with $result
        assert "SLOP_POST(" in c_code
        assert "_retval" in c_code

        # Generated Option type
        assert "slop_option_" in c_code

        # Generated Result type
        assert "slop_result_" in c_code

        # Nested function call
        assert "classify(bitwise_ops(a, b))" in c_code

        # do sequence (println followed by expression)
        assert 'printf("%s\\n"' in c_code
