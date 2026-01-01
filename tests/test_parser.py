"""
Parser tests for SLOP
"""

import pytest
from slop.parser import parse, parse_file, pretty_print, find_holes, is_form, SList, Symbol, Number, String, ParseError


class TestBasicParsing:
    """Test basic S-expression parsing"""

    def test_parse_symbol(self):
        result = parse("foo")
        assert len(result) == 1
        assert isinstance(result[0], Symbol)
        assert result[0].name == "foo"

    def test_parse_number(self):
        result = parse("42")
        assert len(result) == 1
        assert isinstance(result[0], Number)
        assert result[0].value == 42

    def test_parse_negative_number(self):
        result = parse("-7")
        assert len(result) == 1
        assert isinstance(result[0], Number)
        assert result[0].value == -7

    def test_parse_string(self):
        result = parse('"hello world"')
        assert len(result) == 1
        assert isinstance(result[0], String)
        assert result[0].value == "hello world"

    def test_parse_list(self):
        result = parse("(a b c)")
        assert len(result) == 1
        assert isinstance(result[0], SList)
        assert len(result[0].items) == 3

    def test_parse_nested_list(self):
        result = parse("(a (b c) d)")
        assert len(result) == 1
        lst = result[0]
        assert isinstance(lst, SList)
        assert len(lst.items) == 3
        assert isinstance(lst.items[1], SList)


class TestRangeTypes:
    """Test range type parsing"""

    def test_bounded_range(self):
        result = parse("(Int 0 .. 100)")
        assert len(result) == 1
        lst = result[0]
        assert isinstance(lst, SList)
        assert lst[0].name == "Int"
        assert lst[1].value == 0
        assert lst[2].name == ".."
        assert lst[3].value == 100

    def test_lower_bounded_range(self):
        result = parse("(Int 0 ..)")
        assert len(result) == 1
        lst = result[0]
        assert lst[0].name == "Int"
        assert lst[1].value == 0
        assert lst[2].name == ".."

    def test_ptr_type(self):
        result = parse("(Ptr Limiter)")
        assert len(result) == 1
        lst = result[0]
        assert lst[0].name == "Ptr"
        assert lst[1].name == "Limiter"


class TestModuleParsing:
    """Test module and function parsing"""

    def test_simple_module(self):
        source = """
        (module test
          (type Age (Int 0 .. 150))
          (fn greet ((name String))
            (@intent "Say hello")
            (@spec ((String) -> String))
            name))
        """
        result = parse(source)
        assert len(result) == 1
        module = result[0]
        assert is_form(module, 'module')
        assert module[1].name == "test"

    def test_find_holes(self):
        source = """
        (fn test ()
          (hole Int "Fill this"))
        """
        result = parse(source)
        holes = find_holes(result[0])
        assert len(holes) == 1
        assert is_form(holes[0], 'hole')


class TestQuotedSymbols:
    """Test quoted symbol parsing"""

    def test_quoted_symbol(self):
        """'foo is normalized to Symbol("'foo") - not (quote foo) list"""
        result = parse("'foo")
        assert len(result) == 1
        sym = result[0]
        assert isinstance(sym, Symbol)
        assert sym.name == "'foo"

    def test_quoted_with_hyphen(self):
        """'rate-limited is normalized to Symbol("'rate-limited")"""
        result = parse("'rate-limited")
        assert len(result) == 1
        sym = result[0]
        assert isinstance(sym, Symbol)
        assert sym.name == "'rate-limited"

    def test_quote_form_normalized(self):
        """(quote x) is normalized to 'x"""
        result = parse("(quote foo)")
        assert len(result) == 1
        sym = result[0]
        assert isinstance(sym, Symbol)
        assert sym.name == "'foo"


class TestComments:
    """Test comment handling"""

    def test_line_comment(self):
        source = """
        ; This is a comment
        (foo bar)
        """
        result = parse(source)
        assert len(result) == 1
        assert is_form(result[0], 'foo')

    def test_inline_comment(self):
        source = "(foo ; comment\n bar)"
        result = parse(source)
        assert len(result) == 1
        assert len(result[0].items) == 2


