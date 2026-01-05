"""
SLOP Code Formatter - Idiomatic formatting for SLOP source.

Formats SLOP code with proper indentation and line wrapping based on
the semantic structure of the language.
"""

from typing import List, Optional
from slop.parser import SExpr, SList, Symbol, String, Number, parse, is_form

# Max line length before wrapping
MAX_LINE = 80

# Indent size
INDENT = 2


def format_source(source: str) -> str:
    """Format SLOP source code.

    Args:
        source: Raw SLOP source code

    Returns:
        Formatted source code
    """
    ast = parse(source)
    return '\n\n'.join(format_expr(form, 0) for form in ast) + '\n'


def format_file(path: str) -> str:
    """Format a SLOP file.

    Args:
        path: Path to SLOP file

    Returns:
        Formatted source code
    """
    with open(path) as f:
        return format_source(f.read())


def inline(expr: SExpr) -> str:
    """Render expression on a single line."""
    if isinstance(expr, SList):
        if len(expr) == 0:
            return "()"
        parts = [inline(item) for item in expr.items]
        return "(" + " ".join(parts) + ")"
    return str(expr)


def fits_inline(expr: SExpr, max_len: int = MAX_LINE) -> bool:
    """Check if expression fits on one line."""
    return len(inline(expr)) <= max_len


def format_expr(expr: SExpr, indent: int) -> str:
    """Format a single expression."""
    if isinstance(expr, SList):
        return format_list(expr, indent)
    return str(expr)


def format_list(expr: SList, indent: int) -> str:
    """Format an SList based on its head form."""
    if len(expr) == 0:
        return "()"

    head = expr[0].name if isinstance(expr[0], Symbol) else str(expr[0])

    # Dispatch to specialized formatters
    formatters = {
        'module': format_module,
        'fn': format_fn,
        'impl': format_fn,
        'type': format_type,
        'const': format_const,
        'let': format_let,
        'let*': format_let,
        'if': format_if,
        'when': format_when,
        'cond': format_cond,
        'match': format_match,
        'do': format_do,
        'ffi': format_ffi,
        'ffi-struct': format_ffi_struct,
        'import': format_import,
        'export': format_export,
        'hole': format_hole,
        'with-arena': format_with_arena,
        'while': format_while,
        'for': format_for,
        'for-each': format_for_each,
    }

    # Annotations stay inline if short
    if head.startswith('@'):
        return format_annotation(expr, indent)

    formatter = formatters.get(head, format_generic)
    return formatter(expr, indent)


def pad(indent: int) -> str:
    """Create indentation padding."""
    return " " * (indent * INDENT)


# =============================================================================
# Specialized Formatters
# =============================================================================

def format_module(expr: SList, indent: int) -> str:
    """Format module: (module name (export ...) (import ...) body...)"""
    if len(expr) < 2:
        return inline(expr)

    lines = [f"(module {expr[1]}"]
    p = pad(indent + 1)

    # Separate exports/imports from body
    exports_imports = []
    body = []

    for item in expr.items[2:]:
        if is_form(item, 'export') or is_form(item, 'import'):
            exports_imports.append(item)
        else:
            body.append(item)

    # Format exports/imports
    for item in exports_imports:
        lines.append(p + format_expr(item, indent + 1))

    # Add blank line before body if we have both sections
    if exports_imports and body:
        lines.append("")

    # Format body with blank lines between top-level forms
    for i, item in enumerate(body):
        if i > 0:
            lines.append("")  # Blank line between top-level forms
        lines.append(p + format_expr(item, indent + 1))

    lines[-1] += ")"
    return '\n'.join(lines)


def format_fn(expr: SList, indent: int) -> str:
    """Format function: (fn name ((param Type) ...) @annotations... body)"""
    if len(expr) < 3:
        return inline(expr)

    name = expr[1]
    params = expr[2]
    rest = expr.items[3:]

    # Build first line: (fn name params
    params_str = inline(params)
    first_line = f"(fn {name} {params_str}"

    p = pad(indent + 1)
    lines = [first_line]

    # Separate annotations from body
    annotations = []
    body = []
    for item in rest:
        if isinstance(item, SList) and len(item) > 0:
            head = item[0].name if isinstance(item[0], Symbol) else ""
            if head.startswith('@'):
                annotations.append(item)
                continue
        body.append(item)

    # Format annotations
    for ann in annotations:
        lines.append(p + format_annotation(ann, indent + 1))

    # Format body
    for item in body:
        lines.append(p + format_expr(item, indent + 1))

    lines[-1] += ")"
    return '\n'.join(lines)


