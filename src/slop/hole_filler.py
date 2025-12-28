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
from slop.parser import SExpr, SList, Symbol, String, Number, parse, find_holes, is_form, pretty_print
from slop.providers import Tier, ModelConfig, Provider, MockProvider, create_default_configs

logger = logging.getLogger(__name__)


# Cache for SKILL.md + builtins.md content
_SKILL_SPEC: Optional[str] = None

# Valid built-in expression forms from SLOP spec
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
    'cast', 'sizeof', 'addr',
    'arena-new', 'arena-alloc', 'arena-free', 'with-arena',
    # Error handling
    'try', '?',
    # Result operations
    'is-ok', 'unwrap',
    # I/O
    'print', 'println', 'file-read', 'file-write',
    # String operations
    'string-new', 'string-len', 'string-concat', 'string-eq', 'string-slice',
    'int-to-string',
    # List operations
    'list-new', 'list-push', 'list-get', 'list-len',
    # Map operations
    'map-new', 'map-put', 'map-get', 'map-has',
    # Time
    'now-ms', 'sleep-ms',
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
}


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
    """Load and cache the SLOP language spec from LANGUAGE.md, builtins.md, and patterns.md"""
    global _SKILL_SPEC
    if _SKILL_SPEC is not None:
        return _SKILL_SPEC

    # Find spec files relative to this file's location
    # hole_filler.py is in src/slop/, spec/ and skill/ are at project root
    pkg_root = Path(__file__).parent.parent.parent

    content = ""

    # Load spec/LANGUAGE.md - complete language specification with grammar
    # This includes the crucial 'symbol syntax showing quoted enum variants
    lang_path = pkg_root / "spec" / "LANGUAGE.md"
    if lang_path.exists():
        content = lang_path.read_text()

    # Load builtins.md - concise function reference with common mistakes
    builtins_path = pkg_root / "skill" / "references" / "builtins.md"
    if builtins_path.exists():
        content += "\n\n" + builtins_path.read_text()

    # Load full patterns.md - includes Error Handling section with (error 'variant) syntax
    patterns_path = pkg_root / "skill" / "references" / "patterns.md"
    if patterns_path.exists():
        content += "\n\n" + patterns_path.read_text()

    _SKILL_SPEC = content.strip()
    return _SKILL_SPEC


@dataclass
class Hole:
    type_expr: SExpr
    prompt: str
    complexity: Optional[str] = None
    constraints: Optional[List[SExpr]] = None
    examples: Optional[List[SExpr]] = None
    must_use: Optional[List[str]] = None


@dataclass
class FillResult:
    success: bool
    expression: Optional[SExpr] = None
    error: Optional[str] = None
    model_used: Optional[str] = None
    attempts: int = 0
    quality_score: float = 0.0
    quality_metrics: Optional[Dict[str, float]] = None


