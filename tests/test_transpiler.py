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


class TestModuleTypePrefixing:
    """Tests for type prefixing in multi-module builds"""

    def test_enum_type_prefixing(self):
        """Enum types should be prefixed with module name"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        code = """
        (module mymod
          (type Status (enum pending done failed)))
        """
        ast = parse(code)
        t = Transpiler()
        t.enable_prefixing = True
        t.current_module = "mymod"
        c_code = t.transpile(ast)

        # Enum type should be prefixed
        assert "mymod_Status" in c_code
        # Variants should be prefixed
        assert "mymod_Status_pending" in c_code
        assert "mymod_Status_done" in c_code
        assert "mymod_Status_failed" in c_code

    def test_record_type_prefixing(self):
        """Record types should be prefixed with module name"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        code = """
        (module mymod
          (type User (record (name String) (age Int))))
        """
        ast = parse(code)
        t = Transpiler()
        t.enable_prefixing = True
        t.current_module = "mymod"
        c_code = t.transpile(ast)

        # Struct should be prefixed
        assert "struct mymod_User" in c_code
        assert "typedef struct mymod_User mymod_User" in c_code

    def test_range_type_prefixing(self):
        """Range types should be prefixed with module name"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        code = """
        (module mymod
          (type UserId (Int 1 ..)))
        """
        ast = parse(code)
        t = Transpiler()
        t.enable_prefixing = True
        t.current_module = "mymod"
        c_code = t.transpile(ast)

        # Typedef should be prefixed
        assert "typedef" in c_code
        assert "mymod_UserId" in c_code
        # Constructor should be prefixed
        assert "mymod_UserId_new" in c_code

    def test_union_type_prefixing(self):
        """Union types should be prefixed with module name"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        code = """
        (module mymod
          (type Result (union (ok Int) (err String))))
        """
        ast = parse(code)
        t = Transpiler()
        t.enable_prefixing = True
        t.current_module = "mymod"
        c_code = t.transpile(ast)

        # Union typedef should be prefixed
        assert "} mymod_Result;" in c_code
        # Tag constants should be prefixed
        assert "mymod_Result_ok_TAG" in c_code
        assert "mymod_Result_err_TAG" in c_code

    def test_no_prefixing_without_enable(self):
        """Without enable_prefixing, types should not be prefixed"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        code = """
        (module mymod
          (type Status (enum pending done)))
        """
        ast = parse(code)
        t = Transpiler()
        t.enable_prefixing = False  # Default
        t.current_module = "mymod"
        c_code = t.transpile(ast)

        # Should NOT have module prefix
        assert "mymod_Status" not in c_code
        assert "typedef enum" in c_code
        assert "Status_pending" in c_code


class TestParameterModes:
    """Test parameter mode transpilation"""

    def test_in_mode_default(self):
        """Parameters without explicit mode default to 'in' (pass by value)"""
        source = """
        (fn process ((x Int))
          (@intent "Default in mode")
          (@spec ((Int) -> Int))
          x)
        """
        c_code = transpile(source)
        # Should pass by value (just "int64_t x")
        assert "int64_t x" in c_code
        assert "int64_t* x" not in c_code

    def test_in_mode_explicit(self):
        """Explicit 'in' mode passes by value"""
        source = """
        (fn process ((in x Int))
          (@intent "Explicit in mode")
          (@spec ((Int) -> Int))
          x)
        """
        c_code = transpile(source)
        assert "int64_t x" in c_code
        assert "int64_t* x" not in c_code

    def test_out_mode_pointer(self):
        """'out' mode generates pointer parameter"""
        source = """
        (fn init-result ((out result Int))
          (@intent "Output parameter")
          (@spec ((Int) -> Unit))
          (set! result 42))
        """
        c_code = transpile(source)
        # Should be pointer type
        assert "int64_t* result" in c_code

    def test_mut_mode_pointer(self):
        """'mut' mode generates pointer parameter"""
        source = """
        (fn increment ((mut counter Int))
          (@intent "Mutate counter")
          (@spec ((Int) -> Unit))
          (set! counter (+ counter 1)))
        """
        c_code = transpile(source)
        # Should be pointer type
        assert "int64_t* counter" in c_code

    def test_mixed_modes(self):
        """Function with mixed parameter modes"""
        source = """
        (fn compute ((in a Int) (out result Int) (mut state Int))
          (@intent "Mixed modes")
          (@spec ((Int Int Int) -> Unit))
          (set! result (+ a state)))
        """
        c_code = transpile(source)
        assert "int64_t a" in c_code  # in: pass by value
        assert "int64_t* result" in c_code  # out: pointer
        assert "int64_t* state" in c_code  # mut: pointer


class TestMapLiterals:
    """Test map literal transpilation"""

    def test_empty_map_literal(self):
        """Empty map literal should generate empty slop_map"""
        source = """
        (fn get-map ()
          (@intent "Return empty map")
          (@spec (() -> (Map String Int)))
          (map))
        """
        c_code = transpile(source)
        # Should have map struct with NULL entries
        assert ".entries = NULL" in c_code or "slop_map" in c_code

    def test_map_literal_with_pairs(self):
        """Map literal with key-value pairs"""
        source = """
        (fn get-map ()
          (@intent "Return map with values")
          (@spec (() -> (Map String Int)))
          (map ("a" 1) ("b" 2)))
        """
        c_code = transpile(source)
        # Should have entries with keys and values
        assert ".key = " in c_code
        assert ".value = " in c_code
        assert ".occupied = true" in c_code

    def test_map_type_generation(self):
        """Map type should generate proper C struct"""
        source = """
        (fn use-map ((m (Map String Int)))
          (@intent "Use a map")
          (@spec (((Map String Int)) -> Int))
          0)
        """
        c_code = transpile(source)
        # Should generate map type definition
        assert "slop_map_" in c_code or "entries" in c_code
