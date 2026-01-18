"""
Transpiler tests for SLOP
"""

import pytest
from slop.transpiler import transpile, Transpiler, TranspileError
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

    def test_record_new_with_inline_type(self):
        """Test record-new with inline anonymous record type instead of named type."""
        source = '''
        (module test
          (fn make-pair ()
            (@intent "Create a pair")
            (@spec (() -> (record (x Int) (y Int))))
            (record-new (record (x Int) (y Int)) (x 1) (y 2))))
        '''
        result = transpile(source)
        # Uses inline struct for anonymous record type
        assert 'struct {' in result
        assert '.x = 1' in result
        assert '.y = 2' in result

    def test_record_positional_constructor(self):
        """Test (TypeName val1 val2) generates C struct literal, not function call."""
        source = '''
        (module test
          (type Point (record (x Int) (y Int)))
          (fn make-point ((x Int) (y Int))
            (@intent "Create a point")
            (@spec ((Int Int) -> Point))
            (Point x y)))
        '''
        result = transpile(source)
        # Should generate C struct literal with designated initializers
        assert '.x = x' in result
        assert '.y = y' in result
        # Should NOT generate a function call
        assert 'test_Point(x, y)' not in result
        assert 'Point(x, y)' not in result

    def test_record_positional_constructor_with_expressions(self):
        """Test positional constructor with complex expressions."""
        source = '''
        (module test
          (type Point (record (x Int) (y Int)))
          (fn make-doubled-point ((a Int) (b Int))
            (@intent "Create a point with doubled values")
            (@spec ((Int Int) -> Point))
            (Point (* a 2) (+ b 1))))
        '''
        result = transpile(source)
        # Should generate struct literal with expressions
        assert '.x = (a * 2)' in result
        assert '.y = (b + 1)' in result


class TestOptionType:
    """Test Option type transpilation"""

    def test_none_symbol_in_if_expression(self):
        """Test that 'none' as a symbol generates proper Option struct literal."""
        source = '''
        (module test
          (fn safe-div ((a Int) (b Int))
            (@intent "Safe division")
            (@spec ((Int Int) -> (Option Int)))
            (if (== b 0)
                none
                (some (/ a b)))))
        '''
        result = transpile(source)
        # Should generate proper struct literal for none
        assert '{ .has_value = false }' in result
        # Should NOT have bare 'none' identifier
        assert 'return' in result and 'none;' not in result

    def test_some_with_value(self):
        """Test that (some val) generates proper Option struct literal."""
        source = '''
        (module test
          (fn make-some ((x Int))
            (@intent "Wrap in Some")
            (@spec ((Int) -> (Option Int)))
            (some x)))
        '''
        result = transpile(source)
        assert '{ .has_value = true' in result
        assert '.value = x' in result


class TestCollectionTypes:
    """Test List and Map type transpilation"""

    def test_list_new_with_type_parameter(self):
        """Test (list-new arena Type) generates properly typed list."""
        source = '''
        (module test
          (fn make-list ((arena Arena))
            (@intent "Create an int list")
            (@spec ((Arena) -> (List Int)))
            (list-new arena Int)))
        '''
        result = transpile(source)
        # Should generate typed list struct (int64_t normalized to int in identifiers)
        assert 'slop_list_int' in result
        # Should cast data pointer to element type
        assert 'int64_t*)slop_arena_alloc' in result

    def test_map_new_with_type_parameters(self):
        """Test (map-new arena KeyType ValueType) generates properly typed map."""
        source = '''
        (module test
          (fn make-map ((arena Arena))
            (@intent "Create a string->int map")
            (@spec ((Arena) -> (Map String Int)))
            (map-new arena String Int)))
        '''
        result = transpile(source)
        # Should generate typed map constructor (int64_t normalized to int in identifiers)
        assert 'slop_map_string_int_new' in result


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

    def test_match_enum_unquoted_variant(self):
        """Test that unquoted enum variants in match generate correct case labels."""
        source = """
        (module test
          (type Status (enum ok error))
          (fn handle ((s Status))
            (@intent "Handle status")
            (@spec ((Status) -> Int))
            (match s
              (ok 1)
              (error 0))))
        """
        c_code = transpile(source)
        # Should use enum constant names, not indices
        # Note: single-file transpile doesn't add module prefix
        assert "case Status_ok:" in c_code
        assert "case Status_error:" in c_code
        assert "case 0:" not in c_code
        assert "case 1:" not in c_code