def format_annotation(expr: SList, indent: int) -> str:
    """Format annotation: (@intent "..."), (@spec ...), etc."""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    # Multi-line annotation
    head = expr[0]
    p = pad(indent + 1)
    lines = [f"({head}"]
    for item in expr.items[1:]:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_type(expr: SList, indent: int) -> str:
    """Format type definition: (type Name definition)"""
    if len(expr) < 3:
        return inline(expr)

    name = expr[1]
    defn = expr[2]

    # Short types stay inline
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    # Check what kind of type definition
    if is_form(defn, 'record'):
        return format_type_record(name, defn, indent)
    elif is_form(defn, 'enum'):
        # Enums usually fit inline
        return inline(expr)
    else:
        # Range types, etc.
        return inline(expr)


def format_type_record(name, defn: SList, indent: int) -> str:
    """Format record type with fields on separate lines."""
    p = pad(indent + 1)
    pf = pad(indent + 2)

    lines = [f"(type {name} (record"]
    for field in defn.items[1:]:
        lines.append(pf + inline(field))
    lines[-1] += "))"
    return '\n'.join(lines)


def format_const(expr: SList, indent: int) -> str:
    """Format const: (const NAME Type value)"""
    return inline(expr)


def format_let(expr: SList, indent: int) -> str:
    """Format let: (let ((x val) (y val)) body...)"""
    if len(expr) < 3:
        return inline(expr)

    bindings = expr[1]
    body = expr.items[2:]

    # Calculate alignment for bindings
    p = pad(indent + 1)
    pb = " " * (indent * INDENT + 6)  # Align after "(let (("

    # First binding
    lines = []
    if isinstance(bindings, SList) and len(bindings) > 0:
        first_binding = inline(bindings[0])
        lines.append(f"(let (({first_binding[1:-1]})") # Strip outer parens from inline

        # Remaining bindings aligned
        for binding in bindings.items[1:]:
            lines.append(pb + inline(binding))

        if len(bindings) > 0:
            lines[-1] += ")"
    else:
        lines.append(f"(let {inline(bindings)}")

    # Body
    for item in body:
        lines.append(p + format_expr(item, indent + 1))

    lines[-1] += ")"
    return '\n'.join(lines)


