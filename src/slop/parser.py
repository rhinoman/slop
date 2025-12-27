"""
SLOP Parser - S-expression parser for SLOP language

Intentionally simple - S-expressions are trivially parseable.
"""

from dataclasses import dataclass
from typing import Union, List, Optional, Any
import re


# AST Node Types

@dataclass
class Symbol:
    name: str
    line: int = 0
    col: int = 0
    def __repr__(self): return self.name

@dataclass
class String:
    value: str
    line: int = 0
    col: int = 0
    def __repr__(self):
        escaped = self.value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')
        return f'"{escaped}"'

@dataclass
class Number:
    value: Union[int, float]
    line: int = 0
    col: int = 0
    def __repr__(self): return str(self.value)

@dataclass
class SList:
    items: List['SExpr']
    line: int = 0
    col: int = 0

    def __repr__(self):
        return f"({' '.join(repr(x) for x in self.items)})"

    def __getitem__(self, idx): return self.items[idx]
    def __len__(self): return len(self.items)
    def __iter__(self): return iter(self.items)

SExpr = Union[Symbol, String, Number, SList]


def _unescape_string(s: str) -> str:
    """Unescape a string value, handling \\, \", \n, \t, \r"""
    result = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            next_char = s[i + 1]
            if next_char == 'n':
                result.append('\n')
            elif next_char == 't':
                result.append('\t')
            elif next_char == 'r':
                result.append('\r')
            elif next_char == '"':
                result.append('"')
            elif next_char == '\\':
                result.append('\\')
            else:
                # Unknown escape, keep as-is
                result.append(s[i])
                result.append(next_char)
            i += 2
        else:
            result.append(s[i])
            i += 1
    return ''.join(result)


class ParseError(Exception):
    def __init__(self, message: str, line: int = 0, col: int = 0):
        self.message = message
        self.line = line
        self.col = col
        super().__init__(f"Parse error at {line}:{col}: {message}")


class Lexer:
    TOKEN_PATTERNS = [
        ('COMMENT', r';[^\n]*'),
        ('WHITESPACE', r'\s+'),
        ('LPAREN', r'\('),
        ('RPAREN', r'\)'),
        ('STRING', r'"(?:[^"\\]|\\.)*"'),
        ('NUMBER', r'-?\d+\.?\d*'),
        ('QUOTE', r"'"),
        ('SYMBOL', r'[a-zA-Z_@$][a-zA-Z0-9_\-/*<>=!?.]*'),
        ('RANGE', r'\.\.'),
        ('OPERATOR', r'[+\-*/!<>=&|^%?]+|\.'),
        ('COLON', r':'),
    ]

    def __init__(self, source: str):
        self.source = source
        self.pattern = '|'.join(f'(?P<{name}>{pattern})'
                                for name, pattern in self.TOKEN_PATTERNS)
        self.regex = re.compile(self.pattern)

    def tokenize(self):
        tokens = []
        line, col = 1, 1
        pos = 0  # Track current position for gap detection

        for match in self.regex.finditer(self.source):
            # Check for gap (unrecognized characters)
            if match.start() > pos:
                bad_char = self.source[pos]
                raise ParseError(f"Unexpected character: '{bad_char}'", line, col)

            kind = match.lastgroup
            value = match.group()

            if kind not in ('COMMENT', 'WHITESPACE'):
                tokens.append((kind, value, line, col))

            newlines = value.count('\n')
            if newlines:
                line += newlines
                col = len(value) - value.rfind('\n')
            else:
                col += len(value)

            pos = match.end()

        # Check for trailing unrecognized content
        if pos < len(self.source):
            bad_char = self.source[pos]
            raise ParseError(f"Unexpected character: '{bad_char}'", line, col)

        return tokens


class Parser:
    def __init__(self, source: str):
        self.tokens = Lexer(source).tokenize()
        self.pos = 0

    def parse(self) -> List[SExpr]:
        forms = []
        while self.pos < len(self.tokens):
            forms.append(self.parse_expr())
        return forms

    def parse_expr(self) -> SExpr:
        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input")

        kind, value, line, col = self.tokens[self.pos]

        if kind == 'LPAREN':
            return self.parse_list()
        elif kind == 'NUMBER':
            self.pos += 1
            return Number(float(value) if '.' in value else int(value), line, col)
        elif kind == 'STRING':
            self.pos += 1
            return String(_unescape_string(value[1:-1]), line, col)
        elif kind == 'QUOTE':
            self.pos += 1
            return SList([Symbol('quote', line, col), self.parse_expr()], line, col)
        elif kind == 'SYMBOL':
            self.pos += 1
            return Symbol(value, line, col)
        elif kind == 'OPERATOR':
            self.pos += 1
            return Symbol(value, line, col)
        elif kind == 'COLON':
            self.pos += 1
            if self.pos < len(self.tokens):
                _, next_val, _, _ = self.tokens[self.pos]
                self.pos += 1
                return Symbol(':' + next_val, line, col)
            raise ParseError("Expected symbol after ':'", line, col)
        elif kind == 'RANGE':
            self.pos += 1
            return Symbol('..', line, col)
        else:
            raise ParseError(f"Unexpected token: {value}", line, col)

    def parse_list(self) -> SList:
        kind, _, line, col = self.tokens[self.pos]
        if kind != 'LPAREN':
            raise ParseError("Expected '('", line, col)

        self.pos += 1
        items = []

        while self.pos < len(self.tokens):
            kind, _, _, _ = self.tokens[self.pos]
            if kind == 'RPAREN':
                self.pos += 1
                return SList(items, line, col)
            items.append(self.parse_expr())

        raise ParseError("Unclosed list", line, col)


def parse(source: str) -> List[SExpr]:
    return Parser(source).parse()

def parse_file(path: str) -> List[SExpr]:
    with open(path) as f:
        return parse(f.read())


# AST utilities

def is_form(expr: SExpr, keyword: str) -> bool:
    return (isinstance(expr, SList) and
            len(expr) > 0 and
            isinstance(expr[0], Symbol) and
            expr[0].name == keyword)

def get_forms(ast: List[SExpr], keyword: str) -> List[SList]:
    return [e for e in ast if is_form(e, keyword)]

def find_all(expr: SExpr, predicate) -> List[SExpr]:
    results = []
    def walk(e):
        if predicate(e):
            results.append(e)
        if isinstance(e, SList):
            for item in e.items:
                walk(item)
    walk(expr)
    return results

def find_holes(expr: SExpr) -> List[SList]:
    return find_all(expr, lambda e: is_form(e, 'hole'))


# Pretty printing

def pretty_print(expr: SExpr, indent: int = 0) -> str:
    if isinstance(expr, SList):
        if len(expr) == 0:
            return "()"

        simple = all(not isinstance(x, SList) for x in expr.items)
        if simple and len(str(expr)) < 60:
            return str(expr)

        prefix = "  " * indent
        lines = [f"({expr[0]}"]
        for item in expr.items[1:]:
            lines.append(pretty_print(item, indent + 1))

        result = lines[0]
        for line in lines[1:]:
            result += f"\n{prefix}  {line}"
        result += ")"
        return result

    return str(expr)


if __name__ == '__main__':
    test = '''
    (module example
      (export (foo 1))
      (type Age (Int 0 .. 150))
      (fn greet ((name String))
        (@intent "Say hello")
        (@spec ((String) -> String))
        (concat "Hello, " name)))
    '''
    for form in parse(test):
        print(pretty_print(form))