class TestPrettyPrint:
    """Test pretty printing"""

    def test_roundtrip_simple(self):
        source = "(+ 1 2)"
        result = parse(source)
        printed = pretty_print(result[0])
        reparsed = parse(printed)
        assert len(reparsed) == 1

    def test_roundtrip_complex(self, rate_limiter_source):
        result = parse(rate_limiter_source)
        for form in result:
            printed = pretty_print(form)
            reparsed = parse(printed)
            assert len(reparsed) == 1


class TestExampleFiles:
    """Test parsing example files"""

    def test_parse_rate_limiter(self, rate_limiter_source):
        result = parse(rate_limiter_source)
        assert len(result) >= 1
        assert is_form(result[0], 'module')

    def test_parse_hello(self, hello_source):
        result = parse(hello_source)
        assert len(result) >= 1
        assert is_form(result[0], 'module')


class TestInfixContracts:
    """Test infix notation in contracts (@pre, @post, @assume)"""

    def test_simple_comparison(self):
        """Basic infix comparison: {x > 0} -> (> x 0)"""
        result = parse("(@pre {x > 0})")
        assert len(result) == 1
        pre = result[0]
        assert is_form(pre, '@pre')
        cond = pre[1]
        assert is_form(cond, '>')
        assert cond[1].name == 'x'
        assert cond[2].value == 0

    def test_arithmetic_precedence(self):
        """Multiplication binds tighter than addition: {a + b * c} -> (+ a (* b c))"""
        result = parse("(@pre {a + b * c == 0})")
        pre = result[0]
        cond = pre[1]  # (== (+ a (* b c)) 0)
        assert is_form(cond, '==')
        add = cond[1]  # (+ a (* b c))
        assert is_form(add, '+')
        assert add[1].name == 'a'
        mul = add[2]  # (* b c)
        assert is_form(mul, '*')
        assert mul[1].name == 'b'
        assert mul[2].name == 'c'

    def test_boolean_operators(self):
        """{x >= 0 and x <= 100} -> (and (>= x 0) (<= x 100))"""
        result = parse("(@pre {x >= 0 and x <= 100})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, 'and')
        left = cond[1]  # (>= x 0)
        assert is_form(left, '>=')
        right = cond[2]  # (<= x 100)
        assert is_form(right, '<=')

    def test_or_binds_looser_than_and(self):
        """{a or b and c} -> (or a (and b c))"""
        result = parse("(@pre {a or b and c})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, 'or')
        assert cond[1].name == 'a'
        and_expr = cond[2]
        assert is_form(and_expr, 'and')

    def test_grouping_with_parens(self):
        """Parentheses for grouping: {(a + b) * c} -> (* (+ a b) c)"""
        result = parse("(@pre {(a + b) * c == 0})")
        pre = result[0]
        cond = pre[1]  # (== (* (+ a b) c) 0)
        assert is_form(cond, '==')
        mul = cond[1]  # (* (+ a b) c)
        assert is_form(mul, '*')
        add = mul[1]  # (+ a b)
        assert is_form(add, '+')
        assert mul[2].name == 'c'

    def test_function_call_in_infix(self):
        """Prefix function call in infix: {(len arr) > 0} -> (> (len arr) 0)"""
        result = parse("(@pre {(len arr) > 0})")
        pre = result[0]
        cond = pre[1]  # (> (len arr) 0)
        assert is_form(cond, '>')
        call = cond[1]  # (len arr)
        assert is_form(call, 'len')
        assert call[1].name == 'arr'
        assert cond[2].value == 0

    def test_field_access_in_infix(self):
        """Field access in infix: {(. ptr field) > 0} -> (> (. ptr field) 0)"""
        result = parse("(@pre {(. ptr field) > 0})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, '>')
        access = cond[1]
        assert is_form(access, '.')

    def test_result_variable(self):
        """$result in postconditions: {$result == a + b}"""
        result = parse("(@post {$result == a + b})")
        post = result[0]
        cond = post[1]  # (== $result (+ a b))
        assert is_form(cond, '==')
        assert cond[1].name == '$result'
        add = cond[2]
        assert is_form(add, '+')

    def test_unary_not(self):
        """Unary not: {not valid} -> (not valid)"""
        result = parse("(@pre {not valid})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, 'not')
        assert cond[1].name == 'valid'

    def test_unary_minus(self):
        """Unary minus: {-x > 0} -> (> (- 0 x) 0)"""
        result = parse("(@pre {-x > 0})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, '>')
        neg = cond[1]  # (- 0 x)
        assert is_form(neg, '-')
        assert neg[1].value == 0
        assert neg[2].name == 'x'

    def test_complex_nested(self):
        """{a > 0 or (b < 10 and c != 0)} -> (or (> a 0) (and (< b 10) (!= c 0)))"""
        result = parse("(@pre {a > 0 or (b < 10 and c != 0)})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, 'or')
        left = cond[1]  # (> a 0)
        assert is_form(left, '>')
        right = cond[2]  # (and (< b 10) (!= c 0))
        assert is_form(right, 'and')

    def test_infix_in_pre(self):
        """Infix works in @pre"""
        result = parse("(@pre {x > 0})")
        assert is_form(result[0], '@pre')

    def test_infix_in_post(self):
        """Infix works in @post"""
        result = parse("(@post {$result >= 0})")
        assert is_form(result[0], '@post')

    def test_infix_in_assume(self):
        """Infix works in @assume"""
        result = parse("(@assume {ptr != nil})")
        assert is_form(result[0], '@assume')

    def test_infix_outside_contract_error(self):
        """Infix outside contract raises ParseError"""
        with pytest.raises(ParseError) as exc_info:
            parse("(let x {1 + 2})")
        assert "only allowed inside @pre, @post, or @assume" in str(exc_info.value)

    def test_infix_in_regular_expression_error(self):
        """Infix in regular code raises ParseError"""
        with pytest.raises(ParseError):
            parse("{x + y}")

    def test_prefix_still_works_in_contracts(self):
        """Prefix notation still works in contracts"""
        result = parse("(@pre (> x 0))")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, '>')
        assert cond[1].name == 'x'

    def test_mixed_function_and_grouping(self):
        """Mix of function calls and grouping in same expression"""
        result = parse("(@pre {(len arr) > 0 and (x + 1) < 10})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, 'and')
        # left: (> (len arr) 0)
        left = cond[1]
        assert is_form(left, '>')
        assert is_form(left[1], 'len')  # function call
        # right: (< (+ x 1) 10)
        right = cond[2]
        assert is_form(right, '<')
        assert is_form(right[1], '+')  # grouping became addition

    def test_quoted_symbol_in_infix(self):
        """Quoted symbols in infix: {status == 'ok}"""
        result = parse("(@post {status == 'ok})")
        post = result[0]
        cond = post[1]
        assert is_form(cond, '==')
        assert cond[1].name == 'status'
        # The quote is normalized to a Symbol with 'ok name
        quoted = cond[2]
        assert isinstance(quoted, Symbol)
        assert quoted.name == "'ok"

    def test_not_equal(self):
        """Not equal operator: {x != 0}"""
        result = parse("(@pre {x != 0})")
        pre = result[0]
        cond = pre[1]
        assert is_form(cond, '!=')

    def test_all_comparison_operators(self):
        """All comparison operators work"""
        ops = ['>', '<', '>=', '<=', '==', '!=']
        for op in ops:
            result = parse(f"(@pre {{x {op} 0}})")
            cond = result[0][1]
            assert is_form(cond, op), f"Failed for operator {op}"

    def test_all_arithmetic_operators(self):
        """All arithmetic operators work"""
        ops = ['+', '-', '*', '/', '%']
        for op in ops:
            result = parse(f"(@pre {{a {op} b == 0}})")
            eq = result[0][1]
            assert is_form(eq, '==')
            arith = eq[1]
            assert is_form(arith, op), f"Failed for operator {op}"