class TestPointerTypeIdentifiers:
    """Test that pointer types generate valid C identifiers."""

    def test_result_with_pointer_type(self):
        """Test Result<Ptr<T>, E> generates valid type name."""
        source = """
        (module test
          (type Pet (record (name String)))
          (type ApiError (enum not-found))
          (fn get-pet ()
            (@intent "Get pet")
            (@spec (() -> (Result (Ptr Pet) ApiError)))
            (error 'not-found)))
        """
        c_code = transpile(source)
        # Should have _ptr instead of * in type name
        # Note: single-file transpile doesn't add module prefix
        assert "slop_result_Pet_ptr_ApiError" in c_code
        # Should NOT have * in identifier
        assert "slop_result_Pet*" not in c_code


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

        # While loop (using mutable parameter directly)
        assert "while ((n > 0))" in c_code

        # Enum comparison (cond with equality checks)
        assert "(s == GameState_waiting)" in c_code
        assert "(s == GameState_playing)" in c_code

        # Match on union generates switch
        assert "switch (" in c_code

        # Match on union with binding
        assert "__auto_type n = " in c_code  # binding from (number n)

        # Result constructors
        assert ".is_ok = true, .data.ok =" in c_code  # ok
        assert ".is_ok = false, .data.err =" in c_code  # error

        # Early return with ?
        assert "if (!_tmp.is_ok) return _tmp" in c_code

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

        # Union struct should be prefixed
        assert "struct mymod_Result {" in c_code
        # Union typedef should be prefixed
        assert "typedef struct mymod_Result mymod_Result;" in c_code
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

    def test_mut_mode_value(self):
        """'mut' mode with value type generates mutable value parameter"""
        source = """
        (fn increment ((mut counter Int))
          (@intent "Mutate counter")
          (@spec ((Int) -> Unit))
          (set! counter (+ counter 1)))
        """
        c_code = transpile(source)
        # mut on value type = mutable local copy, NOT pointer
        assert "int64_t counter" in c_code
        assert "int64_t* counter" not in c_code

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
        assert "int64_t state" in c_code  # mut on value type: mutable local copy
        assert "int64_t* state" not in c_code  # NOT a pointer


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

    def test_map_literal_with_explicit_types(self):
        """Map literal with explicit KeyType ValueType"""
        source = """
        (fn get-map ()
          (@intent "Return map with explicit types")
          (@spec (() -> (Map String Int)))
          (map String Int ("a" 1) ("b" 2)))
        """
        c_code = transpile(source)
        # Should have entries with keys and values (type args are stripped)
        assert ".key = " in c_code
        assert ".value = " in c_code


class TestListLiterals:
    """Test list literal transpilation"""

    def test_list_literal_with_explicit_type(self):
        """List literal with explicit element type"""
        source = """
        (fn get-list ()
          (@intent "Return list with explicit type")
          (@spec (() -> (List Int)))
          (list Int 1 2 3))
        """
        c_code = transpile(source)
        # Should have array with elements
        assert "1" in c_code
        assert "2" in c_code
        assert "3" in c_code
        assert ".len = 3" in c_code

    def test_list_literal_inferred_type(self):
        """List literal with inferred element type"""
        source = """
        (fn get-list ()
          (@intent "Return list with inferred type")
          (@spec (() -> (List Int)))
          (list 1 2 3))
        """
        c_code = transpile(source)
        # Should have array with elements
        assert ".len = 3" in c_code