def format_if(expr: SList, indent: int) -> str:
    """Format if: (if cond then else)"""
    if len(expr) < 3:
        return inline(expr)

    # Short if can be inline
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    cond = expr[1]
    then_expr = expr[2] if len(expr) > 2 else None
    else_expr = expr[3] if len(expr) > 3 else None

    # Condition inline if short
    cond_str = inline(cond) if fits_inline(cond, 40) else format_expr(cond, indent + 1)

    lines = [f"(if {cond_str}"]
    if then_expr:
        lines.append(p + format_expr(then_expr, indent + 1))
    if else_expr:
        lines.append(p + format_expr(else_expr, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_when(expr: SList, indent: int) -> str:
    """Format when: (when cond body...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    cond = inline(expr[1]) if len(expr) > 1 else ""
    body = expr.items[2:]

    lines = [f"(when {cond}"]
    for item in body:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_cond(expr: SList, indent: int) -> str:
    """Format cond: (cond (test1 result1) (test2 result2) ...)"""
    p = pad(indent + 1)
    p2 = pad(indent + 2)
    lines = ["(cond"]
    for clause in expr.items[1:]:
        if isinstance(clause, SList) and len(clause) >= 2:
            # Format clause with test and body on separate lines
            test = clause[0]
            body = clause.items[1:]
            test_str = format_expr(test, indent + 2) if isinstance(test, SList) else inline(test)
            # Short clause: ((test) body) on one line if simple
            if len(body) == 1 and fits_inline(clause, 60):
                lines.append(p + f"({test_str} {format_expr(body[0], indent + 2)})")
            else:
                # Multi-line clause
                lines.append(p + f"({test_str}")
                for item in body:
                    lines.append(p2 + format_expr(item, indent + 2))
                lines[-1] += ")"
        else:
            lines.append(p + format_expr(clause, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_match(expr: SList, indent: int) -> str:
    """Format match: (match value (pattern1 result1) ...)"""
    if len(expr) < 2:
        return inline(expr)

    p = pad(indent + 1)
    value = inline(expr[1])
    clauses = expr.items[2:]

    lines = [f"(match {value}"]
    for clause in clauses:
        lines.append(p + format_expr(clause, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_do(expr: SList, indent: int) -> str:
    """Format do: (do expr1 expr2 ...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    lines = ["(do"]
    for item in expr.items[1:]:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_while(expr: SList, indent: int) -> str:
    """Format while: (while cond body...)"""
    if len(expr) < 2:
        return inline(expr)

    p = pad(indent + 1)
    cond = inline(expr[1])
    body = expr.items[2:]

    lines = [f"(while {cond}"]
    for item in body:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_for(expr: SList, indent: int) -> str:
    """Format for: (for (init cond step) body...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    control = inline(expr[1]) if len(expr) > 1 else "()"
    body = expr.items[2:]

    lines = [f"(for {control}"]
    for item in body:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_for_each(expr: SList, indent: int) -> str:
    """Format for-each: (for-each (var collection) body...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    binding = inline(expr[1]) if len(expr) > 1 else "()"
    body = expr.items[2:]

    lines = [f"(for-each {binding}"]
    for item in body:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_with_arena(expr: SList, indent: int) -> str:
    """Format with-arena: (with-arena size body...)"""
    if len(expr) < 2:
        return inline(expr)

    p = pad(indent + 1)
    size = inline(expr[1])
    body = expr.items[2:]

    lines = [f"(with-arena {size}"]
    for item in body:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_ffi(expr: SList, indent: int) -> str:
    """Format ffi: (ffi "header.h" (func ...) ...)"""
    if len(expr) < 2:
        return inline(expr)

    p = pad(indent + 1)
    header = expr[1]
    funcs = expr.items[2:]

    lines = [f'(ffi {header}']
    for func in funcs:
        lines.append(p + format_ffi_func(func, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_ffi_func(expr: SList, indent: int) -> str:
    """Format FFI function: (name ((param Type) ...) ReturnType)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT - 4):
        return inline(expr)

    if len(expr) < 3:
        return inline(expr)

    name = expr[0]
    params = expr[1]
    ret_type = expr[2] if len(expr) > 2 else None

    p = pad(indent + 1)

    # Try to fit params inline
    params_inline = inline(params)
    if len(params_inline) < 50:
        if ret_type:
            return f"({name} {params_inline} {inline(ret_type)})"
        return f"({name} {params_inline})"

    # Multi-line params
    lines = [f"({name}"]
    lines.append(p + format_expr(params, indent + 1))
    if ret_type:
        lines.append(p + format_expr(ret_type, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_ffi_struct(expr: SList, indent: int) -> str:
    """Format ffi-struct: (ffi-struct "header" name (field Type) ...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    lines = [f"({expr[0]}"]
    for item in expr.items[1:]:
        lines.append(p + format_expr(item, indent + 1))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_import(expr: SList, indent: int) -> str:
    """Format import: (import module (fn arity) Type ...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    lines = [f"(import {expr[1]}"]
    for item in expr.items[2:]:
        lines.append(p + inline(item))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_export(expr: SList, indent: int) -> str:
    """Format export: (export (fn1 arity) (fn2 arity) ...)"""
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    p = pad(indent + 1)
    lines = ["(export"]
    for item in expr.items[1:]:
        lines.append(p + inline(item))
    lines[-1] += ")"
    return '\n'.join(lines)


def format_hole(expr: SList, indent: int) -> str:
    """Format hole: (hole Type "prompt" :complexity tier :context (...) :required (...))"""
    if len(expr) < 3:
        return inline(expr)

    p = pad(indent + 1)
    type_expr = expr[1]
    prompt = expr[2]
    rest = expr.items[3:]

    lines = [f"(hole {inline(type_expr)}"]
    lines.append(p + str(prompt))

    # Format keyword args
    i = 0
    while i < len(rest):
        item = rest[i]
        if isinstance(item, Symbol) and item.name.startswith(':'):
            # Keyword with value
            if i + 1 < len(rest):
                lines.append(p + f"{item} {inline(rest[i+1])}")
                i += 2
                continue
        lines.append(p + inline(item))
        i += 1

    lines[-1] += ")"
    return '\n'.join(lines)


def format_generic(expr: SList, indent: int) -> str:
    """Generic formatter for other forms - smart wrapping."""
    # Short expressions stay inline
    if fits_inline(expr, MAX_LINE - indent * INDENT):
        return inline(expr)

    # Check if all args are simple (no nested lists)
    all_simple = all(not isinstance(x, SList) for x in expr.items[1:])
    if all_simple and len(expr) < 6:
        return inline(expr)

    # Multi-line format
    p = pad(indent + 1)
    head = inline(expr[0])
    lines = [f"({head}"]

    for item in expr.items[1:]:
        formatted = format_expr(item, indent + 1)
        lines.append(p + formatted)

    lines[-1] += ")"
    return '\n'.join(lines)
