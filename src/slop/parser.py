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
    resolved_type: Optional[Any] = None  # Set by type checker
    def __repr__(self): return self.name

@dataclass
class String:
    value: str
    line: int = 0
    col: int = 0
    resolved_type: Optional[Any] = None  # Set by type checker
    def __repr__(self):
        escaped = self.value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t')
        return f'"{escaped}"'

@dataclass
class Number:
    value: Union[int, float]
    line: int = 0
    col: int = 0
    resolved_type: Optional[Any] = None  # Set by type checker
    def __repr__(self): return str(self.value)

@dataclass
class SList:
    items: List['SExpr']
    line: int = 0
    col: int = 0
    resolved_type: Optional[Any] = None  # Set by type checker

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
        ('LBRACE', r'\{'),
        ('RBRACE', r'\}'),
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


# Operator precedence for infix expressions (higher = binds tighter)
INFIX_PRECEDENCE = {
    'or': 1,
    'and': 2,
    '==': 3, '!=': 3,
    '<': 4, '<=': 4, '>': 4, '>=': 4,
    '+': 5, '-': 5,
    '*': 6, '/': 6, '%': 6,
}


class Parser:
    def __init__(self, source: str):
        self.tokens = Lexer(source).tokenize()
        self.pos = 0
        self.in_contract = False  # Track if inside @pre/@post/@assume

    def parse(self) -> List[SExpr]:
        forms = []
        while self.pos < len(self.tokens):
            forms.append(self.parse_expr())
        return forms

    def parse_expr(self) -> SExpr:
        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input")

        kind, value, line, col = self.tokens[self.pos]

        if kind == 'LBRACE':
            return self.parse_infix_expr()
        elif kind == 'LPAREN':
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

        # Track if this is a contract form for infix support
        is_contract_form = False

        while self.pos < len(self.tokens):
            kind, value, _, _ = self.tokens[self.pos]
            if kind == 'RPAREN':
                self.pos += 1
                return SList(items, line, col)

            # Detect contract annotations after parsing first item
            if len(items) == 0 and kind == 'SYMBOL' and value in ('@pre', '@post', '@assume'):
                is_contract_form = True

            # Set in_contract context when parsing the argument of a contract
            if is_contract_form and len(items) == 1:
                old_in_contract = self.in_contract
                self.in_contract = True
                try:
                    items.append(self.parse_expr())
                finally:
                    self.in_contract = old_in_contract
            else:
                items.append(self.parse_expr())

        raise ParseError("Unclosed list", line, col)

    def parse_infix_expr(self) -> SExpr:
        """Parse {infix expression} and convert to prefix AST.

        Only allowed inside @pre, @post, or @assume contracts.
        """
        _, _, line, col = self.tokens[self.pos]

        if not self.in_contract:
            raise ParseError(
                "Infix syntax {expr} is only allowed inside @pre, @post, or @assume",
                line, col
            )

        self.pos += 1  # consume LBRACE

        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input in infix expression", line, col)

        # Check for empty braces
        if self.tokens[self.pos][0] == 'RBRACE':
            raise ParseError("Empty infix expression", line, col)

        ast = self._parse_infix_precedence(0)

        # Expect RBRACE
        if self.pos >= len(self.tokens):
            raise ParseError("Expected '}' to close infix expression", line, col)
        if self.tokens[self.pos][0] != 'RBRACE':
            _, val, ln, cl = self.tokens[self.pos]
            raise ParseError(f"Expected '}}', got '{val}'", ln, cl)
        self.pos += 1

        return ast

    def _parse_infix_precedence(self, min_prec: int) -> SExpr:
        """Precedence climbing algorithm for infix expressions."""
        left = self._parse_infix_atom()

        while True:
            op = self._peek_binary_op()
            if op is None:
                break
            op_prec = INFIX_PRECEDENCE.get(op)
            if op_prec is None or op_prec < min_prec:
                break

            # Consume operator
            _, _, op_line, op_col = self.tokens[self.pos]
            self.pos += 1

            # Left associative: use op_prec + 1 for right operand
            right = self._parse_infix_precedence(op_prec + 1)

            # Convert to prefix form: a + b -> (+ a b)
            left = SList([Symbol(op, op_line, op_col), left, right], left.line, left.col)

        return left

    def _peek_binary_op(self) -> str | None:
        """Peek at current token and return operator name if it's a binary operator."""
        if self.pos >= len(self.tokens):
            return None

        kind, value, _, _ = self.tokens[self.pos]

        # Check for RBRACE or RPAREN - end of expression
        if kind in ('RBRACE', 'RPAREN'):
            return None

        # 'and' and 'or' are symbols, not operators
        if kind == 'SYMBOL' and value in ('and', 'or'):
            return value

        # Standard operators
        if kind == 'OPERATOR' and value in INFIX_PRECEDENCE:
            return value

        return None

    def _parse_infix_atom(self) -> SExpr:
        """Parse atomic element in infix expression."""
        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input in infix expression")

        kind, value, line, col = self.tokens[self.pos]

        # Unary 'not'
        if kind == 'SYMBOL' and value == 'not':
            self.pos += 1
            operand = self._parse_infix_atom()
            return SList([Symbol('not', line, col), operand], line, col)

        # Unary minus (only at start or after operator, handled by context)
        if kind == 'OPERATOR' and value == '-':
            self.pos += 1
            operand = self._parse_infix_atom()
            # Convert to (- 0 x) for unary negation
            return SList([Symbol('-', line, col), Number(0, line, col), operand], line, col)

        # Parenthesized expression - could be grouping OR prefix S-expression
        if kind == 'LPAREN':
            return self._parse_infix_paren()

        # Number
        if kind == 'NUMBER':
            self.pos += 1
            return Number(float(value) if '.' in value else int(value), line, col)

        # String
        if kind == 'STRING':
            self.pos += 1
            return String(_unescape_string(value[1:-1]), line, col)

        # Symbol (variable, $result, etc.)
        if kind == 'SYMBOL':
            self.pos += 1
            return Symbol(value, line, col)

        # Quote
        if kind == 'QUOTE':
            self.pos += 1
            quoted = self._parse_infix_atom()
            return SList([Symbol('quote', line, col), quoted], line, col)

        raise ParseError(f"Unexpected token in infix expression: {value}", line, col)

    def _parse_infix_paren(self) -> SExpr:
        """Parse parenthesized expression in infix context.

        Distinguishes between:
        - Grouping: (a + b) -> recurse infix
        - Prefix form: (len arr) or (. ptr field) -> parse as S-expression
        """
        _, _, line, col = self.tokens[self.pos]

        # Look ahead to determine if this is a function call or grouping
        # Save position for potential backtracking
        save_pos = self.pos
        self.pos += 1  # consume LPAREN

        if self.pos >= len(self.tokens):
            raise ParseError("Unexpected end of input after '('", line, col)

        kind, value, _, _ = self.tokens[self.pos]

        # If first element is a symbol, check if it's followed by an operator
        # If not followed by an operator, it's likely a function call
        if kind == 'SYMBOL':
            # Check next token
            if self.pos + 1 < len(self.tokens):
                next_kind, next_val, _, _ = self.tokens[self.pos + 1]
                # If next is RPAREN, it could be (x) grouping or (x) single element
                # If next is not an operator, treat as prefix call
                if next_kind == 'RPAREN':
                    # Single element in parens, treat as grouping
                    self.pos += 1  # consume symbol
                    result = Symbol(value, line, col)
                    self.pos += 1  # consume RPAREN
                    return result
                elif next_kind not in ('OPERATOR',) and next_val not in ('and', 'or'):
                    # This is a function call like (len arr) - parse as prefix
                    self.pos = save_pos
                    # Temporarily exit contract mode to parse the S-expression normally
                    return self.parse_list()

        # Special case: if first element is an operator like '.', '-', '+', etc.
        # These are prefix function calls like (. obj field) or (- 0 n)
        if kind == 'OPERATOR':
            self.pos = save_pos
            return self.parse_list()

        # Otherwise treat as grouping - parse as infix
        expr = self._parse_infix_precedence(0)

        # Expect RPAREN
        if self.pos >= len(self.tokens):
            raise ParseError("Expected ')' in infix grouping", line, col)
        if self.tokens[self.pos][0] != 'RPAREN':
            _, val, ln, cl = self.tokens[self.pos]
            raise ParseError(f"Expected ')', got '{val}'", ln, cl)
        self.pos += 1

        return expr