class TestListOperations:
    """Test list operations transpilation"""

    def test_list_get_with_match_infers_option_type(self):
        """list-get with match should infer Option type from list element type"""
        source = """
        (module test
          (fn sum-first ((lst (List Int)))
            (@intent "Sum first element")
            (@spec (((List Int)) -> Int))
            (match (list-get lst 0)
              ((some val) val)
              ((none) 0))))
        """
        c_code = transpile(source)
        # Should generate slop_option_int (int64_t normalized to int)
        assert "slop_option_int" in c_code
        assert ".has_value" in c_code
        assert ".value" in c_code

    def test_list_get_from_list_new_var(self):
        """list-get on variable from list-new should infer element type"""
        source = """
        (module test
          (fn test-list ((arena Arena))
            (@intent "Test list operations")
            (@spec ((Arena) -> Int))
            (let ((nums (list-new arena Int)))
              (list-push nums 42)
              (match (list-get nums 0)
                ((some v) v)
                ((none) 0)))))
        """
        c_code = transpile(source)
        # Should use slop_option_int (int64_t normalized to int)
        assert "slop_option_int" in c_code

    def test_unwrap_on_option_uses_value(self):
        """unwrap on Option should use .value, not .data.ok"""
        source = """
        (module test
          (fn first-or-zero ((lst (List Int)))
            (@intent "Get first element or 0")
            (@spec (((List Int)) -> Int))
            (if (> (list-len lst) 0)
              (unwrap (list-get lst 0))
              0)))
        """
        c_code = transpile(source)
        # Should use .value for Option, not .data.ok
        assert ".value)" in c_code
        assert ".data.ok" not in c_code


class TestMatchExpressions:
    """Test match expression transpilation"""

    def test_match_in_while_with_bool_result(self):
        """Match expression returning bool in while condition should use bool type"""
        source = """
        (module test
          (fn find-above ((lst (List Int)) (threshold Int))
            (@intent "Find first above threshold")
            (@spec (((List Int) Int) -> (Option Int)))
            (let ((result none))
              (while (match result ((none) true) (else false))
                (set! result (some 1)))
              result)))
        """
        c_code = transpile(source)
        # Should use bool type for match result, not slop_result
        assert "bool _match_result" in c_code
        assert "slop_result _match_result" not in c_code


class TestFieldAccess:
    """Test field access transpilation"""

    def test_list_get_on_field_access_infers_element_type(self):
        """list-get on list from record field should infer correct element type"""
        source = """
        (module test
          (type Container (record (items (List Int))))
          (fn first-item ((c (Ptr Container)))
            (@intent "Get first item")
            (@spec (((Ptr Container)) -> Int))
            (let ((items (. c items)))
              (match (list-get items 0)
                ((some v) v)
                (none 0)))))
        """
        c_code = transpile(source)
        # Should use slop_option_int (int64_t normalized to int)
        assert "slop_option_int" in c_code
        assert ".has_value" in c_code


class TestMapOperations:
    """Test map operations transpilation"""

    def test_map_put_uses_typed_function_for_string_keyed_maps(self):
        """map-put on string-keyed map should use typed put function"""
        source = """
        (module test
          (fn test-map ((arena Arena))
            (@intent "Test map operations")
            (@spec ((Arena) -> Int))
            (let ((m (map-new arena String Int)))
              (map-put m "key" 42)
              1)))
        """
        c_code = transpile(source)
        # Should use typed map functions, not generic map_put
        assert "slop_map_string_int_put" in c_code
        assert "&m" in c_code  # map passed by pointer

    def test_map_keys_transpilation(self):
        """map-keys should generate correct C code"""
        source = """
        (module test
          (fn get-keys ((arena Arena))
            (@intent "Get map keys")
            (@spec ((Arena) -> (List String)))
            (let ((m (map-new arena String Int)))
              (map-keys m))))
        """
        c_code = transpile(source)
        assert "slop_map_keys" in c_code

    def test_map_remove_transpilation(self):
        """map-remove should generate correct C code for string-keyed maps"""
        source = """
        (module test
          (fn remove-key ((arena Arena))
            (@intent "Remove from map")
            (@spec ((Arena) -> Unit))
            (let ((m (map-new arena String Int)))
              (do
                (map-put m "key" 42)
                (map-remove m "key")))))
        """
        c_code = transpile(source)
        assert "slop_map_remove" in c_code


