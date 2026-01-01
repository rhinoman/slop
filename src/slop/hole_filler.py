"""
SLOP Hole Filler - LLM-based hole completion with tiered model routing

Routes holes to appropriately-sized models based on complexity,
with automatic escalation on verification failure.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Semaphore
from typing import Optional, List, Dict, Any, Callable
from slop.parser import SExpr, SList, Symbol, String, Number, parse, find_holes, is_form, pretty_print, ParseError
from slop.providers import Tier, ModelConfig, Provider, MockProvider, create_default_configs
from slop.types import BUILTIN_FUNCTIONS

logger = logging.getLogger(__name__)


# Cache for spec/LANGUAGE.md + spec/REFERENCE.md content
_SKILL_SPEC: Optional[str] = None

# Valid built-in expression forms from SLOP spec
# Note: BUILTIN_FUNCTIONS (from types.py) is unioned with these special forms
VALID_EXPRESSION_FORMS = {
    # Control flow
    'if', 'cond', 'match', 'when', 'while', 'for', 'for-each',
    'break', 'continue', 'return', 'else',
    # Binding
    'let', 'let*',
    # Data construction
    'array', 'list', 'map', 'record-new', 'union-new',
    'ok', 'error', 'some', 'none',
    # Data access
    '.', '@', 'put', 'set!', 'deref',
    # Arithmetic
    '+', '-', '*', '/', '%', '&', '|', '^', '<<', '>>',
    # Comparison
    '==', '!=', '<', '<=', '>', '>=', '=',
    # Boolean
    'and', 'or', 'not',
    # Type/memory
    'cast', 'sizeof', 'addr', 'with-arena',
    # Error handling
    'try', '?',
    # Sequencing
    'do',
    # FFI
    'c-inline',
    # Type constructors (appear in cast, sizeof, type expressions)
    # These are not functions but can appear as heads of nested lists
    'Ptr', 'OwnPtr', 'OptPtr', 'Option', 'Result', 'List', 'Array', 'Map',
    'Int', 'U8', 'U16', 'U32', 'U64', 'I8', 'I16', 'I32', 'I64',
    'String', 'Bool', 'Float', 'Void', 'Unit', 'Arena',
    'ffi-struct',  # Nested FFI struct type
} | BUILTIN_FUNCTIONS


def _transform_lisp_forms(expr: SExpr) -> SExpr:
    """Transform common Lisp forms to SLOP equivalents.

    LLMs trained on Lisp often generate forms that don't exist in SLOP.
    This transforms them to valid SLOP.
    """
    if isinstance(expr, Symbol):
        # Transform symbols: t -> true, nil -> false (when used as boolean)
        name = expr.name
        if name == 't':
            return Symbol('true')
        if name == '#t':
            return Symbol('true')
        if name == '#f':
            return Symbol('false')
        return expr

    if not isinstance(expr, SList) or len(expr) == 0:
        return expr

    head = expr[0]
    if isinstance(head, Symbol):
        form = head.name

        # (quote symbol) -> 'symbol
        # This is critical - LLMs often use (quote foo) but SLOP uses 'foo
        if form == 'quote' and len(expr) == 2:
            inner = expr[1]
            if isinstance(inner, Symbol):
                logger.debug(f"Transforming (quote {inner.name}) -> '{inner.name}")
                return Symbol(f"'{inner.name}")
            # For non-symbols, log warning but still try to transform
            logger.warning(f"quote with non-symbol: {inner}")

        # (setq var val) -> (set! var val)
        # (setf var val) -> (set! var val)
        if form in ('setq', 'setf') and len(expr) == 3:
            new_items = [Symbol('set!')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

        # (eq a b) / (eql a b) / (equal a b) -> (== a b)
        if form in ('eq', 'eql', 'equal', 'equalp') and len(expr) == 3:
            new_items = [Symbol('==')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

        # (mod a b) -> (% a b)
        if form == 'mod' and len(expr) == 3:
            new_items = [Symbol('%')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

        # (unless cond body) -> (when (not cond) body)
        if form == 'unless' and len(expr) >= 3:
            cond = _transform_lisp_forms(expr[1])
            body = [_transform_lisp_forms(item) for item in expr.items[2:]]
            negated = SList([Symbol('not'), cond])
            if len(body) == 1:
                return SList([Symbol('when'), negated, body[0]])
            else:
                return SList([Symbol('when'), negated, SList([Symbol('do')] + body)])

        # (case expr ...) -> (match expr ...)  (structure is similar enough)
        if form == 'case':
            new_items = [Symbol('match')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

        # (nth n list) -> (@ list n)
        if form == 'nth' and len(expr) == 3:
            n = _transform_lisp_forms(expr[1])
            lst = _transform_lisp_forms(expr[2])
            return SList([Symbol('@'), lst, n])

        # (elt seq n) / (aref arr n) -> (@ seq n)
        if form in ('elt', 'aref') and len(expr) == 3:
            seq = _transform_lisp_forms(expr[1])
            n = _transform_lisp_forms(expr[2])
            return SList([Symbol('@'), seq, n])

        # (display x) -> (print x)
        if form == 'display' and len(expr) == 2:
            return SList([Symbol('print'), _transform_lisp_forms(expr[1])])

        # (newline) -> (println "")
        if form == 'newline' and len(expr) == 1:
            return SList([Symbol('println'), String("")])

        # (1+ x) -> (+ x 1)
        if form == '1+' and len(expr) == 2:
            return SList([Symbol('+'), _transform_lisp_forms(expr[1]), Number(1)])

        # (1- x) -> (- x 1)
        if form == '1-' and len(expr) == 2:
            return SList([Symbol('-'), _transform_lisp_forms(expr[1]), Number(1)])

        # (lambda args body) -> (fn args body)
        if form == 'lambda':
            new_items = [Symbol('fn')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

        # (progn ...) / (begin ...) -> (do ...)
        if form in ('progn', 'begin'):
            new_items = [Symbol('do')] + [_transform_lisp_forms(item) for item in expr.items[1:]]
            return SList(new_items)

    # Recursively transform children
    new_items = []
    for item in expr.items:
        new_items.append(_transform_lisp_forms(item))

    return SList(new_items)


def _extract_function_calls(expr: SExpr) -> set:
    """Extract all function/form names called in an expression.

    Skips known binding forms where identifiers are not function calls:
    - (let ((x val)) body) - x is a binding, not a call
    - (for (i start end) body) - i is a binding
    - (for-each (x list) body) - x is a binding
    - (match expr (Pattern body)) - Pattern is a tag, not a call
    - (record-new Type (field val)) - field is a field name
    """
    calls = set()
    if not isinstance(expr, SList) or len(expr) == 0:
        return calls

    head = expr[0]
    if not isinstance(head, Symbol):
        # Head is not a symbol, recurse on all items
        for item in expr.items:
            calls.update(_extract_function_calls(item))
        return calls

    head_name = head.name

    # Always add the head as a call (we'll filter builtins later)
    calls.add(head_name)

    # Handle special forms that introduce bindings (don't recurse into binding positions)
    if head_name in ('let', 'let*') and len(expr) >= 3:
        # (let ((x val) (y val2)) body) - skip binding names, recurse on values and body
        bindings = expr[1]
        if isinstance(bindings, SList):
            for binding in bindings.items:
                if isinstance(binding, SList) and len(binding) >= 2:
                    # Skip first element (binding name), recurse on value
                    calls.update(_extract_function_calls(binding[1]))
        # Recurse on body
        for item in expr.items[2:]:
            calls.update(_extract_function_calls(item))

    elif head_name == 'for' and len(expr) >= 3:
        # (for (i start end) body) - skip i, recurse on start, end, body
        loop_spec = expr[1]
        if isinstance(loop_spec, SList) and len(loop_spec) >= 3:
            calls.update(_extract_function_calls(loop_spec[1]))  # start
            calls.update(_extract_function_calls(loop_spec[2]))  # end
        for item in expr.items[2:]:
            calls.update(_extract_function_calls(item))

    elif head_name == 'for-each' and len(expr) >= 3:
        # (for-each (x list) body) - skip x, recurse on list, body
        loop_spec = expr[1]
        if isinstance(loop_spec, SList) and len(loop_spec) >= 2:
            calls.update(_extract_function_calls(loop_spec[1]))  # list expr
        for item in expr.items[2:]:
            calls.update(_extract_function_calls(item))

    elif head_name == 'match' and len(expr) >= 2:
        # (match expr (Pattern body) ...) - skip pattern heads, recurse on bodies
        calls.update(_extract_function_calls(expr[1]))  # matched expr
        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                # Skip pattern (first element), recurse on body (rest)
                for item in clause.items[1:]:
                    calls.update(_extract_function_calls(item))

    elif head_name == 'record-new' and len(expr) >= 2:
        # (record-new Type (field val) ...) - skip Type and field names
        for item in expr.items[2:]:
            if isinstance(item, SList) and len(item) >= 2:
                # Skip field name, recurse on value
                calls.update(_extract_function_calls(item[1]))

    elif head_name == 'cond':
        # (cond (test body) ... (else body)) - recurse on all tests and bodies
        for clause in expr.items[1:]:
            if isinstance(clause, SList):
                for item in clause.items:
                    # Skip 'else' keyword
                    if isinstance(item, Symbol) and item.name == 'else':
                        continue
                    calls.update(_extract_function_calls(item))

    else:
        # Default: recurse on all items after head
        for item in expr.items[1:]:
            calls.update(_extract_function_calls(item))

    return calls


def load_skill_spec() -> str:
    """Load and cache the SLOP language spec from spec/LANGUAGE.md and spec/REFERENCE.md"""
    global _SKILL_SPEC
    if _SKILL_SPEC is not None:
        return _SKILL_SPEC

    # Find spec files relative to this file's location
    # hole_filler.py is in src/slop/, spec/ is at project root
    pkg_root = Path(__file__).parent.parent.parent

    content = ""

    # Load spec/LANGUAGE.md - complete language specification
    lang_path = pkg_root / "spec" / "LANGUAGE.md"
    if lang_path.exists():
        content = lang_path.read_text()

    # Load spec/REFERENCE.md - quick reference for LLM code generation
    # Contains common mistakes, loop patterns, and idioms
    ref_path = pkg_root / "spec" / "REFERENCE.md"
    if ref_path.exists():
        content += "\n\n" + ref_path.read_text()

    _SKILL_SPEC = content.strip()
    return _SKILL_SPEC


@dataclass
class Hole:
    type_expr: SExpr
    prompt: str
    complexity: Optional[str] = None
    constraints: Optional[List[SExpr]] = None
    examples: Optional[List[SExpr]] = None
    context: Optional[List[str]] = None     # Whitelist of available identifiers
    required: Optional[List[str]] = None    # Identifiers that MUST appear in output


@dataclass
class FillResult:
    success: bool
    expression: Optional[SExpr] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    attempts: int = 0
    quality_score: float = 0.0
    quality_metrics: Optional[Dict[str, float]] = None


@dataclass
class CheckResult:
    """Result of hole implementation type check."""
    valid: bool
    errors: List[str]
    inferred_type: Optional[str] = None
    expected_type: Optional[str] = None


def extract_hole(hole_expr: SList) -> Hole:
    """Extract hole information from AST"""
    items = hole_expr.items[1:]

    type_expr = items[0] if items else None
    prompt = items[1].value if len(items) > 1 and isinstance(items[1], String) else ""

    complexity = None
    constraints = None
    examples = None
    context = None
    required = None

    i = 2
    while i < len(items):
        if isinstance(items[i], Symbol) and items[i].name.startswith(':'):
            key = items[i].name[1:]
            if i + 1 < len(items):
                val = items[i + 1]
                if key == 'complexity':
                    complexity = val.name if isinstance(val, Symbol) else str(val)
                elif key == 'constraints':
                    constraints = val.items if isinstance(val, SList) else [val]
                elif key == 'examples':
                    examples = val.items if isinstance(val, SList) else [val]
                elif key == 'context':
                    if isinstance(val, SList):
                        context = [str(x) for x in val.items]
                    else:
                        context = [str(val)]
                elif key == 'required':
                    if isinstance(val, SList):
                        required = [str(x) for x in val.items]
                    else:
                        required = [str(val)]
                i += 2
            else:
                i += 1
        else:
            i += 1

    return Hole(type_expr, prompt, complexity, constraints, examples, context, required)


def classify_tier(hole: Hole) -> Tier:
    """Classify hole complexity"""

    # Explicit override
    if hole.complexity:
        tier_map = {
            'tier-1': Tier.TIER_1,
            'tier-2': Tier.TIER_2,
            'tier-3': Tier.TIER_3,
            'tier-4': Tier.TIER_4,
        }
        if hole.complexity in tier_map:
            return tier_map[hole.complexity]

    # Has examples → simpler
    base_tier = Tier.TIER_1 if hole.examples else Tier.TIER_2

    # Type complexity
    type_tier = _type_complexity(hole.type_expr)

    # Constraints complexity
    constraint_tier = Tier.TIER_1
    if hole.constraints and len(hole.constraints) > 2:
        constraint_tier = Tier.TIER_2

    return max([base_tier, type_tier, constraint_tier], key=lambda t: t.value)


def _type_complexity(type_expr: SExpr) -> Tier:
    """Determine complexity from type"""
    if isinstance(type_expr, Symbol):
        if type_expr.name in ['Bool', 'Int', 'String', 'Float', 'U8', 'I32']:
            return Tier.TIER_1
        return Tier.TIER_2

    if isinstance(type_expr, SList) and len(type_expr) > 0:
        head = type_expr[0]
        if isinstance(head, Symbol):
            if head.name == 'Fn':
                return Tier.TIER_3
            if head.name in ('Result', 'Option'):
                return Tier.TIER_2
            if head.name == 'List':
                return Tier.TIER_2

    return Tier.TIER_2


def _extract_error_type_name(type_expr: SExpr) -> Optional[str]:
    """Extract the error type name from a Result type expression.

    (Result Pet ApiError) -> 'ApiError'
    (Result (List Pet) ApiError) -> 'ApiError'
    """
    if not isinstance(type_expr, SList) or len(type_expr) < 3:
        return None
    head = type_expr[0]
    if not isinstance(head, Symbol) or head.name != 'Result':
        return None
    # Error type is the third element
    err_type = type_expr[2]
    if isinstance(err_type, Symbol):
        return err_type.name
    return None


def _get_syntax_hints_for_type(type_str: str) -> str:
    """Generate syntax hints based on expected return type"""
    hints = []
    if 'Result' in type_str:
        hints.append("For Result types: return (ok value) for success, (error 'variant) for failure")
        hints.append("IMPORTANT: Error variants must be QUOTED: (error 'not-found) NOT (error not-found)")
        hints.append("Match with: (match result ((ok val) use-val) ((error e) handle-e))")
    if 'Option' in type_str:
        hints.append("For Option types: return (some value) or (none)")
        hints.append("Match with: (match opt ((some val) use-val) ((none) handle-none))")
    if 'Bool' in type_str:
        hints.append("Return true or false (bare symbols, not quoted)")
    if 'String' in type_str:
        hints.append("String operations need arena: (string-concat arena s1 s2), (int-to-string arena n)")
    return '\n'.join(hints) if hints else ""


# Common function mistakes and their correct alternatives
FUNCTION_ALTERNATIVES = {
    # SLOP-specific
    'parse-int': 'No parse-int in SLOP. Use (string-to-int str) via FFI or manual loop',
    'json-parse': 'No JSON library. Parse manually or use record-new with known fields',
    'string-find': 'No string-find. Use for-each to iterate characters',
    'substring': 'Use (string-slice s start end) if available via FFI',
    'list-length': 'Arrays are fixed size (e.g., 100). Track length manually for lists',
    'print-int': 'Use (println (int-to-string arena n))',
    'arr.length': 'Use the declared array size directly (e.g., 100)',
    'malloc': 'Use (arena-alloc arena size)',
    'free': 'Arenas auto-free. Use (arena-free arena) only for explicit cleanup',
    # Common Lisp forms not in SLOP (ones we can't auto-transform)
    'car': 'No car in SLOP. Use (@ list 0) for first element',
    'cdr': 'No cdr in SLOP. Use slice or iterate from index 1',
    'first': 'No first in SLOP. Use (@ list 0)',
    'rest': 'No rest in SLOP. Use slice or iterate from index 1',
    'cons': 'No cons in SLOP. Use (list ...) to create lists',
    'append': 'No append in SLOP. Build new list with for-each',
    'reverse': 'No reverse in SLOP. Build reversed list with for loop',
    'length': 'No length in SLOP. Use string-len for strings, track list length manually',
    'null?': 'No null? in SLOP. Use (== x nil) or (none? x) for Option',
    'nil?': 'No nil? in SLOP. Use (== x nil)',
    'empty?': 'No empty? in SLOP. Check length or use == nil',
    'atom?': 'No atom? in SLOP. Type checking is static',
    'pair?': 'No pair? in SLOP. Type checking is static',
    'list?': 'No list? in SLOP. Type checking is static',
    'apply': 'No apply in SLOP. Call function directly with arguments',
    'funcall': 'No funcall in SLOP. Call function directly',
    'mapcar': 'No mapcar in SLOP. Use for-each loop',
    'filter': 'No filter in SLOP. Use for-each with when',
    'reduce': 'No reduce in SLOP. Use for-each with accumulator',
    'format': 'No format in SLOP. Use string-concat and int-to-string',
    'princ': 'No princ in SLOP. Use print',
    'prin1': 'No prin1 in SLOP. Use print',
    'terpri': 'No terpri in SLOP. Use (println "")',
    'read': 'No read in SLOP. Use FFI for stdio',
    'read-line': 'No read-line in SLOP. Use FFI for stdio',
}


def _extract_enum_variants(context: Dict[str, Any]) -> set:
    """Extract all enum variant names from type definitions.

    This allows enum variant constructors like (literal "pets") to be
    recognized as valid forms, not undefined functions.
    """
    variants = set()
    all_type_defs = context.get('type_defs', []) + [t['type_def'] for t in context.get('imported_types', [])]

    for type_def_str in all_type_defs:
        try:
            type_ast = parse(type_def_str)
            if type_ast and is_form(type_ast[0], 'type') and len(type_ast[0]) > 2:
                type_expr = type_ast[0][2]
                if is_form(type_expr, 'enum'):
                    for v in type_expr.items[1:]:
                        if isinstance(v, Symbol):
                            variants.add(v.name)
                        elif isinstance(v, SList) and len(v) > 0 and isinstance(v[0], Symbol):
                            # Variant with payload like (literal String)
                            variants.add(v[0].name)
        except Exception:
            pass

    return variants


def _extract_param_names(params_str: str) -> List[str]:
    """Extract variable names from params string like '((arena Arena) (request (Ptr Request)))'"""
    if not params_str:
        return []

    try:
        parsed = parse(params_str)
        if not parsed or not isinstance(parsed[0], SList):
            return []
        names = []
        for param in parsed[0].items:
            if isinstance(param, SList) and len(param) >= 1 and isinstance(param[0], Symbol):
                names.append(param[0].name)
        return names
    except Exception:
        return []


def _extract_referenced_types(hole: 'Hole', context: Dict[str, Any]) -> set:
    """Find all type names referenced by hole's context functions and return type.

    This enables context-aware prompt filtering - only include enum values and
    record fields for types that are actually used by the hole.
    """
    import re

    # Collect all known type names from type definitions
    all_type_defs = context.get('type_defs', []) + [t['type_def'] for t in context.get('imported_types', [])]
    type_names = set()
    for type_def_str in all_type_defs:
        # Extract name from "(type Name ...)"
        match = re.match(r'\(type\s+(\w+)', type_def_str)
        if match:
            type_names.add(match.group(1))

    referenced = set()

    # 1. Include types from the hole's return type
    if hole.type_expr:
        hole_type_str = str(hole.type_expr)
        for type_name in type_names:
            if type_name in hole_type_str:
                referenced.add(type_name)

    # Use context list (whitelist of available items) for type extraction
    context_items = hole.context or []
    context_set = set(context_items)

    # 2. Include types from context function signatures
    all_specs = (context.get('fn_specs', []) +
                 context.get('ffi_specs', []) +
                 context.get('imported_specs', []))

    for fn_name in context_items:
        spec = next((s for s in all_specs if s['name'] == fn_name), None)
        if not spec:
            continue
        signature = spec.get('params', '') + ' ' + spec.get('return_type', '')
        for type_name in type_names:
            if type_name in signature:
                referenced.add(type_name)

    # 3. Include types from function parameters (context often lists param names)
    params_str = context.get('params', '')
    if params_str and context_set:
        try:
            params_ast = parse(params_str)
            if params_ast and isinstance(params_ast[0], SList):
                for param in params_ast[0].items:
                    if isinstance(param, SList) and len(param) >= 2:
                        param_name = param[0].name if isinstance(param[0], Symbol) else str(param[0])
                        if param_name in context_set:
                            # Extract type names from this param's type
                            param_type_str = str(param[1])
                            for type_name in type_names:
                                if type_name in param_type_str:
                                    referenced.add(type_name)
        except Exception:
            pass

    return referenced


def build_prompt(hole: Hole, context: Dict[str, Any], failed_attempts: List[tuple] = None) -> str:
    """Build prompt for hole filling, optionally including feedback from failures"""
    spec = load_skill_spec()

    sections = [
        "You are filling a typed hole in SLOP code.",
    ]

    if spec:
        sections.extend([
            "",
            spec,
        ])

    # Strong constraints to prevent invented functions
    sections.extend([
        "",
        "## CRITICAL CONSTRAINTS",
        "1. ONLY use functions listed in the Built-in Functions above",
        "2. DO NOT invent functions - these DO NOT exist in SLOP:",
        "   - json-parse, json-get-*, parse-json (no JSON library)",
        "   - string-find, string-index, substring (use string-slice or string-split)",
        "   - parse-int, atoi, str-to-int (use FFI if needed)",
        "   - read, write (use recv, send for sockets)",
        "   - ref (no references - use deref for pointer dereferencing)",
        "   - list-invoices, find-invoice, create-invoice (define these yourself)",
        "3. Use (do expr1 expr2 ...) for sequencing multiple expressions",
        "4. ENUM MATCHING: Use QUOTED symbols for enum values in match patterns:",
        "   - (match x ('Foo ...) ('Bar ...)) - quoted symbols for value match",
        "   - Bare identifiers are BINDINGS, not value matches",
        "5. Escape quotes in strings: \"hello \\\"world\\\"\"",
        "6. Arrays are FIXED SIZE - no .length property. Use the declared size (e.g., 100 for Array T 100)",
        "7. ENUM VARIANTS: The quote is PART OF THE SYMBOL NAME, not a separate form!",
        "   - 'not-found is a SINGLE TOKEN (the symbol literally starts with a quote character)",
        "   - CORRECT: (error 'not-found)  - the 'not-found is one atomic symbol",
        "   - CORRECT: (match x ('Fizz ...) ('Buzz ...))  - quoted for value match",
        "   - WRONG: (error not-found) - bare symbol is undefined variable",
        "8. For Result types with specific error enums (like ApiError), use the enum variant:",
        "   - If return type is (Result T ApiError), use (error 'bad-request) or (error 'not-found)",
        "   - The error variant MUST come from the declared error enum in the @spec",
        "   - Remember: 'bad-request is ONE token, not (quote bad-request)",
        "9. POINTER ALLOCATION: To allocate and cast to a pointer type:",
        "   - CORRECT: (cast (Ptr User) (arena-alloc arena (sizeof User)))",
        "   - WRONG: (Ptr User (arena-alloc ...))  -- Ptr is NOT a function!",
        "   - The pattern is: (cast (Ptr Type) allocation-expression)",
        "   - Always use 'cast' to convert (Ptr Void) from arena-alloc to specific pointer type",
    ])

    # Add type-specific syntax hints
    type_str = str(hole.type_expr)
    type_hints = _get_syntax_hints_for_type(type_str)
    if type_hints:
        sections.extend([
            "",
            "## Type-Specific Syntax",
            type_hints,
        ])

    # If return type is Result with a named error type, extract available variants
    error_type_name = _extract_error_type_name(hole.type_expr)
    if error_type_name and context.get('error_variants'):
        variants = context.get('error_variants', {}).get(error_type_name, [])
        if variants:
            sections.extend([
                "",
                f"## Available Error Variants for {error_type_name}",
                f"When returning an error, use ONE of these quoted variants:",
                f"  {', '.join(repr(v) for v in variants)}",
                f"Example: (error '{variants[0]})",
            ])

    # Examples of correct fills
    sections.extend([
        "",
        "## Examples of Correct Fills",
        "Hole: (hole Int \"Add two numbers\" :context (a b))",
        "Fill: (+ a b)",
        "",
        "Hole: (hole FizzBuzzResult \"Return result based on divisibility\")",
        "Fill: (cond ((== (% n 15) 0) 'FizzBuzz) ((== (% n 3) 0) 'Fizz) ((== (% n 5) 0) 'Buzz) (else 'Number))",
        "",
        "Hole: (hole (Result Pet ApiError) \"Get pet or return not-found error\")",
        "Fill: (match (lookup id) ((some pet) (ok pet)) ((none) (error 'not-found)))",
        "",
        "Hole: (hole (Result Unit ApiError) \"Delete or return error\")",
        "Fill: (if success (ok ()) (error 'not-found))",
    ])

    sections.extend([
        "",
        "## Context",
        f"Function: {context.get('fn_name', 'unknown')}",
        f"Intent: {context.get('intent', '')}",
        f"Parameters: {context.get('params', '')}",
        f"Return type: {context.get('return_type', '')}",
    ])

    # Explicitly list available variables to prevent using wrong names
    if context.get('params'):
        param_names = _extract_param_names(context.get('params', ''))
        if param_names:
            sections.append("")
            sections.append("## Available Variables")
            sections.append(f"Existing variables: {', '.join(param_names)}")
            sections.append(f"Full parameter types: {context.get('params', '')}")
            sections.append("")
            sections.append("To CREATE new variables, use:")
            sections.append("  (let ((name value)) body)  - binds 'name' to result of 'value'")
            sections.append("  (match expr ((ok x) ...) ((error e) ...))  - binds x or e from Result")
            sections.append("  (with-arena SIZE body)  - binds 'arena' inside body")
            sections.append("")
            sections.append("Do NOT use undefined variables. Either use existing ones or create new ones with let/match.")

    if context.get('type_defs'):
        sections.append("")
        sections.append("## Type Definitions")
        for td in context.get('type_defs', []):
            sections.append(td)

    if context.get('fn_specs'):
        sections.append("")
        sections.append("## Functions Defined in This File")
        sections.append("You may call these functions:")
        for spec in context['fn_specs']:
            ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
            sections.append(f"  ({spec['name']} {spec['params']}{ret})")

    if context.get('ffi_specs'):
        sections.append("")
        sections.append("## FFI Functions (from C headers)")
        sections.append("These C functions are available via FFI. Use them with EXACT argument types:")
        for spec in context['ffi_specs']:
            ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
            sections.append(f"  ({spec['name']} {spec['params']}{ret})")

    if context.get('requires_fns'):
        sections.append("")
        sections.append("## Required Functions (from @requires)")
        sections.append("These function signatures are declared as dependencies. Use them as specified:")
        for sig in context['requires_fns']:
            sections.append(f"  {sig}")

    # Show imported functions (from other modules) WITH SIGNATURES
    if context.get('imported_specs'):
        sections.append("")
        sections.append("## Imported Functions (from other modules)")
        sections.append("These functions are imported and available. Use EXACT parameter types:")
        for spec in context['imported_specs']:
            ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
            sections.append(f"  ({spec['name']} {spec['params']}{ret})")
    elif context.get('defined_functions'):
        # Fallback to just names if specs not available
        fn_names = {s['name'] for s in context.get('fn_specs', [])}
        ffi_names = {s['name'] for s in context.get('ffi_specs', [])}
        imported = [f for f in context['defined_functions']
                    if f not in fn_names and f not in ffi_names and f not in VALID_EXPRESSION_FORMS and f not in BUILTIN_FUNCTIONS]
        if imported:
            sections.append("")
            sections.append("## Imported Functions (from other modules)")
            sections.append("These functions are imported and available to use:")
            sections.append(f"  {', '.join(sorted(imported))}")

    # Extract enum values and record fields from type_defs and imported_types
    all_type_defs = context.get('type_defs', []) + [t['type_def'] for t in context.get('imported_types', [])]

    enum_info = []
    record_info = []

    for type_def_str in all_type_defs:
        try:
            type_ast = parse(type_def_str)
            if type_ast and is_form(type_ast[0], 'type') and len(type_ast[0]) > 2:
                name = type_ast[0][1].name if isinstance(type_ast[0][1], Symbol) else str(type_ast[0][1])
                type_expr = type_ast[0][2]

                if is_form(type_expr, 'enum'):
                    # Extract enum variants with payload info
                    variants = []
                    for v in type_expr.items[1:]:
                        if isinstance(v, Symbol):
                            variants.append(f"'{v.name}")
                        elif isinstance(v, SList) and len(v) > 0 and isinstance(v[0], Symbol):
                            # Variant with payload - show as (variant-name PayloadType)
                            variant_name = v[0].name
                            if len(v) > 1:
                                payload_type = str(v[1])
                                variants.append(f"({variant_name} {payload_type})")
                            else:
                                variants.append(f"'{variant_name}")
                    if variants:
                        enum_info.append(f"{name}: {', '.join(variants)}")

                elif is_form(type_expr, 'record'):
                    # Extract field names
                    fields = []
                    for field in type_expr.items[1:]:
                        if isinstance(field, SList) and len(field) >= 1:
                            field_name = field[0].name if isinstance(field[0], Symbol) else str(field[0])
                            fields.append(field_name)
                    if fields:
                        record_info.append(f"{name}: {', '.join(fields)}")
        except Exception:
            pass

    # Filter enum_info and record_info to only include types referenced by this hole
    if hole.context:
        referenced_types = _extract_referenced_types(hole, context)
        filtered_enum_info = [e for e in enum_info if e.split(':')[0] in referenced_types]
        filtered_record_info = [r for r in record_info if r.split(':')[0] in referenced_types]
        # Safety: if filtering removed everything but there were items, fall back to all
        if not filtered_enum_info and enum_info:
            filtered_enum_info = enum_info
        if not filtered_record_info and record_info:
            filtered_record_info = record_info
    else:
        # No context whitelist - include all types
        filtered_enum_info = enum_info
        filtered_record_info = record_info

    if filtered_enum_info:
        sections.append("")
        sections.append("## Enum Values")
        sections.append("Simple variants: use quoted like 'ok, 'created (NOT integers!)")
        sections.append("Variants with payload: (variant-name value) e.g., (literal \"pets\"), (param \"id\")")
        for info in filtered_enum_info:
            sections.append(f"  {info}")

    if filtered_record_info:
        sections.append("")
        sections.append("## Record Fields (use (. record field-name) to access)")
        for info in filtered_record_info:
            sections.append(f"  {info}")

    # Range type guidance - SLOP-specific, LLMs may not understand
    sections.append("")
    sections.append("## Range Types (IMPORTANT)")
    sections.append("Range types like (Int 1 ..) are DIFFERENT from plain Int.")
    sections.append("When a function expects (Int 1 ..) but you have Int, you MUST cast:")
    sections.append("  (cast (Int 1 ..) value)")
    sections.append("Example: If path-param-int returns (Option Int) and you extract id,")
    sections.append("  but delete-by-id expects (Int 1 ..), use: (delete-by-id state (cast (Int 1 ..) id))")

    sections.extend([
        "",
        "## Hole to Fill",
        f"Type: {hole.type_expr}",
        f"Description: {hole.prompt}",
    ])

    if hole.constraints:
        sections.append("")
        sections.append("## Constraints")
        for c in hole.constraints:
            sections.append(f"- {c}")

    if hole.examples:
        sections.append("")
        sections.append("## Examples")
        for ex in hole.examples:
            sections.append(f"- {ex}")

    if hole.context:
        sections.append("")
        sections.append("## Available Context")
        sections.append(f"You may ONLY call these functions/identifiers: {', '.join(hole.context)}")
        sections.append("(Built-in forms like let, if, match, do are always available)")

    if hole.required:
        sections.append("")
        sections.append("## Required")
        sections.append(f"The expression MUST use: {', '.join(hole.required)}")

    # Include feedback from failed attempts
    if failed_attempts:
        import re
        sections.append("")
        sections.append("## Previous Failed Attempts")
        sections.append("Your previous attempts were rejected. Fix ALL of these issues:")
        # Include last 3 failures to avoid prompt bloat
        for i, (attempt, error) in enumerate(failed_attempts[-3:], 1):
            sections.append("")
            sections.append(f"Attempt {i}: {attempt[:80]}...")
            # Split multiple errors (joined with "; ") for readability
            error_parts = error.split("; ")
            for err in error_parts:
                if "Undefined variable:" in err:
                    var_match = re.search(r"Undefined variable: (\S+)", err)
                    var_name = var_match.group(1) if var_match else "it"
                    sections.append(f"  - CRITICAL: {err}")
                    sections.append(f"    To fix: use (let (({var_name} ...)) body) or bind in match")
                else:
                    sections.append(f"  - {err}")

    sections.append("")
    sections.append("Respond with ONLY the SLOP S-expression. No explanation.")

    return '\n'.join(sections)


def _validate_expr_type(
    expr: SExpr,
    expected_type_expr: SExpr,
    context: dict,
) -> tuple:
    """Core type validation logic.

    Args:
        expr: Parsed expression to validate
        expected_type_expr: Parsed expected type expression
        context: Context dictionary with type_defs, fn_specs, params, etc.

    Returns:
        Tuple of (error_messages: List[str], inferred_type_str: Optional[str], expected_type_str: Optional[str])
    """
    import re
    from slop.type_checker import TypeChecker
    from slop.types import (
        Type, PrimitiveType, PtrType, OptionType, ResultType,
        TypeVar, UNKNOWN, FnType, RecordType, EnumType, UnionType
    )

    def types_compatible(inferred: Type, expected: Type) -> bool:
        """Check if inferred type is compatible with expected type."""
        if inferred == UNKNOWN or expected == UNKNOWN:
            return True
        if isinstance(inferred, TypeVar) or isinstance(expected, TypeVar):
            return True
        if isinstance(inferred, ResultType) and isinstance(expected, ResultType):
            if isinstance(inferred.ok_type, TypeVar) or isinstance(inferred.err_type, TypeVar):
                return True
            if isinstance(expected.ok_type, RecordType) and expected.ok_type.name == '<anonymous>':
                return types_compatible(inferred.err_type, expected.err_type)
        if hasattr(inferred, 'is_subtype_of'):
            return inferred.is_subtype_of(expected)
        if hasattr(inferred, 'equals'):
            return inferred.equals(expected)
        return str(inferred) == str(expected)

    def is_allowable_unknown(typ: Type) -> bool:
        """Check if type is an allowable unknown (c-inline, type var)."""
        if typ == UNKNOWN:
            return True
        if isinstance(typ, TypeVar):
            return True
        return False

    try:
        # Create type checker (built-ins registered automatically)
        checker = TypeChecker()

        # Register imported types FIRST
        imported_types = context.get('imported_types', [])
        for type_info in imported_types:
            try:
                type_ast = parse(type_info['type_def'])
                if type_ast and is_form(type_ast[0], 'type') and len(type_ast[0]) > 2:
                    name = type_info['name']
                    typ = checker.parse_type_expr(type_ast[0][2])
                    if isinstance(typ, RecordType) and typ.name == '<anonymous>':
                        typ = RecordType(name, typ.fields)
                    elif isinstance(typ, EnumType) and typ.name == '<anonymous>':
                        typ = EnumType(name, typ.variants)
                    elif isinstance(typ, UnionType) and typ.name == '<anonymous>':
                        typ = UnionType(name, typ.variants)
                    checker.env.register_type(name, typ)
            except Exception:
                pass

        # Register local type definitions
        type_defs = context.get('type_defs', [])
        for type_def_str in type_defs:
            try:
                type_ast = parse(type_def_str)
                if type_ast and is_form(type_ast[0], 'type') and len(type_ast[0]) > 2:
                    name = type_ast[0][1].name if isinstance(type_ast[0][1], Symbol) else str(type_ast[0][1])
                    typ = checker.parse_type_expr(type_ast[0][2])
                    if isinstance(typ, RecordType) and typ.name == '<anonymous>':
                        typ = RecordType(name, typ.fields)
                    elif isinstance(typ, EnumType) and typ.name == '<anonymous>':
                        typ = EnumType(name, typ.variants)
                    elif isinstance(typ, UnionType) and typ.name == '<anonymous>':
                        typ = UnionType(name, typ.variants)
                    checker.env.register_type(name, typ)
            except Exception:
                pass

        # Parse and bind parameters
        params_str = context.get('params', '')
        if params_str:
            try:
                params_ast = parse(params_str)
                if params_ast and isinstance(params_ast[0], SList):
                    for param in params_ast[0].items:
                        if isinstance(param, SList) and len(param) >= 2:
                            param_name = param[0].name if isinstance(param[0], Symbol) else str(param[0])
                            param_type = checker.parse_type_expr(param[1])
                            checker.env.bind_var(param_name, param_type)
            except Exception:
                pass

        # Register functions from fn_specs
        for spec in context.get('fn_specs', []):
            try:
                fn_name = spec.get('name', '')
                if fn_name and spec.get('return_type'):
                    ret_ast = parse(spec['return_type'])
                    if ret_ast:
                        ret_type = checker.parse_type_expr(ret_ast[0])
                        param_types = []
                        params_str = spec.get('params', '')
                        if params_str:
                            params_ast = parse(params_str)
                            if params_ast and isinstance(params_ast[0], SList):
                                for param in params_ast[0].items:
                                    if isinstance(param, SList) and len(param) >= 2:
                                        param_type = checker.parse_type_expr(param[1])
                                        param_types.append(param_type)
                        checker.env.register_function(fn_name, FnType(tuple(param_types), ret_type))
            except Exception:
                pass

        # Register FFI functions
        for spec in context.get('ffi_specs', []):
            try:
                fn_name = spec.get('name', '')
                if fn_name and spec.get('return_type'):
                    ret_ast = parse(spec['return_type'])
                    if ret_ast:
                        ret_type = checker.parse_type_expr(ret_ast[0])
                        param_types = []
                        params_str = spec.get('params', '')
                        if params_str:
                            params_ast = parse(params_str)
                            if params_ast and isinstance(params_ast[0], SList):
                                for param in params_ast[0].items:
                                    if isinstance(param, SList) and len(param) >= 2:
                                        param_type = checker.parse_type_expr(param[1])
                                        param_types.append(param_type)
                        checker.env.register_function(fn_name, FnType(tuple(param_types), ret_type))
            except Exception:
                pass

        # Register imported functions
        for spec in context.get('imported_specs', []):
            try:
                fn_name = spec.get('name', '')
                if fn_name and spec.get('return_type'):
                    ret_ast = parse(spec['return_type'])
                    if ret_ast:
                        ret_type = checker.parse_type_expr(ret_ast[0])
                        param_types = []
                        params_str = spec.get('params', '')
                        if params_str:
                            params_ast = parse(params_str)
                            if params_ast and isinstance(params_ast[0], SList):
                                for param in params_ast[0].items:
                                    if isinstance(param, SList) and len(param) >= 2:
                                        param_type = checker.parse_type_expr(param[1])
                                        param_types.append(param_type)
                        checker.env.register_function(fn_name, FnType(tuple(param_types), ret_type))
            except Exception:
                pass

        # Register constants
        for spec in context.get('const_specs', []):
            try:
                const_name = spec.get('name', '')
                type_expr_str = spec.get('type_expr', '')
                if const_name and type_expr_str:
                    type_ast = parse(type_expr_str)
                    if type_ast:
                        const_type = checker.parse_type_expr(type_ast[0])
                        checker.env.bind_var(const_name, const_type)
            except Exception:
                pass

        # Parse expected type and infer expression type
        expected = checker.parse_type_expr(expected_type_expr)
        inferred = checker.infer_expr(expr)

        # Collect type errors with enhancements
        error_msgs = []
        errors = [d for d in checker.diagnostics if d.severity == 'error']
        param_names = _extract_param_names(context.get('params', ''))

        for err in errors:
            msg = err.message
            if "Undefined variable:" in msg and param_names:
                msg += f" -- Available: {', '.join(param_names)}"
            if re.search(r"expected \(Int \d+ \.\..*\), got Int", msg):
                msg += " -- Use (cast <range-type> value)"
            error_msgs.append(msg)

        # Check type compatibility
        if not error_msgs and not types_compatible(inferred, expected):
            if not is_allowable_unknown(inferred):
                error_msgs.append(f"Type mismatch: expected {expected}, got {inferred}")

        return (error_msgs, str(inferred), str(expected))

    except Exception as e:
        logger.warning(f"Type check exception (allowing): {e}")
        return ([], None, None)


def check_hole_impl(
    expr_str: str,
    expected_type: str,
    context_file: Optional[str] = None,
    fn_name: Optional[str] = None,
    params: Optional[str] = None,
) -> CheckResult:
    """Type check an expression against an expected type with context.

    Args:
        expr_str: SLOP expression string to validate
        expected_type: Expected type as SLOP type expression string
        context_file: Path to .slop file for context (types, functions, etc.)
        fn_name: If provided, extract params from this function in context_file
        params: Parameter string like "((x Int) (y String))" - overrides fn_name extraction

    Returns:
        CheckResult with valid flag, errors list, and type information
    """
    from pathlib import Path

    # Parse the expression
    try:
        expr_ast = parse(expr_str)
        if not expr_ast:
            return CheckResult(valid=False, errors=["Empty expression"])
        expr = expr_ast[0]
    except ParseError as e:
        return CheckResult(valid=False, errors=[f"Parse error in expression: {e}"])

    # Parse the expected type
    try:
        type_ast = parse(expected_type)
        if not type_ast:
            return CheckResult(valid=False, errors=["Empty type expression"])
        expected_type_expr = type_ast[0]
    except ParseError as e:
        return CheckResult(valid=False, errors=[f"Parse error in type: {e}"])

    # Build context from file if provided
    context: Dict[str, Any] = {
        'type_defs': [],
        'fn_specs': [],
        'ffi_specs': [],
        'const_specs': [],
        'imported_specs': [],
        'imported_types': [],
        'params': params or '',
    }

    if context_file:
        try:
            from slop.cli import extract_file_context
            file_context = extract_file_context(context_file, fn_name if not params else None)
            context.update(file_context)
            # If params provided explicitly, use those instead
            if params:
                context['params'] = params
        except Exception as e:
            return CheckResult(valid=False, errors=[f"Error reading context file: {e}"])

    # Validate expression type
    errors, inferred_str, expected_str = _validate_expr_type(expr, expected_type_expr, context)

    return CheckResult(
        valid=len(errors) == 0,
        errors=errors,
        inferred_type=inferred_str,
        expected_type=expected_str,
    )


class HoleFiller:
    """Fill holes with tiered model routing"""

    # Default concurrency limits per tier
    DEFAULT_TIER_LIMITS = {
        Tier.TIER_1: 8,   # Local models - can handle more
        Tier.TIER_2: 4,   # Haiku - moderate
        Tier.TIER_3: 3,   # Sonnet - careful
        Tier.TIER_4: 2,   # Opus - very careful
    }

    def __init__(self, configs: Dict[Tier, ModelConfig], provider,
                 tier_limits: Optional[Dict[Tier, int]] = None):
        self.configs = configs
        self.provider = provider
        self.max_retries = 2

        # Create per-tier semaphores for rate limiting
        limits = tier_limits or self.DEFAULT_TIER_LIMITS
        self._tier_semaphores: Dict[Tier, Semaphore] = {
            tier: Semaphore(limits.get(tier, 2))
            for tier in Tier
        }

    def fill(self, hole: Hole, context: Dict[str, Any],
             use_semaphores: bool = False) -> FillResult:
        """Fill a single hole.

        Args:
            hole: The hole to fill
            context: Context dict with type_defs, fn_specs, ffi_specs, etc.
            use_semaphores: If True, use per-tier semaphores for rate limiting
        """
        return self._fill_internal(hole, context, use_semaphores)

    def _fill_internal(self, hole: Hole, context: Dict[str, Any],
                       use_semaphores: bool) -> FillResult:
        """Internal fill implementation with optional semaphore support."""
        tier = classify_tier(hole)
        failed_attempts = []  # List of (response_snippet, error) tuples

        logger.debug(f"Filling hole: {hole.prompt}")
        logger.debug(f"Classified as {tier.name}")

        attempts = 0
        current_tier = tier
        held_semaphore: Optional[Tier] = None

        try:
            while current_tier.value <= Tier.TIER_4.value:
                config = self.configs.get(current_tier)
                if not config:
                    logger.debug(f"No config for {current_tier.name}, skipping")
                    next_val = current_tier.value + 1
                    if next_val > Tier.TIER_4.value:
                        break
                    current_tier = Tier(next_val)
                    continue

                # Acquire semaphore for current tier (release old one if escalating)
                if use_semaphores:
                    if held_semaphore and held_semaphore != current_tier:
                        self._tier_semaphores[held_semaphore].release()
                        held_semaphore = None
                    if held_semaphore is None:
                        self._tier_semaphores[current_tier].acquire()
                        held_semaphore = current_tier

                for retry in range(self.max_retries):
                    attempts += 1
                    # Rebuild prompt with feedback from failed attempts
                    prompt = build_prompt(hole, context, failed_attempts)
                    logger.info(f"Attempt {attempts}: {current_tier.name} using {config.model}")
                    logger.debug(f"Prompt:\n{prompt}")

                    try:
                        response = self.provider.complete(prompt, config)
                        logger.debug(f"Response:\n{response}")

                        expr, parse_error = self._parse_response(response)
                        if expr is None:
                            error = parse_error or "Failed to parse response"
                            logger.warning(error)
                            failed_attempts.append((response[:100], error))
                            continue

                        success, error = self._validate(expr, hole, context)
                        if success:
                            logger.info(f"Success after {attempts} attempt(s)")
                            quality_metrics = self._calculate_quality(expr, hole, context, attempts)
                            return FillResult(
                                success=True,
                                expression=expr,
                                model_used=config.model,
                                attempts=attempts,
                                quality_score=quality_metrics['overall'],
                                quality_metrics=quality_metrics
                            )
                        else:
                            logger.warning(f"Validation failed: {error}")
                            failed_attempts.append((str(expr)[:100], error))
                    except Exception as e:
                        logger.warning(f"Exception during attempt {attempts}: {e}")
                        failed_attempts.append(("(exception)", str(e)))
                        continue

                # Escalate to next tier
                next_val = current_tier.value + 1
                if next_val > Tier.TIER_4.value:
                    break
                logger.info(f"Escalating from {current_tier.name} to {Tier(next_val).name}")
                current_tier = Tier(next_val)

            logger.error(f"Failed after {attempts} attempt(s)")
            return FillResult(
                success=False,
                error=f"Failed after {attempts} attempts",
                attempts=attempts
            )
        finally:
            # Always release semaphore on exit
            if use_semaphores and held_semaphore:
                self._tier_semaphores[held_semaphore].release()

    def fill_parallel(
        self,
        holes: List[tuple],  # List of (Hole, context) tuples
        max_workers: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int, FillResult], None]] = None
    ) -> List[FillResult]:
        """Fill multiple holes in parallel with per-tier rate limiting.

        Args:
            holes: List of (Hole, context) tuples to fill
            max_workers: Max concurrent threads (default: sum of tier limits)
            progress_callback: Optional callback(completed, total, result) for progress updates

        Returns:
            List of FillResults in same order as input holes
        """
        if not holes:
            return []

        # Default max_workers to total tier capacity
        if max_workers is None:
            max_workers = sum(self.DEFAULT_TIER_LIMITS.values())

        results: List[Optional[FillResult]] = [None] * len(holes)
        completed = 0

        def fill_one(idx: int, hole: Hole, context: Dict[str, Any]) -> tuple:
            result = self._fill_internal(hole, context, use_semaphores=True)
            return idx, result

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(fill_one, i, hole, ctx): i
                for i, (hole, ctx) in enumerate(holes)
            }

            for future in as_completed(futures):
                idx, result = future.result()
                results[idx] = result
                completed += 1

                if progress_callback:
                    progress_callback(completed, len(holes), result)

        return results

    def _parse_response(self, response: str) -> tuple[Optional[SExpr], Optional[str]]:
        """Parse LLM response. Returns (expr, None) on success, (None, error) on failure."""
        response = response.strip()
        if response.startswith('```'):
            lines = response.split('\n')
            response = '\n'.join(lines[1:-1])
            logger.debug(f"Stripped code fence, parsing: {response[:100]}...")

        try:
            exprs = parse(response)
            if exprs:
                expr = exprs[0]
                # Transform common Lisp forms to SLOP equivalents
                # e.g., (quote x) -> 'x, (setq x v) -> (set! x v), etc.
                expr = _transform_lisp_forms(expr)
                logger.debug(f"Parsed expression: {expr}")
                return (expr, None)
            logger.debug("Parse returned empty list")
            return (None, "Parse returned empty result")
        except ParseError as e:
            # Include specific error like "Unclosed list at line 3"
            logger.debug(f"Parse error: {e}")
            logger.debug(f"Failed to parse: {response[:200]}")
            return (None, f"Parse error: {e}")
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            logger.debug(f"Failed to parse: {response[:200]}")
            return (None, f"Parse error: {e}")

    def _find_invalid_form(self, expr: SExpr) -> Optional[str]:
        """Recursively find any invalid form names in the expression.
        Returns the invalid form name if found, None otherwise."""
        if not isinstance(expr, SList) or len(expr) == 0:
            return None

        head = expr[0]
        if isinstance(head, Symbol):
            form_name = head.name
            # Known bad forms that LLMs invent
            INVALID_FORMS = {'block', 'begin', 'progn', 'seq', 'sequence', 'do-block'}
            if form_name in INVALID_FORMS:
                return form_name
            # Reject (quote ...) - should have been transformed to 'symbol
            if form_name == 'quote':
                return 'quote (use quoted symbol like \'ok instead of (quote ok))'

        # Recursively check all children
        for item in expr.items:
            if isinstance(item, SList):
                invalid = self._find_invalid_form(item)
                if invalid:
                    return invalid

        return None

    def _check_syntax(self, expr: SExpr) -> Optional[str]:
        """Validate expression syntax that parser doesn't catch.
        Returns error message if invalid, None otherwise."""
        if isinstance(expr, SList) and len(expr) >= 1:
            # Check field access: (. obj field) requires exactly 2 args and field must be Symbol
            if is_form(expr, '.'):
                if len(expr) != 3:
                    return f"Field access requires exactly 2 arguments (obj field), got {len(expr) - 1}"
                if not isinstance(expr[2], Symbol):
                    return f"Field access requires symbol, got {type(expr[2]).__name__}: (. obj {expr[2]})"
            # Recurse into children
            for item in expr.items:
                if isinstance(item, SList):
                    err = self._check_syntax(item)
                    if err:
                        return err
        return None

    def _validate(self, expr: SExpr, hole: Hole, context: dict = None) -> tuple[bool, Optional[str]]:
        """Validate expression, return (success, error_messages).

        Collects ALL errors rather than stopping at the first one,
        so the LLM gets complete feedback and can fix multiple issues at once.
        """
        context = context or {}
        errors = []

        # Check 1: Reject if the "fill" is itself a hole form
        if is_form(expr, 'hole'):
            errors.append("Do not return a hole form - fill it with actual code")

        # Check 2: Reject definition forms - holes must be filled with expressions
        invalid_forms = ['fn', 'type', 'module', 'impl', 'ffi', 'ffi-struct',
                         '@intent', '@spec', '@pre', '@post', '@example', '@pure', '@alloc']
        for form_name in invalid_forms:
            if is_form(expr, form_name):
                errors.append(f"Do not return a '{form_name}' form - fill the hole with an expression, not a definition")
                break  # Only report first matching definition form

        # Check 3: Recursive check for invented forms (quote, block, etc.)
        invalid_form = self._find_invalid_form(expr)
        if invalid_form:
            errors.append(f"Invalid form '{invalid_form}' - not a valid SLOP expression. Use 'do' for sequencing.")

        # Check 3.5: Structural syntax validation (field access, etc.)
        syntax_error = self._check_syntax(expr)
        if syntax_error:
            errors.append(syntax_error)

        # Check 4: Undefined function calls
        defined_fns = set(context.get('defined_functions', []))
        enum_variants = _extract_enum_variants(context)  # Allow enum variant constructors
        allowed = VALID_EXPRESSION_FORMS | BUILTIN_FUNCTIONS | defined_fns | enum_variants
        calls = _extract_function_calls(expr)
        undefined = calls - allowed
        if undefined:
            errors.append(f"Undefined function(s): {', '.join(sorted(undefined))}. Only use built-ins or functions defined in this file.")

        # Check 5a: context whitelist (limits what functions/identifiers can be used)
        if hole.context:
            context_set = set(hole.context)
            # Include FFI functions - they're globally available
            ffi_names = {s['name'] for s in context.get('ffi_specs', [])}
            # Validate context items are actually defined
            valid_items = VALID_EXPRESSION_FORMS | BUILTIN_FUNCTIONS | defined_fns | enum_variants | ffi_names
            invalid_context = context_set - valid_items
            if invalid_context:
                errors.append(f"Invalid context items (not defined): {', '.join(sorted(invalid_context))}")
            # Allow built-in forms, built-in functions, enum variants, FFI functions, and context items
            context_allowed = VALID_EXPRESSION_FORMS | BUILTIN_FUNCTIONS | enum_variants | ffi_names | context_set
            disallowed = calls - context_allowed
            if disallowed:
                errors.append(f"Only allowed to use: {', '.join(hole.context)}")
                errors.append(f"Disallowed calls found: {', '.join(sorted(disallowed))}")

        # Check 5b: required items (must appear in output)
        if hole.required:
            expr_str = str(expr)
            missing_uses = []
            for req in hole.required:
                req_normalized = req.replace('-', '_').replace('.', '')
                expr_normalized = expr_str.replace('-', '_').replace('.', '')
                if req_normalized not in expr_normalized:
                    missing_uses.append(req)
            if missing_uses:
                errors.append(f"Expression must use: {', '.join(missing_uses)}")

        # Check 6: Type checking - get ALL type errors
        type_errors = self._check_type_all(expr, hole, context)
        errors.extend(type_errors)

        # Return all errors joined, or success
        if errors:
            # Deduplicate and cap at 5 errors to avoid prompt bloat
            unique_errors = list(dict.fromkeys(errors))[:5]
            for err in unique_errors:
                logger.warning(err)
            return False, "; ".join(unique_errors)

        return True, None

    def _check_type_all(self, expr: SExpr, hole: Hole, context: dict) -> List[str]:
        """Type check filled expression against hole's expected type.

        Returns list of ALL error messages (empty if valid).

        Policy (strict with exceptions):
        - FAIL on clear type mismatches (wrong type, missing cast)
        - ALLOW unknown types (?) from c-inline expressions
        - ALLOW unresolved type variables
        """
        if not hole.type_expr:
            return []  # No type constraint to check

        errors, _, _ = _validate_expr_type(expr, hole.type_expr, context)
        return errors

    def _types_compatible(self, inferred: 'Type', expected: 'Type') -> bool:
        """Check if inferred type is compatible with expected type.

        Uses subtype checking where available, falls back to equality.
        """
        from slop.types import TypeVar, UNKNOWN, PtrType, PrimitiveType, ResultType, RecordType

        # Unknown always compatible (can't verify)
        if inferred == UNKNOWN or expected == UNKNOWN:
            return True

        # Type variables are compatible (unresolved)
        if isinstance(inferred, TypeVar) or isinstance(expected, TypeVar):
            return True

        # Handle Result types with type variables or anonymous types
        if isinstance(inferred, ResultType) and isinstance(expected, ResultType):
            # If inferred has type variables, allow it (can't fully verify)
            if isinstance(inferred.ok_type, TypeVar) or isinstance(inferred.err_type, TypeVar):
                return True
            # If expected has anonymous record as ok_type, be lenient
            if (isinstance(expected.ok_type, RecordType) and
                expected.ok_type.name == '<anonymous>'):
                # Just verify error types are compatible
                return self._types_compatible(inferred.err_type, expected.err_type)

        # Check subtype relationship
        if hasattr(inferred, 'is_subtype_of'):
            return inferred.is_subtype_of(expected)

        # Fall back to equality
        if hasattr(inferred, 'equals'):
            return inferred.equals(expected)

        return str(inferred) == str(expected)

    def _is_allowable_unknown(self, typ: 'Type') -> bool:
        """Check if type is an allowable unknown (c-inline, type var)."""
        from slop.types import TypeVar, UNKNOWN

        if typ == UNKNOWN:
            return True
        if isinstance(typ, TypeVar):
            return True
        return False

    def _calculate_quality(self, expr: SExpr, hole: Hole, context: dict, attempts: int) -> Dict[str, float]:
        """Calculate quality metrics for a successful fill"""
        metrics = {
            'parse_success': 1.0,  # Always 1.0 if we get here
            'validation_pass': 1.0,  # Always 1.0 if we get here
        }

        # Check required items coverage
        if hole.required:
            expr_str = str(expr)
            found = 0
            for req in hole.required:
                req_normalized = req.replace('-', '_').replace('.', '')
                expr_normalized = expr_str.replace('-', '_').replace('.', '')
                if req_normalized in expr_normalized:
                    found += 1
            metrics['required_coverage'] = found / len(hole.required)
        else:
            metrics['required_coverage'] = 1.0

        # Check for undefined calls (should be 0 if validation passed)
        defined_fns = set(context.get('defined_functions', []))
        allowed = VALID_EXPRESSION_FORMS | BUILTIN_FUNCTIONS | defined_fns
        calls = _extract_function_calls(expr)
        undefined = calls - allowed
        metrics['no_undefined_calls'] = 1.0 if not undefined else 0.0

        # Penalize multiple attempts (first attempt = 1.0, decreases by 0.15 per attempt)
        metrics['first_attempt_bonus'] = max(0.0, 1.0 - (attempts - 1) * 0.15)

        # Calculate overall score (weighted average)
        weights = {
            'parse_success': 0.2,
            'validation_pass': 0.3,
            'required_coverage': 0.2,
            'no_undefined_calls': 0.2,
            'first_attempt_bonus': 0.1,
        }
        score = sum(metrics[k] * weights[k] for k in weights)
        metrics['overall'] = round(score, 2)

        return metrics

    def fill_batch(
        self,
        holes: List[tuple],  # List of (Hole, context_dict) tuples
        tier: Tier
    ) -> List[FillResult]:
        """Fill multiple holes in a single interactive session.

        This is more efficient for interactive providers since it
        batches all holes into one prompt, minimizing context switches.
        """
        if not holes:
            return []

        config = self.configs.get(tier)
        if not config:
            return [FillResult(success=False, error="No config for tier") for _ in holes]

        # Build combined prompt
        combined = self._build_batch_prompt(holes)

        try:
            response = self.provider.complete(combined, config)
            return self._parse_batch_response(response, holes)
        except Exception as e:
            return [FillResult(success=False, error=str(e)) for _ in holes]

    def _build_batch_prompt(self, holes: List[tuple]) -> str:
        """Build a combined prompt for multiple holes"""
        spec = load_skill_spec()

        sections = [
            "You are filling multiple typed holes in SLOP code.",
        ]

        if spec:
            sections.extend([
                "",
                spec,
            ])

        sections.extend([
            "",
            "For each hole, provide ONLY the expression with no explanation.",
            "Use the [HOLE N] format for each response.",
            "",
        ])

        # Include type definitions once (they're shared across holes)
        if holes:
            first_context = holes[0][1]
            if first_context.get('type_defs'):
                sections.append("## Type Definitions")
                for td in first_context.get('type_defs', []):
                    sections.append(td)
                sections.append("")

            if first_context.get('fn_specs'):
                sections.append("## Functions Defined in This File")
                for spec in first_context['fn_specs']:
                    ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
                    sections.append(f"  ({spec['name']} {spec['params']}{ret})")
                sections.append("")

            if first_context.get('ffi_specs'):
                sections.append("## FFI Functions (from C headers)")
                sections.append("Use these with EXACT argument types:")
                for spec in first_context['ffi_specs']:
                    ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
                    sections.append(f"  ({spec['name']} {spec['params']}{ret})")
                sections.append("")

            # Show imported functions (from other modules) WITH SIGNATURES
            if first_context.get('imported_specs'):
                sections.append("## Imported Functions (from other modules)")
                sections.append("These functions are imported and available. Use EXACT parameter types:")
                for spec in first_context['imported_specs']:
                    ret = f" -> {spec['return_type']}" if spec.get('return_type') else ""
                    sections.append(f"  ({spec['name']} {spec['params']}{ret})")
                sections.append("")
            elif first_context.get('defined_functions'):
                # Fallback to just names if specs not available
                fn_names = {s['name'] for s in first_context.get('fn_specs', [])}
                ffi_names = {s['name'] for s in first_context.get('ffi_specs', [])}
                imported = [f for f in first_context['defined_functions']
                            if f not in fn_names and f not in ffi_names and f not in VALID_EXPRESSION_FORMS and f not in BUILTIN_FUNCTIONS]
                if imported:
                    sections.append("## Imported Functions (from other modules)")
                    sections.append("These functions are imported and available to use:")
                    sections.append(f"  {', '.join(sorted(imported))}")
                    sections.append("")

        for i, (hole, context) in enumerate(holes, 1):
            sections.append(f"## Hole {i}: {context.get('fn_name', 'unknown')}")
            sections.append(f"Type: {hole.type_expr}")
            sections.append(f"Intent: {hole.prompt}")
            if context.get('params'):
                param_names = _extract_param_names(context.get('params', ''))
                if param_names:
                    sections.append(f"Available variables: {', '.join(param_names)} (use these EXACT names)")
                sections.append(f"Parameters: {context.get('params')}")
            if hole.context:
                sections.append(f"Context (allowed): {', '.join(hole.context)}")
            if hole.required:
                sections.append(f"Required: {', '.join(hole.required)}")
            sections.append("")

        sections.append("## Response Format")
        sections.append("```")
        for i in range(1, len(holes) + 1):
            sections.append(f"[HOLE {i}]")
            sections.append("<s-expression>")
        sections.append("```")

        return '\n'.join(sections)

    def _parse_batch_response(
        self,
        response: str,
        holes: List[tuple]
    ) -> List[FillResult]:
        """Parse batch response into individual FillResults"""
        results = []

        # Strip code block markers if present
        response = response.strip()
        if response.startswith('```'):
            lines = response.split('\n')
            response = '\n'.join(lines[1:-1] if lines[-1].strip() == '```' else lines[1:])

        # Split by [HOLE N] markers
        import re
        parts = re.split(r'\[HOLE\s+\d+\]', response)
        # First part is usually empty or preamble
        parts = [p.strip() for p in parts[1:] if p.strip()]

        for i, (hole, context) in enumerate(holes):
            if i < len(parts):
                expr_str = parts[i].strip()
                expr = self._parse_response(expr_str)
                if expr:
                    valid, error = self._validate(expr, hole, context)
                    if valid:
                        results.append(FillResult(
                            success=True,
                            expression=expr,
                            model_used=self.configs.get(Tier.TIER_3, ModelConfig('', '')).model,
                            attempts=1
                        ))
                    else:
                        results.append(FillResult(
                            success=False,
                            error=error or f"Validation failed: {expr_str[:50]}...",
                            attempts=1
                        ))
                else:
                    results.append(FillResult(
                        success=False,
                        error=f"Failed to parse: {expr_str[:50]}...",
                        attempts=1
                    ))
            else:
                results.append(FillResult(
                    success=False,
                    error="No response for this hole",
                    attempts=1
                ))

        return results


def replace_hole_in_expr(expr: SExpr, hole: SList, replacement: SExpr) -> SExpr:
    """Replace a specific hole in an expression with its filled value"""
    if expr is hole:
        return replacement

    if isinstance(expr, SList):
        new_items = []
        for item in expr.items:
            new_items.append(replace_hole_in_expr(item, hole, replacement))
        return SList(new_items)

    return expr


def replace_holes_in_ast(ast: List[SExpr], replacements: Dict[int, SExpr]) -> List[SExpr]:
    """Replace holes in AST using id-based replacement map"""
    result = []
    for form in ast:
        new_form = _replace_holes_recursive(form, replacements)
        result.append(new_form)
    return result


def _replace_holes_recursive(expr: SExpr, replacements: Dict[int, SExpr]) -> SExpr:
    """Recursively replace holes identified by their id()"""
    if id(expr) in replacements:
        logger.debug(f"Replacing hole id={id(expr)} with {replacements[id(expr)]}")
        return replacements[id(expr)]

    if isinstance(expr, SList):
        new_items = []
        for item in expr.items:
            new_items.append(_replace_holes_recursive(item, replacements))
        return SList(new_items)

    return expr


if __name__ == '__main__':
    # Test
    test = '''
    (fn withdraw ((account (Ptr Account)) (amount (Int 1 ..)))
      (@intent "Withdraw funds")
      (hole (Result (Ptr Account) Error)
        "Check balance and deduct"
        :complexity tier-2
        :context (account amount)))
    '''

    from slop.parser import parse, find_holes
    ast = parse(test)

    holes = find_holes(ast[0])
    print(f"Found {len(holes)} holes")

    for h in holes:
        info = extract_hole(h)
        tier = classify_tier(info)
        print(f"  {info.prompt}: {tier.name}")