def _normalize_quotes(expr: SExpr) -> SExpr:
    """Normalize (quote symbol) to 'symbol.

    This handles Lisp-style quote forms, converting them to SLOP's 'symbol syntax.
    Applied post-parse to ensure consistent AST regardless of input style.
    """
    if isinstance(expr, SList) and len(expr) == 2:
        if isinstance(expr[0], Symbol) and expr[0].name == 'quote':
            inner = expr[1]
            if isinstance(inner, Symbol):
                # (quote foo) -> 'foo
                return Symbol(f"'{inner.name}", expr.line, expr.col)
    # Recursively normalize children
    if isinstance(expr, SList):
        normalized = [_normalize_quotes(item) for item in expr.items]
        return SList(normalized, expr.line, expr.col)
    return expr


def _normalize_bare_forms(ast: List[SExpr]) -> List[SExpr]:
    """Normalize bare record/enum forms to wrapped (type Name ...) form.

    Converts:
        (record Name (field Type) ...) → (type Name (record (field Type) ...))
        (enum Name variant ...)        → (type Name (enum variant ...))

    This allows the transpiler to handle a single form instead of both.
    """
    result = []
    for form in ast:
        if is_form(form, 'record') and len(form) >= 2 and isinstance(form[1], Symbol):
            # (record Name fields...) → (type Name (record fields...))
            name = form[1]
            record_body = SList([Symbol('record')] + list(form.items[2:]))
            # Preserve source location if available
            if hasattr(form, 'line'):
                record_body.line = form.line
            wrapped = SList([Symbol('type'), name, record_body])
            if hasattr(form, 'line'):
                wrapped.line = form.line
            result.append(wrapped)
        elif is_form(form, 'enum') and len(form) >= 2 and isinstance(form[1], Symbol):
            # (enum Name variants...) → (type Name (enum variants...))
            name = form[1]
            enum_body = SList([Symbol('enum')] + list(form.items[2:]))
            if hasattr(form, 'line'):
                enum_body.line = form.line
            wrapped = SList([Symbol('type'), name, enum_body])
            if hasattr(form, 'line'):
                wrapped.line = form.line
            result.append(wrapped)
        elif is_form(form, 'module'):
            # Recursively normalize inside module
            # Keep module keyword and name, normalize the rest
            normalized_items = list(form.items[:2])  # 'module' and name
            normalized_items.extend(_normalize_bare_forms(list(form.items[2:])))
            normalized = SList(normalized_items)
            if hasattr(form, 'line'):
                normalized.line = form.line
            result.append(normalized)
        else:
            result.append(form)
    return result