class TestMultiModuleTypes:
    """Test cross-module type resolution"""

    def test_imported_type_alias_uses_source_module_prefix(self):
        """Imported type alias should use source module prefix, not current module"""
        from pathlib import Path
        from slop.transpiler import transpile_multi_split
        from slop.resolver import ModuleInfo
        from slop.parser import ImportSpec

        # Module 'math' defines Score type
        math_source = """
        (module math
          (export Score)
          (type Score (Int 0 .. 100)))
        """
        math_ast = parse(math_source)

        # Module 'main' imports and uses Score
        main_source = """
        (module main
          (import math (Score))
          (fn process ((s Score))
            (@intent "Process score")
            (@spec ((Score) -> Int))
            s))
        """
        main_ast = parse(main_source)

        # Create module info
        math_info = ModuleInfo(name='math', path=Path('math.slop'), ast=math_ast)
        math_info.exports = {'Score'}
        main_info = ModuleInfo(name='main', path=Path('main.slop'), ast=main_ast)
        main_info.imports = [ImportSpec(module_name='math', symbols=['Score'])]

        # Transpile multi-module
        modules = {'math': math_info, 'main': main_info}
        order = ['math', 'main']
        results = transpile_multi_split(modules, order)

        # main.h should have math_Score, not main_Score
        main_header = results['main'][0]
        assert 'math_Score' in main_header
        assert 'main_Score' not in main_header


class TestTranspilerValidation:
    """Test that transpiler rejects malformed input instead of generating invalid C"""

    def test_const_too_many_args_error(self):
        """Malformed const with extra args should raise error."""
        source = "(const FOO Bar Baz 42)"  # 4 args instead of 3
        with pytest.raises(TranspileError) as exc:
            transpile(source)
        assert "3 arguments" in str(exc.value)

    def test_const_too_few_args_error(self):
        """Malformed const with missing args should raise error."""
        source = "(const FOO Int)"  # 2 args instead of 3
        with pytest.raises(TranspileError) as exc:
            transpile(source)
        assert "3 arguments" in str(exc.value)

    def test_valid_const_works(self):
        """Valid const should transpile without error."""
        source = "(const FOO Int 42)"
        c_code = transpile(source)
        assert "FOO" in c_code
        assert "42" in c_code

    def test_field_access_requires_symbol(self):
        """Field access with non-symbol should raise TranspileError with line."""
        source = "(fn test () (@spec (() -> Int)) (. x 42))"
        with pytest.raises(TranspileError) as exc:
            transpile(source)
        assert "Field name must be a symbol" in str(exc.value)
        assert "line" in str(exc.value)


class TestDerefPointerTracking:
    """Test that deref and addr operators are correctly tracked for field access."""

    def test_deref_uses_dot_not_arrow(self):
        """(. (deref ptr) field) should use . not -> since deref yields value"""
        source = """
        (type Point (record (x Int) (y Int)))
        (fn get-x ((p (Ptr Point)))
          (@spec (((Ptr Point)) -> Int))
          (. (deref p) x))
        """
        c_code = transpile(source)
        # deref yields value, so should use . not ->
        # May have extra parens: ((*p)).x or (*p).x
        assert ".x" in c_code and "*p" in c_code
        assert "->x" not in c_code

    def test_addr_uses_arrow(self):
        """Field access on (addr val) should use -> since addr yields pointer"""
        source = """
        (type Point (record (x Int) (y Int)))
        (fn set-x ((p Point))
          (@spec ((Point) -> Int))
          (. (addr p) x))
        """
        c_code = transpile(source)
        # addr yields pointer, so should use -> not .
        # May have extra parens: ((&p))->x or (&p)->x
        assert "->x" in c_code and "&p" in c_code


