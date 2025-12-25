"""
Parser tests for SLOP
"""

import pytest
from slop.parser import parse, parse_file, pretty_print, find_holes, is_form, SList, Symbol, Number, String


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
        result = parse("'foo")
        assert len(result) == 1
        lst = result[0]
        assert is_form(lst, 'quote')
        assert lst[1].name == 'foo'

    def test_quoted_with_hyphen(self):
        result = parse("'rate-limited")
        assert len(result) == 1
        lst = result[0]
        assert is_form(lst, 'quote')
        assert lst[1].name == 'rate-limited'


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