def parse(source: str) -> List[SExpr]:
    forms = Parser(source).parse()
    forms = [_normalize_quotes(form) for form in forms]
    forms = _normalize_bare_forms(forms)
    return forms

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

def get_imports(ast: List[SExpr]) -> List[SList]:
    """Get all import declarations from AST.

    Import form: (import module-name name*)
    """
    return get_forms(ast, 'import')

def get_exports(ast: List[SExpr]) -> List[SList]:
    """Get all export declarations from AST.

    Export form: (export name*)
    """
    return get_forms(ast, 'export')

@dataclass
class ImportSpec:
    """Parsed import specification."""
    module_name: str
    symbols: List[str]
    line: int = 0
    col: int = 0

def parse_import(form: SList) -> ImportSpec:
    """Parse an import form into ImportSpec.

    (import module-name sym1 sym2 ...)
    (import module-name (sym1 sym2 ...))  -- grouped in list
    """
    if len(form) < 2:
        raise ParseError("import requires module name", form.line, form.col)

    module_name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
    symbols = []

    for item in form.items[2:]:
        if isinstance(item, Symbol):
            symbols.append(item.name)
        elif isinstance(item, SList):
            # Grouped list of symbols: (sym1 sym2 ...)
            for sub_item in item.items:
                if isinstance(sub_item, Symbol):
                    symbols.append(sub_item.name)

    return ImportSpec(module_name, symbols, form.line, form.col)

@dataclass
class ExportSpec:
    """Parsed export specification."""
    symbols: List[str]
    line: int = 0
    col: int = 0

def parse_export(form: SList) -> ExportSpec:
    """Parse an export form into ExportSpec.

    (export sym1 sym2 ...)
    """
    symbols = []

    for item in form.items[1:]:
        if isinstance(item, Symbol):
            symbols.append(item.name)

    return ExportSpec(symbols, form.line, form.col)

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