class TestStringPointerCast:
    """Test that string literals cast to pointers emit raw C strings."""

    def test_string_literal_cast_to_ptr_void(self):
        """String literal cast to (Ptr Void) should emit raw C string"""
        source = """
        (ffi "stdio.h" (fwrite ((buf (Ptr Void)) (size U64) (count U64) (file (Ptr Void))) U64))
        (fn write-newline ((f (Ptr Void)))
          (@spec (((Ptr Void)) -> U64))
          (fwrite (cast (Ptr Void) "\\n") 1 1 f))
        """
        c_code = transpile(source)
        # Should NOT wrap in SLOP_STR when casting to pointer
        # Check that raw C string with void* cast is used
        assert '(void*)"\\n"' in c_code
        assert 'SLOP_STR' not in c_code


class TestFfiStructCName:
    """Test ffi-struct with :c-name parameter."""

    def test_ffi_struct_c_name_override(self):
        """ffi-struct with :c-name should use the specified C type name"""
        source = """
        (ffi-struct "sys/stat.h" stat_buf :c-name "stat"
          (st_size I64))
        (fn get-size ((s (Ptr stat_buf)))
          (@spec (((Ptr stat_buf)) -> I64))
          (. s st_size))
        """
        c_code = transpile(source)
        # struct prefix is added automatically for non-_t types
        assert "struct stat" in c_code
        assert "struct stat_buf" not in c_code


class TestFfiConstants:
    """Test FFI constant declarations."""

    def test_ffi_const_no_code_emitted(self):
        """ffi constants should not emit #define or static const"""
        source = """
        (ffi "limits.h"
          (INT_MAX Int))
        (fn get-max ()
          (@spec (() -> Int))
          INT_MAX)
        """
        c_code = transpile(source)
        assert '#include <limits.h>' in c_code
        assert '#define INT_MAX' not in c_code
        assert 'static const' not in c_code
        assert 'INT_MAX' in c_code  # Used in function body

    def test_ffi_mixed_funcs_and_consts(self):
        """ffi can declare both functions and constants"""
        source = """
        (ffi "stdio.h"
          (EOF Int)
          (fclose ((file (Ptr Void))) Int))
        (fn check-eof ((result Int))
          (@spec ((Int) -> Bool))
          (== result EOF))
        """
        c_code = transpile(source)
        assert '#include <stdio.h>' in c_code
        assert '#define EOF' not in c_code
        assert 'EOF' in c_code  # Used in comparison