def extract_hole(hole_expr: SList) -> Hole:
    """Extract hole information from AST"""
    items = hole_expr.items[1:]

    type_expr = items[0] if items else None
    prompt = items[1].value if len(items) > 1 and isinstance(items[1], String) else ""

    complexity = None
    constraints = None
    examples = None
    must_use = None

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
                elif key == 'must-use':
                    if isinstance(val, SList):
                        must_use = [str(x) for x in val.items]
                    else:
                        must_use = [str(val)]
                i += 2
            else:
                i += 1
        else:
            i += 1

    return Hole(type_expr, prompt, complexity, constraints, examples, must_use)


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
        "   - string-find, string-index, substring, string-slice",
        "   - parse-int, atoi, str-to-int (use FFI if needed)",
        "   - read, write (use recv, send for sockets)",
        "   - ref (no references - use deref for pointer dereferencing)",
        "   - list-invoices, find-invoice, create-invoice (define these yourself)",
        "3. Use (do expr1 expr2 ...) for sequencing multiple expressions",
        "4. Use match with bare enum names: (match x (Foo ...) (Bar ...)) not ((Foo v) ...)",
        "5. Escape quotes in strings: \"hello \\\"world\\\"\"",
        "6. Arrays are FIXED SIZE - no .length property. Use the declared size (e.g., 100 for Array T 100)",
        "7. ENUM VARIANTS: The quote is PART OF THE SYMBOL NAME, not a separate form!",
        "   - 'not-found is a SINGLE TOKEN (the symbol literally starts with a quote character)",
        "   - DO NOT write (quote not-found) - there is NO quote function in SLOP!",
        "   - CORRECT: (error 'not-found)  - the 'not-found is one atomic symbol",
        "   - WRONG: (error (quote not-found)) - quote does not exist!",
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
        "Hole: (hole Int \"Add two numbers\" :must-use (a b))",
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

    # Explicitly list available variables with types to prevent using wrong names
    if context.get('params'):
        sections.append("")
        sections.append("## Available Variables")
        # Include typed params if available, otherwise just names
        typed_params = context.get('typed_params', context.get('params'))
        sections.append(f"IMPORTANT: Use ONLY these parameter names in your expression: {typed_params}")
        sections.append("Note: Value types use . for field access (val.field), pointer types use -> (ptr.field maps to ptr->field in C)")

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

    if hole.must_use:
        sections.append("")
        sections.append("## Must Use")
        sections.append(f"The expression MUST use: {', '.join(hole.must_use)}")

    # Include feedback from failed attempts
    if failed_attempts:
        sections.append("")
        sections.append("## Previous Failed Attempts")
        sections.append("Your previous attempts were rejected. Do NOT repeat these mistakes:")
        # Include last 3 failures to avoid prompt bloat
        for i, (attempt, error) in enumerate(failed_attempts[-3:], 1):
            sections.append("")
            sections.append(f"Attempt {i}: {attempt}...")
            sections.append(f"Error: {error}")

    sections.append("")
    sections.append("Respond with ONLY the SLOP S-expression. No explanation.")

    return '\n'.join(sections)


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

                        expr = self._parse_response(response)
                        if not expr:
                            error = "Failed to parse as valid S-expression"
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

    def _parse_response(self, response: str) -> Optional[SExpr]:
        """Parse LLM response and transform Lisp forms to SLOP equivalents"""
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
                return expr
            logger.debug("Parse returned empty list")
            return None
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            logger.debug(f"Failed to parse: {response[:200]}")
            return None

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

    def _validate(self, expr: SExpr, hole: Hole, context: dict = None) -> tuple[bool, Optional[str]]:
        """Validate expression, return (success, error_message)"""
        context = context or {}

        # Reject if the "fill" is itself a hole form
        if is_form(expr, 'hole'):
            error = "Do not return a hole form - fill it with actual code"
            logger.warning(error)
            return False, error

        # Note: (quote symbol) is transformed to 'symbol by _transform_quote_forms()
        # before reaching validation, so we don't need to reject it here

        # Reject definition forms - holes must be filled with expressions, not definitions
        invalid_forms = ['fn', 'type', 'module', 'impl', 'ffi', 'ffi-struct',
                         '@intent', '@spec', '@pre', '@post', '@example', '@pure', '@alloc']
        for form_name in invalid_forms:
            if is_form(expr, form_name):
                error = f"Do not return a '{form_name}' form - fill the hole with an expression, not a definition"
                logger.warning(error)
                return False, error

        # Recursive check: reject invented forms anywhere in the expression tree
        invalid_form = self._find_invalid_form(expr)
        if invalid_form:
            error = f"Invalid form '{invalid_form}' - not a valid SLOP expression. Use 'do' for sequencing."
            logger.warning(error)
            return False, error

        # Check for undefined function calls
        defined_fns = set(context.get('defined_functions', []))
        allowed = VALID_EXPRESSION_FORMS | defined_fns
        calls = _extract_function_calls(expr)
        undefined = calls - allowed
        if undefined:
            error = f"Undefined function(s): {', '.join(sorted(undefined))}. Only use built-ins or functions defined in this file."
            logger.warning(error)
            return False, error

        if hole.must_use:
            expr_str = str(expr)
            for must in hole.must_use:
                must_normalized = must.replace('-', '_').replace('.', '')
                expr_normalized = expr_str.replace('-', '_').replace('.', '')
                if must_normalized not in expr_normalized:
                    error = f"Expression must use '{must}' but it was not found"
                    logger.debug(error)
                    return False, error

        # Type check the filled expression against expected type
        type_error = self._check_type(expr, hole, context)
        if type_error:
            return False, type_error

        return True, None

    def _check_type(self, expr: SExpr, hole: Hole, context: dict) -> Optional[str]:
        """Type check filled expression against hole's expected type.

        Returns error message if type mismatch, None if valid.

        Policy (strict with exceptions):
        - FAIL on clear type mismatches (wrong type, missing cast)
        - ALLOW unknown types (?) from c-inline expressions
        - ALLOW unresolved type variables
        """
        from slop.type_checker import (
            TypeChecker, TypeEnv,
            Type, PrimitiveType, PtrType, OptionType, ResultType,
            TypeVar, UNKNOWN
        )

        if not hole.type_expr:
            return None  # No type constraint to check

        try:
            # Create type checker
            checker = TypeChecker()

            # Register built-in functions (from slop_runtime.h)
            from slop.type_checker import FnType, PrimitiveType
            STRING = PrimitiveType('String')
            INT64 = PrimitiveType('I64')
            BOOL = PrimitiveType('Bool')
            ARENA = PrimitiveType('Arena')

            # String operations
            checker.env.register_function('string-concat', FnType((ARENA, STRING, STRING), STRING))
            checker.env.register_function('int-to-string', FnType((ARENA, INT64), STRING))
            checker.env.register_function('string-len', FnType((STRING,), INT64))
            checker.env.register_function('string-eq', FnType((STRING, STRING), BOOL))

            # Parse and register types from type_defs (pretty-printed strings)
            from slop.type_checker import RecordType, EnumType, UnionType
            type_defs = context.get('type_defs', [])
            for type_def_str in type_defs:
                try:
                    type_ast = parse(type_def_str)
                    if type_ast and is_form(type_ast[0], 'type') and len(type_ast[0]) > 2:
                        name = type_ast[0][1].name if isinstance(type_ast[0][1], Symbol) else str(type_ast[0][1])
                        typ = checker.parse_type_expr(type_ast[0][2])
                        # Fix anonymous names for record/enum/union types
                        if isinstance(typ, RecordType) and typ.name == '<anonymous>':
                            typ = RecordType(name, typ.fields)
                        elif isinstance(typ, EnumType) and typ.name == '<anonymous>':
                            typ = EnumType(name, typ.variants)
                        elif isinstance(typ, UnionType) and typ.name == '<anonymous>':
                            typ = UnionType(name, typ.variants)
                        checker.env.register_type(name, typ)
                except Exception:
                    pass  # Skip malformed type defs

            # Parse parameters from context['params'] string like "((arena Arena) (buf (Ptr U8)))"
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
                    pass  # Skip if params can't be parsed

            # Register functions from fn_specs
            fn_specs = context.get('fn_specs', [])
            for spec in fn_specs:
                try:
                    fn_name = spec.get('name', '')
                    if fn_name and spec.get('return_type'):
                        # Parse return type
                        ret_ast = parse(spec['return_type'])
                        if ret_ast:
                            ret_type = checker.parse_type_expr(ret_ast[0])

                            # Parse parameter types from params string
                            param_types = []
                            params_str = spec.get('params', '')
                            if params_str:
                                params_ast = parse(params_str)
                                if params_ast and isinstance(params_ast[0], SList):
                                    for param in params_ast[0].items:
                                        if isinstance(param, SList) and len(param) >= 2:
                                            param_type = checker.parse_type_expr(param[1])
                                            param_types.append(param_type)

                            from slop.type_checker import FnType
                            checker.env.register_function(fn_name, FnType(tuple(param_types), ret_type))
                except Exception:
                    pass

            # Register FFI functions from ffi_specs
            ffi_specs = context.get('ffi_specs', [])
            for spec in ffi_specs:
                try:
                    fn_name = spec.get('name', '')
                    if fn_name and spec.get('return_type'):
                        # Parse return type
                        ret_ast = parse(spec['return_type'])
                        if ret_ast:
                            ret_type = checker.parse_type_expr(ret_ast[0])

                            # Parse parameter types from params string
                            param_types = []
                            params_str = spec.get('params', '')
                            if params_str:
                                params_ast = parse(params_str)
                                if params_ast and isinstance(params_ast[0], SList):
                                    for param in params_ast[0].items:
                                        if isinstance(param, SList) and len(param) >= 2:
                                            param_type = checker.parse_type_expr(param[1])
                                            param_types.append(param_type)

                            from slop.type_checker import FnType
                            checker.env.register_function(fn_name, FnType(tuple(param_types), ret_type))
                except Exception:
                    pass

            # Parse expected type
            expected = checker.parse_type_expr(hole.type_expr)

            # Infer actual type of expression
            inferred = checker.infer_expr(expr)

            # Check for type errors (especially undefined variables)
            errors = [d for d in checker.diagnostics if d.severity == 'error']
            if errors:
                # Return the first error message
                return errors[0].message

            # Check compatibility
            if not self._types_compatible(inferred, expected):
                # Check for allowable unknowns (c-inline, type vars)
                if self._is_allowable_unknown(inferred):
                    logger.warning(f"Type check warning: inferred unknown type {inferred} for expected {expected}")
                    return None  # Allow but warn
                return f"Type mismatch: expected {expected}, got {inferred}"

            return None  # Types are compatible

        except Exception as e:
            # Type checking failed - log but don't fail validation
            # (might be missing context for complex expressions)
            logger.warning(f"Type check exception (allowing): {e}")
            return None

    def _types_compatible(self, inferred: 'Type', expected: 'Type') -> bool:
        """Check if inferred type is compatible with expected type.

        Uses subtype checking where available, falls back to equality.
        """
        from slop.type_checker import TypeVar, UNKNOWN, PtrType, PrimitiveType

        # Unknown always compatible (can't verify)
        if inferred == UNKNOWN or expected == UNKNOWN:
            return True

        # Type variables are compatible (unresolved)
        if isinstance(inferred, TypeVar) or isinstance(expected, TypeVar):
            return True

        # Check subtype relationship
        if hasattr(inferred, 'is_subtype_of'):
            return inferred.is_subtype_of(expected)

        # Fall back to equality
        if hasattr(inferred, 'equals'):
            return inferred.equals(expected)

        return str(inferred) == str(expected)

    def _is_allowable_unknown(self, typ: 'Type') -> bool:
        """Check if type is an allowable unknown (c-inline, type var)."""
        from slop.type_checker import TypeVar, UNKNOWN

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

        # Check must-use coverage
        if hole.must_use:
            expr_str = str(expr)
            found = 0
            for must in hole.must_use:
                must_normalized = must.replace('-', '_').replace('.', '')
                expr_normalized = expr_str.replace('-', '_').replace('.', '')
                if must_normalized in expr_normalized:
                    found += 1
            metrics['must_use_coverage'] = found / len(hole.must_use)
        else:
            metrics['must_use_coverage'] = 1.0

        # Check for undefined calls (should be 0 if validation passed)
        defined_fns = set(context.get('defined_functions', []))
        allowed = VALID_EXPRESSION_FORMS | defined_fns
        calls = _extract_function_calls(expr)
        undefined = calls - allowed
        metrics['no_undefined_calls'] = 1.0 if not undefined else 0.0

        # Penalize multiple attempts (first attempt = 1.0, decreases by 0.15 per attempt)
        metrics['first_attempt_bonus'] = max(0.0, 1.0 - (attempts - 1) * 0.15)

        # Calculate overall score (weighted average)
        weights = {
            'parse_success': 0.2,
            'validation_pass': 0.3,
            'must_use_coverage': 0.2,
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

        for i, (hole, context) in enumerate(holes, 1):
            sections.append(f"## Hole {i}: {context.get('fn_name', 'unknown')}")
            sections.append(f"Type: {hole.type_expr}")
            sections.append(f"Intent: {hole.prompt}")
            if context.get('params'):
                sections.append(f"Parameters: {context.get('params')}")
            if hole.must_use:
                sections.append(f"Must use: {', '.join(hole.must_use)}")
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
        :must-use (account amount)))
    '''

    from slop.parser import parse, find_holes
    ast = parse(test)

    holes = find_holes(ast[0])
    print(f"Found {len(holes)} holes")

    for h in holes:
        info = extract_hole(h)
        tier = classify_tier(info)
        print(f"  {info.prompt}: {tier.name}")