class TestScopedPtr:
    """Test ScopedPtr type and cleanup generation"""

    def test_scoped_ptr_type_generates_pointer(self):
        """ScopedPtr generates C pointer type"""
        source = """
        (type Container (record (data (ScopedPtr U8))))
        """
        c_code = transpile(source)
        assert "uint8_t*" in c_code
        assert "data" in c_code

    def test_record_with_scoped_ptr_generates_destructor(self):
        """Records with ScopedPtr fields generate destructor functions"""
        source = """
        (type Buffer (record
          (data (ScopedPtr U8))
          (size U64)))
        """
        c_code = transpile(source)
        assert "Buffer_free" in c_code
        assert "if (!ptr) return;" in c_code
        assert "if (ptr->data) free(ptr->data);" in c_code
        assert "free(ptr);" in c_code

    def test_let_with_scoped_ptr_generates_cleanup(self):
        """let bindings with ScopedPtr generate cleanup at scope end"""
        source = """
        (fn test ()
          (@spec (() -> Int))
          (let ((p (ScopedPtr U8) (malloc 100)))
            (use p)
            42))
        """
        c_code = transpile(source)
        # Should have cleanup before the return
        assert "if (p) free(p);" in c_code

    def test_nested_scoped_ptr_records(self):
        """Nested records with ScopedPtr get proper destructor chaining"""
        source = """
        (type Inner (record (buf (ScopedPtr U8))))
        (type Outer (record (inner (ScopedPtr Inner))))
        """
        c_code = transpile(source)
        # Both should have destructors
        assert "Inner_free" in c_code
        assert "Outer_free" in c_code
        # Outer's destructor should call Inner_free
        assert "Inner_free(ptr->inner)" in c_code

    def test_multiple_scoped_ptr_fields(self):
        """Multiple ScopedPtr fields all generate cleanup"""
        source = """
        (type Connection (record
          (socket (ScopedPtr U8))
          (read-buf (ScopedPtr U8))
          (write-buf (ScopedPtr U8))))
        """
        c_code = transpile(source)
        assert "Connection_free" in c_code
        assert "ptr->socket" in c_code
        assert "ptr->read_buf" in c_code
        assert "ptr->write_buf" in c_code


class TestNativeTranspilerContracts:
    """Test that native transpiler generates contract checks"""

    @pytest.fixture
    def native_transpiler(self):
        """Check if native transpiler is available"""
        from pathlib import Path
        binary = Path(__file__).parent.parent / "bin" / "slop-transpiler"
        if not binary.exists():
            pytest.skip("Native transpiler not available")
        return binary

    def run_native_transpile(self, native_bin, source: str) -> str:
        """Run native transpiler on source and return C code"""
        import subprocess
        import json
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode='w', suffix='.slop', delete=False) as f:
            f.write(source)
            f.flush()
            temp_path = f.name

        try:
            result = subprocess.run(
                [str(native_bin), temp_path],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                pytest.fail(f"Native transpiler failed: {result.stderr}")
            data = json.loads(result.stdout)
            # Combine header and impl from all modules
            parts = []
            for mod_data in data.values():
                parts.append(mod_data.get('header', ''))
                parts.append(mod_data.get('impl', ''))
            return '\n'.join(parts)
        finally:
            os.unlink(temp_path)

    def test_native_emits_precondition(self, native_transpiler):
        """Native transpiler emits SLOP_PRE for @pre"""
        source = """
        (module test
          (fn safe-div ((x Int) (y Int))
            (@intent "Divide safely")
            (@spec ((Int Int) -> Int))
            (@pre (!= y 0))
            (/ x y)))
        """
        c_code = self.run_native_transpile(native_transpiler, source)
        assert "SLOP_PRE" in c_code
        assert "(y != 0)" in c_code

    def test_native_emits_postcondition(self, native_transpiler):
        """Native transpiler emits SLOP_POST for @post"""
        source = """
        (module test
          (fn positive ((x Int))
            (@intent "Return positive value")
            (@spec ((Int) -> Int))
            (@post (>= $result 0))
            (if (< x 0) (- 0 x) x)))
        """
        c_code = self.run_native_transpile(native_transpiler, source)
        assert "SLOP_POST" in c_code
        assert "_retval" in c_code

    def test_native_emits_range_bounds_check(self, native_transpiler):
        """Native transpiler emits bounds check for range types"""
        source = """
        (module test
          (type Percentage (Int 0 .. 100)))
        """
        c_code = self.run_native_transpile(native_transpiler, source)
        assert "typedef" in c_code
        assert "Percentage" in c_code
        assert "Percentage_new" in c_code
        assert "SLOP_PRE" in c_code
        assert "v >= 0" in c_code
        assert "v <= 100" in c_code

    def test_native_selects_smallest_c_type_for_range(self, native_transpiler):
        """Native transpiler selects smallest C type for range"""
        source = """
        (module test
          (type Byte (Int 0 .. 255)))
        """
        c_code = self.run_native_transpile(native_transpiler, source)
        assert "uint8_t" in c_code

    def test_native_emits_multiple_preconditions(self, native_transpiler):
        """Native transpiler emits multiple SLOP_PRE for multiple @pre"""
        source = """
        (module test
          (fn clamp ((x Int) (min-val Int) (max-val Int))
            (@intent "Clamp value")
            (@spec ((Int Int Int) -> Int))
            (@pre (<= min-val max-val))
            (@pre (>= x 0))
            (if (< x min-val) min-val (if (> x max-val) max-val x))))
        """
        c_code = self.run_native_transpile(native_transpiler, source)
        # Should have two SLOP_PRE calls
        assert c_code.count("SLOP_PRE") >= 2


class TestFunctionCNameOverride:
    """Test :c-name attribute for functions"""

    def test_c_name_override_in_forward_declaration(self):
        """Function with :c-name uses clean name in forward declaration"""
        source = '''
        (module mymod
          (fn my-parse-int ((s (Ptr Char)))
            (@intent "Parse integer from string")
            (@spec (((Ptr Char)) -> Int))
            0
            :c-name "parse_int"))
        '''
        result = transpile(source)
        # Forward declaration should use clean name
        assert "int64_t parse_int(char* s);" in result

    def test_c_name_override_in_function_definition(self):
        """Function with :c-name uses clean name in definition"""
        source = '''
        (module mymod
          (fn my-parse-int ((s (Ptr Char)))
            (@intent "Parse integer from string")
            (@spec (((Ptr Char)) -> Int))
            42
            :c-name "parse_int"))
        '''
        result = transpile(source)
        # Function definition should use clean name
        assert "int64_t parse_int(char* s) {" in result

    def test_c_name_override_emits_alias(self):
        """Function with :c-name emits alias macro for SLOP-named function"""
        source = '''
        (module mymod
          (fn my-parse-int ((s (Ptr Char)))
            (@intent "Parse integer from string")
            (@spec (((Ptr Char)) -> Int))
            42
            :c-name "parse_int"))
        '''
        result = transpile(source)
        # Should emit alias: #define my_parse_int parse_int
        # (without module prefix since prefixing is not enabled in default transpile)
        assert "#define my_parse_int parse_int" in result
        assert "/* Function name aliases for C interop */" in result

    def test_c_name_no_override_uses_default_name(self):
        """Function without :c-name uses default converted name"""
        source = '''
        (module mymod
          (fn my-helper ((x Int))
            (@intent "Helper function")
            (@spec ((Int) -> Int))
            x))
        '''
        result = transpile(source)
        # Should use default name (hyphens converted to underscores)
        assert "int64_t my_helper(int64_t x);" in result
        assert "int64_t my_helper(int64_t x) {" in result

    def test_c_name_with_multiple_functions(self):
        """Multiple functions with :c-name override work correctly"""
        source = '''
        (module mymod
          (fn parse-int ((s (Ptr Char)))
            (@intent "Parse integer")
            (@spec (((Ptr Char)) -> Int))
            0
            :c-name "parse_int")
          (fn format-int ((n Int))
            (@intent "Format integer")
            (@spec ((Int) -> String))
            "0"
            :c-name "format_int"))
        '''
        result = transpile(source)
        # Both should have clean names in forward declarations
        assert "int64_t parse_int(char* s);" in result
        assert "slop_string format_int(int64_t n);" in result

    def test_c_name_body_not_compiled(self):
        """The :c-name attribute should not appear in the function body"""
        source = '''
        (module mymod
          (fn my-func ()
            (@intent "Test function")
            (@spec (() -> Int))
            42
            :c-name "my_func"))
        '''
        result = transpile(source)
        # Should not have :c-name or "my_func" string as body code
        assert ':c_name' not in result
        assert 'SLOP_STR("my_func")' not in result
        # But should use the clean name
        assert "int64_t my_func(void)" in result


class TestPublicHeaderGeneration:
    """Test public API header generation for library builds"""

    def test_generate_public_header_with_c_name_function(self):
        """Functions with :c-name should appear in public header"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        source = '''
        (module mylib
          (type Config (record (timeout Int) (retries Int)))
          (fn parse-config ((s (Ptr Char)))
            (@intent "Parse config")
            (@spec (((Ptr Char)) -> (Ptr Config)))
            nil
            :c-name "mylib_parse_config")
          (fn internal-helper ((x Int))
            (@intent "Internal use only")
            (@spec ((Int) -> Int))
            (+ x 1)))
        '''
        ast = parse(source)
        transpiler = Transpiler()
        transpiler.transpile(ast)

        # Generate public header
        public_header = transpiler.generate_public_header("mylib")

        # Should have header guards
        assert "#ifndef MYLIB_PUBLIC_H" in public_header
        assert "#define MYLIB_PUBLIC_H" in public_header
        assert "#endif" in public_header

        # Should have standard includes
        assert "#include <stdint.h>" in public_header
        assert "#include <stdbool.h>" in public_header

        # Should have slop_string helper
        assert "slop_string" in public_header
        assert "slop_str" in public_header

        # Should have public function declaration
        assert "mylib_parse_config" in public_header

        # Should NOT have internal function
        assert "internal_helper" not in public_header
        assert "internal-helper" not in public_header

    def test_no_public_header_without_c_name_functions(self):
        """Modules without :c-name functions should return empty public header"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        source = '''
        (module mylib
          (fn my-func ((x Int))
            (@intent "A function")
            (@spec ((Int) -> Int))
            x))
        '''
        ast = parse(source)
        transpiler = Transpiler()
        transpiler.transpile(ast)

        # Generate public header - should be empty
        public_header = transpiler.generate_public_header("mylib")
        assert public_header == ""

    def test_public_header_multiple_c_name_functions(self):
        """Multiple :c-name functions should all appear in public header"""
        from slop.transpiler import Transpiler
        from slop.parser import parse

        source = '''
        (module mylib
          (fn parse-int ((s (Ptr Char)))
            (@intent "Parse integer")
            (@spec (((Ptr Char)) -> Int))
            0
            :c-name "mylib_parse_int")
          (fn format-int ((n Int))
            (@intent "Format integer")
            (@spec ((Int) -> String))
            "0"
            :c-name "mylib_format_int"))
        '''
        ast = parse(source)
        transpiler = Transpiler()
        transpiler.transpile(ast)

        public_header = transpiler.generate_public_header("mylib")

        # Both functions should appear
        assert "mylib_parse_int" in public_header
        assert "mylib_format_int" in public_header

    def test_transpile_multi_split_returns_public_header(self):
        """transpile_multi_split should return public header in tuple"""
        from slop.transpiler import transpile_multi_split
        from slop.resolver import ModuleInfo
        from slop.parser import parse

        source = '''
        (module mylib (export parse-config)
          (fn parse-config ((s (Ptr Char)))
            (@intent "Parse config")
            (@spec (((Ptr Char)) -> Int))
            0
            :c-name "mylib_parse_config"))
        '''
        ast = parse(source)

        # Create mock ModuleInfo
        from pathlib import Path
        mod_info = ModuleInfo(
            name="mylib",
            path=Path("test.slop"),
            ast=ast,
            imports=[],
            exports={"parse-config"}  # Set, not list
        )

        modules = {"mylib": mod_info}
        order = ["mylib"]

        results = transpile_multi_split(modules, order)

        # Result should be (header, impl, public_header)
        assert "mylib" in results
        result = results["mylib"]
        assert len(result) == 3

        header, impl, public_header = result
        assert header  # Should have content
        assert impl  # Should have content
        assert public_header  # Should have public header content
        assert "mylib_parse_config" in public_header
