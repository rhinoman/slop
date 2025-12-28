"""
SLOP Type Checker - Type inference with range propagation

Performs compile-time type checking including:
- Type inference for all expressions
- Range bounds propagation through arithmetic
- Function signature validation
- Array bounds checking
"""

from dataclasses import dataclass, field
from typing import Union, List, Dict, Optional, Tuple, Any, Set
from slop.parser import SExpr, SList, Symbol, String, Number, is_form

# Import all type classes and constants from the shared types module
from slop.types import (
    RangeBounds, Constraint,
    Type, PrimitiveType, RangeType, ListType, ArrayType, MapType,
    RecordType, EnumType, UnionType, OptionType, ResultType, PtrType,
    FnType, TypeVar, UnknownType, UNKNOWN,
    STRING, INT, BOOL, UNIT, ARENA, BUILTIN_FUNCTIONS,
)


# ============================================================================
# Type Environment
# ============================================================================

@dataclass
class Scope:
    """A single scope level"""
    bindings: Dict[str, Type] = field(default_factory=dict)
    parent: Optional['Scope'] = None

    def lookup(self, name: str) -> Optional[Type]:
        if name in self.bindings:
            return self.bindings[name]
        if self.parent:
            return self.parent.lookup(name)
        return None

    def bind(self, name: str, typ: Type):
        self.bindings[name] = typ


class TypeEnv:
    """Type environment with scope management"""

    def __init__(self):
        self.current_scope = Scope()
        self.type_registry: Dict[str, Type] = {}
        self.function_sigs: Dict[str, FnType] = {}
        self.imported_functions: Dict[str, FnType] = {}  # imported fn name -> type
        self.imported_types: Dict[str, Type] = {}  # imported type name -> type
        self._init_builtins()

    def _init_builtins(self):
        """Initialize built-in types"""
        # Primitives
        for name in ['Int', 'Float', 'Bool', 'String', 'Bytes', 'Unit', 'Void']:
            self.type_registry[name] = PrimitiveType(name)

        # Width-specific integers
        for name in ['I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64']:
            self.type_registry[name] = PrimitiveType(name)

        self.type_registry['F32'] = PrimitiveType('F32')
        self.type_registry['Arena'] = PrimitiveType('Arena')
        self.type_registry['Milliseconds'] = RangeType('Int', RangeBounds(0, None))

    def push_scope(self):
        self.current_scope = Scope(parent=self.current_scope)

    def pop_scope(self):
        if self.current_scope.parent:
            self.current_scope = self.current_scope.parent

    def lookup_var(self, name: str) -> Optional[Type]:
        return self.current_scope.lookup(name)

    def bind_var(self, name: str, typ: Type):
        self.current_scope.bind(name, typ)

    def register_type(self, name: str, typ: Type):
        self.type_registry[name] = typ

    def lookup_type(self, name: str) -> Optional[Type]:
        return self.type_registry.get(name)

    def register_function(self, name: str, sig: FnType):
        self.function_sigs[name] = sig

    def lookup_function(self, name: str) -> Optional[FnType]:
        # Check local functions first
        if name in self.function_sigs:
            return self.function_sigs[name]
        # Then check imports
        return self.imported_functions.get(name)

    def register_import(self, name: str, fn_type: FnType):
        """Register an imported function's type signature."""
        self.imported_functions[name] = fn_type

    def register_imported_type(self, name: str, typ: Type):
        """Register an imported type."""
        self.imported_types[name] = typ
        self.type_registry[name] = typ

    def lookup_enum_variant(self, variant_name: str) -> Optional[EnumType]:
        """Look up which enum type contains a given variant."""
        for typ in self.type_registry.values():
            if isinstance(typ, EnumType) and typ.has_variant(variant_name):
                return typ
        return None


# ============================================================================
# Diagnostics
# ============================================================================

@dataclass
class SourceLocation:
    file: str = "<unknown>"
    line: int = 0
    column: int = 0


@dataclass
class TypeDiagnostic:
    severity: str  # 'error', 'warning', 'info'
    message: str
    location: Optional[SourceLocation] = None
    hint: Optional[str] = None

    def __str__(self) -> str:
        loc = ""
        if self.location and self.location.file != "<unknown>":
            loc = f"{self.location.file}:"
            if self.location.line > 0:
                loc += f"{self.location.line}:"
                if self.location.column > 0:
                    loc += f"{self.location.column}:"
            loc += " "
        result = f"{loc}{self.severity}: {self.message}"
        if self.hint:
            result += f"\n  hint: {self.hint}"
        return result


# ============================================================================
# Type Checker
# ============================================================================

class TypeChecker:
    """Main type checker with inference and range propagation"""

    def __init__(self, filename: str = "<unknown>"):
        self.env = TypeEnv()
        self.diagnostics: List[TypeDiagnostic] = []
        self.filename = filename
        self._type_var_counter = 0
        # Path-sensitive refinement context: stack of (var -> refined_type) dicts
        self._refinements: List[Dict[str, Type]] = [{}]
        # Parameter mode tracking for current function: var -> mode
        self._param_modes: Dict[str, str] = {}
        # Track which 'out' parameters have been initialized (written to)
        self._out_initialized: Set[str] = set()
        # Register built-in functions
        self._register_builtins()

    def fresh_type_var(self, name: Optional[str] = None) -> TypeVar:
        self._type_var_counter += 1
        return TypeVar(self._type_var_counter, name)

    def _register_builtins(self):
        """Register built-in function signatures."""
        # Common types
        STRING = PrimitiveType('String')
        INT = PrimitiveType('Int')
        BOOL = PrimitiveType('Bool')
        UNIT = PrimitiveType('Unit')
        ARENA = PrimitiveType('Arena')

        # I/O
        self.env.register_function('print', FnType((STRING,), UNIT))
        self.env.register_function('println', FnType((STRING,), UNIT))

        # String operations
        self.env.register_function('string-len', FnType((STRING,), INT))
        self.env.register_function('string-concat', FnType((ARENA, STRING, STRING), STRING))
        self.env.register_function('string-eq', FnType((STRING, STRING), BOOL))
        self.env.register_function('string-new', FnType((ARENA, PtrType(PrimitiveType('U8'))), STRING))
        self.env.register_function('int-to-string', FnType((ARENA, INT), STRING))

        # Arena/memory operations
        # arena-alloc returns a polymorphic pointer; use UNKNOWN so it unifies with any Ptr type
        self.env.register_function('arena-new', FnType((INT,), ARENA))
        self.env.register_function('arena-alloc', FnType((ARENA, INT), PtrType(UNKNOWN)))
        self.env.register_function('arena-free', FnType((ARENA,), UNIT))

        # List operations - return types use UNKNOWN for polymorphism
        self.env.register_function('list-new', FnType((ARENA,), ListType(UNKNOWN)))
        self.env.register_function('list-push', FnType((ListType(UNKNOWN), UNKNOWN), UNIT))
        self.env.register_function('list-get', FnType((ListType(UNKNOWN), INT), UNKNOWN))
        self.env.register_function('list-len', FnType((ListType(UNKNOWN),), INT))

        # Map operations - use UNKNOWN for polymorphism
        self.env.register_function('map-new', FnType((ARENA,), MapType(UNKNOWN, UNKNOWN)))
        self.env.register_function('map-put', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN, UNKNOWN), UNIT))
        self.env.register_function('map-get', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN), OptionType(UNKNOWN)))
        self.env.register_function('map-has', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN), BOOL))

    def error(self, message: str, node: Optional[SExpr] = None, hint: Optional[str] = None):
        line = getattr(node, 'line', 0) if node else 0
        col = getattr(node, 'col', 0) if node else 0
        self.diagnostics.append(TypeDiagnostic('error', message,
                                               SourceLocation(self.filename, line, col), hint))

    def warning(self, message: str, node: Optional[SExpr] = None, hint: Optional[str] = None):
        line = getattr(node, 'line', 0) if node else 0
        col = getattr(node, 'col', 0) if node else 0
        self.diagnostics.append(TypeDiagnostic('warning', message,
                                               SourceLocation(self.filename, line, col), hint))

    # ========================================================================
    # Path-Sensitive Refinement
    # ========================================================================

    def _push_refinements(self, refinements: Dict[str, Type]):
        """Push a new refinement context for a branch."""
        self._refinements.append(refinements)

    def _pop_refinements(self):
        """Pop the current refinement context."""
        if len(self._refinements) > 1:
            self._refinements.pop()

    def _get_refined_type(self, var: str) -> Optional[Type]:
        """Get the refined type for a variable, checking all active contexts."""
        # Check from innermost to outermost
        for ctx in reversed(self._refinements):
            if var in ctx:
                return ctx[var]
        return None

    # ========================================================================
    # Parameter Mode Handling
    # ========================================================================

    def _parse_parameter_mode(self, param: SExpr) -> Tuple[str, Optional[str], Optional[SExpr]]:
        """Extract (mode, name, type_expr) from parameter form.

        Handles:
        - (name Type)           -> ('in', 'name', Type)
        - (in name Type)        -> ('in', 'name', Type)
        - (out name Type)       -> ('out', 'name', Type)
        - (mut name Type)       -> ('mut', 'name', Type)
        """
        if isinstance(param, SList) and len(param) >= 2:
            first = param[0]

            # Check if first element is a mode keyword
            if isinstance(first, Symbol) and first.name in ('in', 'out', 'mut'):
                mode = first.name
                name = param[1].name if isinstance(param[1], Symbol) else None
                type_expr = param[2] if len(param) > 2 else None
                return (mode, name, type_expr)
            else:
                # No explicit mode -> default to 'in'
                name = first.name if isinstance(first, Symbol) else None
                type_expr = param[1] if len(param) > 1 else None
                return ('in', name, type_expr)

        return ('in', None, None)

    def _extract_constraint(self, cond: SExpr) -> Optional[Constraint]:
        """Extract a type constraint from a condition expression.

        Examples:
            (> x 0)           -> Constraint('x', '>', 0)
            (< (. obj field) 10) -> Constraint('obj.field', '<', 10)
        """
        if not isinstance(cond, SList) or len(cond) < 3:
            return None

        head = cond[0]
        if not isinstance(head, Symbol):
            return None

        op = head.name
        if op not in ('>', '<', '>=', '<=', '==', '!='):
            return None

        left = cond[1]
        right = cond[2]

        # Determine which side is the variable and which is the constant
        var_expr = None
        value = None

        if isinstance(right, Number):
            var_expr = left
            value = int(right.value)
        elif isinstance(left, Number):
            var_expr = right
            value = int(left.value)
            # Flip the operator since we're reversing operands
            op = {'<': '>', '>': '<', '<=': '>=', '>=': '<=', '==': '==', '!=': '!='}[op]
        else:
            return None  # Both sides are expressions, can't extract simple constraint

        # Extract variable name (handle field access)
        var_name = self._expr_to_var_path(var_expr)
        if var_name is None:
            return None

        return Constraint(var_name, op, value)

    def _expr_to_var_path(self, expr: SExpr) -> Optional[str]:
        """Convert expression to variable path string.

        x -> 'x'
        (. obj field) -> 'obj.field'
        """
        if isinstance(expr, Symbol):
            return expr.name

        if isinstance(expr, SList) and len(expr) >= 3:
            head = expr[0]
            if isinstance(head, Symbol) and head.name == '.':
                base = self._expr_to_var_path(expr[1])
                field = expr[2].name if isinstance(expr[2], Symbol) else None
                if base and field:
                    return f"{base}.{field}"

        return None

    def _refine_type_with_constraint(self, typ: Type, constraint: Constraint) -> Type:
        """Apply a constraint to refine a type's range bounds."""
        constraint_bounds = constraint.to_bounds()

        if isinstance(typ, RangeType):
            new_bounds = typ.bounds.intersect(constraint_bounds)
            return RangeType(typ.base, new_bounds)

        if isinstance(typ, PrimitiveType):
            if typ.name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64'):
                return RangeType(typ.name, constraint_bounds)

        return typ  # Can't refine non-numeric types

    # ========================================================================
    # Type Expression Parsing
    # ========================================================================

    def parse_type_expr(self, expr: SExpr) -> Type:
        """Convert AST type expression to Type object"""
        if isinstance(expr, Symbol):
            name = expr.name
            # Check registry first
            if name in self.env.type_registry:
                return self.env.type_registry[name]
            # Unknown type - might be forward reference
            return PrimitiveType(name)

        if isinstance(expr, SList) and len(expr) > 0:
            head = expr[0]
            if isinstance(head, Symbol):
                name = head.name

                # Range types: (Int min .. max)
                if name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64'):
                    return self._parse_range_type(name, expr)

                # Parametric types
                if name == 'List':
                    elem_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    length_bounds = self._parse_length_bounds(expr.items[2:])
                    return ListType(elem_type, length_bounds)

                if name == 'Array':
                    elem_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    size = int(expr[2].value) if len(expr) > 2 and isinstance(expr[2], Number) else 0
                    return ArrayType(elem_type, size)

                if name == 'Option':
                    inner = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return OptionType(inner)

                if name == 'Result':
                    ok_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    err_type = self.parse_type_expr(expr[2]) if len(expr) > 2 else PrimitiveType('Error')
                    return ResultType(ok_type, err_type)

                if name == 'Map':
                    key_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    value_type = self.parse_type_expr(expr[2]) if len(expr) > 2 else UNKNOWN
                    return MapType(key_type, value_type)

                if name == 'Ptr':
                    pointee = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return PtrType(pointee)

                if name == 'OwnPtr':
                    pointee = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return PtrType(pointee, owning=True)

                if name == 'OptPtr':
                    pointee = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return PtrType(pointee, nullable=True)

                if name == 'Fn':
                    return self._parse_fn_type(expr)

                if name == 'record':
                    return self._parse_record_type_inline(expr)

                if name == 'enum':
                    return self._parse_enum_type_inline(expr)

                if name == 'union':
                    return self._parse_union_type_inline(expr)

                # Check if it's a known type name
                if name in self.env.type_registry:
                    return self.env.type_registry[name]

        return UNKNOWN

    def _parse_range_type(self, base: str, expr: SList) -> Type:
        """Parse (Int min .. max) style range types"""
        if len(expr) == 1:
            # Just (Int) - unbounded
            return RangeType(base, RangeBounds())

        min_val, max_val = None, None
        items = expr.items[1:]
        has_range_op = False

        i = 0
        while i < len(items):
            item = items[i]
            if isinstance(item, Number):
                if not has_range_op:
                    min_val = int(item.value)
                else:
                    max_val = int(item.value)
            elif isinstance(item, Symbol) and item.name == '..':
                has_range_op = True
            i += 1

        # Handle (Int 5 ..) - min only
        if has_range_op and max_val is None and min_val is not None:
            return RangeType(base, RangeBounds(min_val, None))

        # Handle (Int .. 100) - max only
        if has_range_op and min_val is None:
            # Actually min_val would be the first number before ..
            pass

        return RangeType(base, RangeBounds(min_val, max_val))

    def _parse_length_bounds(self, items: List[SExpr]) -> Optional[RangeBounds]:
        """Parse length bounds from list type: (List T min .. max)"""
        if not items:
            return None

        min_val, max_val = None, None
        has_range = False

        for item in items:
            if isinstance(item, Number):
                if not has_range:
                    min_val = int(item.value)
                else:
                    max_val = int(item.value)
            elif isinstance(item, Symbol) and item.name == '..':
                has_range = True

        if min_val is not None or max_val is not None:
            return RangeBounds(min_val, max_val)
        return None

    def _parse_fn_type(self, expr: SList) -> FnType:
        """Parse (Fn (A B) -> R) function type"""
        param_types: List[Type] = []
        return_type: Type = PrimitiveType('Unit')

        i = 1
        while i < len(expr):
            item = expr[i]
            if isinstance(item, Symbol) and item.name == '->':
                if i + 1 < len(expr):
                    return_type = self.parse_type_expr(expr[i + 1])
                break
            elif isinstance(item, SList):
                # Parameter list
                for p in item:
                    param_types.append(self.parse_type_expr(p))
            i += 1

        return FnType(tuple(param_types), return_type)

    def _parse_record_type_inline(self, expr: SList) -> RecordType:
        """Parse inline (record (field T) ...) type"""
        fields: Dict[str, Type] = {}
        for item in expr.items[1:]:
            if isinstance(item, SList) and len(item) >= 2:
                field_name = item[0].name if isinstance(item[0], Symbol) else str(item[0])
                field_type = self.parse_type_expr(item[1])
                fields[field_name] = field_type
        return RecordType("<anonymous>", fields)

    def _parse_enum_type_inline(self, expr: SList) -> EnumType:
        """Parse inline (enum v1 v2 ...) type"""
        variants = []
        for item in expr.items[1:]:
            if isinstance(item, Symbol):
                variants.append(item.name)
        return EnumType("<anonymous>", variants)

    def _parse_union_type_inline(self, expr: SList) -> UnionType:
        """Parse inline (union (tag T) ...) type"""
        variants: Dict[str, Optional[Type]] = {}
        for item in expr.items[1:]:
            if isinstance(item, SList) and len(item) >= 1:
                tag = item[0].name if isinstance(item[0], Symbol) else str(item[0])
                payload = self.parse_type_expr(item[1]) if len(item) > 1 else None
                variants[tag] = payload
            elif isinstance(item, Symbol):
                variants[item.name] = None
        return UnionType("<anonymous>", variants)

    # ========================================================================
    # Expression Type Inference
    # ========================================================================

    def infer_expr(self, expr: SExpr) -> Type:
        """Infer type of expression"""
        if isinstance(expr, Number):
            if isinstance(expr.value, float) or '.' in str(expr.value):
                return PrimitiveType('Float')
            val = int(expr.value)
            return RangeType('Int', RangeBounds(val, val))

        if isinstance(expr, String):
            return PrimitiveType('String')

        if isinstance(expr, Symbol):
            return self._infer_symbol(expr)

        if isinstance(expr, SList):
            if len(expr) == 0:
                # Empty list () is Unit
                return PrimitiveType('Unit')
            return self._infer_compound(expr)

        return UNKNOWN

    def _infer_symbol(self, sym: Symbol) -> Type:
        """Infer type of symbol reference"""
        name = sym.name

        # Handle dotted field access: foo.bar.baz
        if '.' in name:
            parts = name.split('.')
            # Check refinements first, then variable lookup
            base_type = self._get_refined_type(parts[0])
            if base_type is None:
                base_type = self.env.lookup_var(parts[0])
            if base_type is None:
                self.error(f"Undefined variable: {parts[0]}", sym)
                return UNKNOWN

            current_type = base_type
            for field in parts[1:]:
                actual_type = current_type
                if isinstance(current_type, PtrType):
                    actual_type = current_type.pointee

                if isinstance(actual_type, RecordType):
                    field_type = actual_type.get_field(field)
                    if field_type:
                        current_type = field_type
                    else:
                        self.error(f"Record '{actual_type.name}' has no field '{field}'", sym)
                        return UNKNOWN
                else:
                    # Can't access field on non-record type
                    return UNKNOWN

            return current_type

        if name == 'nil':
            return PtrType(self.fresh_type_var(), nullable=True)
        if name in ('true', 'false'):
            return PrimitiveType('Bool')
        if name.startswith("'"):
            # Quoted symbol - enum variant, look up which enum it belongs to
            variant_name = name[1:]  # Strip the quote
            enum_type = self.env.lookup_enum_variant(variant_name)
            if enum_type:
                return enum_type
            # Unknown variant, use type variable
            return self.fresh_type_var('EnumVariant')

        # Check path-sensitive refinements first
        refined = self._get_refined_type(name)
        if refined:
            return refined

        # Variable lookup
        typ = self.env.lookup_var(name)
        if typ:
            # Check for uninitialized 'out' parameter read
            if name in self._param_modes and self._param_modes[name] == 'out':
                if name not in self._out_initialized:
                    self.warning(f"Reading from 'out' parameter '{name}' before initialization", sym,
                                hint="'out' parameters must be written to before reading.")
            return typ

        # Function lookup (for first-class functions)
        fn_type = self.env.lookup_function(name)
        if fn_type:
            return fn_type

        self.error(f"Undefined variable: {name}", sym)
        return UNKNOWN

    def _infer_compound(self, expr: SList) -> Type:
        """Infer type of compound expression"""
        head = expr[0]
        if not isinstance(head, Symbol):
            # Could be function expression call
            return UNKNOWN

        op = head.name

        # Arithmetic with range propagation
        if op in ('+', '-', '*', '/', '%'):
            return self._infer_arithmetic(op, expr)

        # Comparisons
        if op in ('==', '!=', '<', '<=', '>', '>='):
            self._check_comparison(expr)
            return PrimitiveType('Bool')

        # Boolean operations
        if op in ('and', 'or'):
            self._check_bool_operands(expr)
            return PrimitiveType('Bool')
        if op == 'not':
            if len(expr) > 1:
                operand_type = self.infer_expr(expr[1])
                self._expect_type(operand_type, PrimitiveType('Bool'), "not operand", expr[1])
            return PrimitiveType('Bool')

        # Conditionals
        if op == 'if':
            return self._infer_if(expr)

        # Match expression
        if op == 'match':
            return self._infer_match(expr)

        # Let bindings
        if op in ('let', 'let*'):
            return self._infer_let(expr)

        # Do block
        if op == 'do':
            return self._infer_do(expr)

        # Field access
        if op == '.':
            return self._infer_field_access(expr)

        # Index access
        if op == '@':
            return self._infer_index_access(expr)

        # Assignment
        if op == 'set!':
            return self._infer_set(expr)

        # Result/Option constructors
        if op == 'ok':
            inner = self.infer_expr(expr[1]) if len(expr) > 1 else PrimitiveType('Unit')
            return ResultType(inner, self.fresh_type_var('E'))
        if op == 'error':
            err = self.infer_expr(expr[1]) if len(expr) > 1 else PrimitiveType('Error')
            return ResultType(self.fresh_type_var('T'), err)
        if op == 'some':
            inner = self.infer_expr(expr[1]) if len(expr) > 1 else UNKNOWN
            return OptionType(inner)
        if op == 'none':
            return OptionType(self.fresh_type_var('T'))

        # Map literal: (map (k1 v1) (k2 v2) ...)
        if op == 'map':
            return self._infer_map_literal(expr)

        # Quote - check if it's an enum variant
        if op == 'quote':
            if len(expr) > 1 and isinstance(expr[1], Symbol):
                variant_name = expr[1].name
                enum_type = self.env.lookup_enum_variant(variant_name)
                if enum_type:
                    return enum_type
            return self.fresh_type_var('Symbol')

        # Annotations - skip
        if op.startswith('@'):
            return PrimitiveType('Unit')

        # C inline escape hatch - defaults to Int, or (c-inline "code" Type)
        if op == 'c-inline':
            if len(expr) >= 3:
                # Optional type annotation: (c-inline "code" Type)
                return self.parse_type_expr(expr[2])
            # Default to Int (most C functions return int)
            return PrimitiveType('Int')

        # Hole - typed placeholder for LLM generation
        if op == 'hole':
            return self._infer_hole(expr)

        # Cast
        if op == 'cast':
            if len(expr) >= 2:
                return self.parse_type_expr(expr[1])
            return UNKNOWN

        # With-arena
        if op == 'with-arena':
            return self._infer_with_arena(expr)

        # Function call
        return self._infer_call(expr)

    # ========================================================================
    # Range Propagation
    # ========================================================================

    def _infer_arithmetic(self, op: str, expr: SList) -> Type:
        """Infer type of arithmetic expression with range propagation"""
        if len(expr) < 3:
            self.error(f"Arithmetic operator '{op}' requires 2 operands", expr)
            return PrimitiveType('Int')

        left = self.infer_expr(expr[1])
        right = self.infer_expr(expr[2])

        left_range = self._get_range_bounds(left)
        right_range = self._get_range_bounds(right)

        # If either operand is Float, result is Float
        if self._is_float_type(left) or self._is_float_type(right):
            return PrimitiveType('Float')

        if left_range is None or right_range is None:
            return PrimitiveType('Int')

        result_bounds = self._propagate_range(op, left_range, right_range)
        return RangeType('Int', result_bounds)

    def _get_range_bounds(self, typ: Type) -> Optional[RangeBounds]:
        """Extract range bounds from type if available"""
        if isinstance(typ, RangeType):
            return typ.bounds
        if isinstance(typ, PrimitiveType):
            if typ.name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64'):
                return RangeBounds()  # Unbounded
        if isinstance(typ, TypeVar):
            resolved = typ.resolve()
            if resolved != typ:
                return self._get_range_bounds(resolved)
        return None

    def _is_float_type(self, typ: Type) -> bool:
        if isinstance(typ, PrimitiveType):
            return typ.name in ('Float', 'F32')
        return False

    def _propagate_range(self, op: str, left: RangeBounds, right: RangeBounds) -> RangeBounds:
        """Propagate range bounds through arithmetic operation"""

        if op == '+':
            min_val = None
            max_val = None
            if left.min_val is not None and right.min_val is not None:
                min_val = left.min_val + right.min_val
            if left.max_val is not None and right.max_val is not None:
                max_val = left.max_val + right.max_val
            return RangeBounds(min_val, max_val)

        if op == '-':
            min_val = None
            max_val = None
            # a - b: min is a.min - b.max, max is a.max - b.min
            if left.min_val is not None and right.max_val is not None:
                min_val = left.min_val - right.max_val
            elif left.min_val is not None and right.max_val is None:
                min_val = None  # Unbounded
            if left.max_val is not None and right.min_val is not None:
                max_val = left.max_val - right.min_val
            elif left.max_val is not None and right.min_val is None:
                max_val = None  # Unbounded
            return RangeBounds(min_val, max_val)

        if op == '*':
            # Multiplication: consider all corner cases
            if (left.min_val is None or left.max_val is None or
                right.min_val is None or right.max_val is None):
                # Can't determine bounds precisely
                # But we can still infer some bounds
                if (left.min_val is not None and left.min_val >= 0 and
                    right.min_val is not None and right.min_val >= 0):
                    # Both non-negative
                    min_val = left.min_val * right.min_val
                    max_val = None
                    if left.max_val is not None and right.max_val is not None:
                        max_val = left.max_val * right.max_val
                    return RangeBounds(min_val, max_val)
                return RangeBounds()

            products = [
                left.min_val * right.min_val,
                left.min_val * right.max_val,
                left.max_val * right.min_val,
                left.max_val * right.max_val
            ]
            return RangeBounds(min(products), max(products))

        if op == '/':
            # Division: be conservative
            if right.min_val is not None and right.max_val is not None:
                if right.min_val > 0 or right.max_val < 0:
                    # Divisor doesn't include zero
                    if left.min_val is not None and left.max_val is not None:
                        if right.min_val > 0:
                            return RangeBounds(
                                left.min_val // right.max_val,
                                left.max_val // right.min_val
                            )
                        elif right.max_val < 0:
                            return RangeBounds(
                                left.max_val // right.max_val,
                                left.min_val // right.min_val
                            )
                elif 0 in range(right.min_val, right.max_val + 1):
                    self.warning("Division may include zero in divisor range")
            return RangeBounds()

        if op == '%':
            # Modulo: result is in [0, |divisor|-1] for positive dividend
            if right.min_val is not None and right.min_val > 0:
                if right.max_val is not None:
                    return RangeBounds(0, right.max_val - 1)
                return RangeBounds(0, None)
            return RangeBounds()

        return RangeBounds()

    # ========================================================================
    # Specific Expression Inference
    # ========================================================================

    def _check_comparison(self, expr: SList):
        """Check types in comparison expression"""
        if len(expr) < 3:
            return
        left = self.infer_expr(expr[1])
        right = self.infer_expr(expr[2])
        # Allow comparison between compatible numeric types
        if not self._types_comparable(left, right):
            self.warning(f"Comparing potentially incompatible types: {left} vs {right}", expr)

    def _types_comparable(self, a: Type, b: Type) -> bool:
        """Check if two types can be compared"""
        # UNKNOWN is compatible with anything (we don't have enough info to reject)
        if isinstance(a, UnknownType) or isinstance(b, UnknownType):
            return True
        if a.equals(b):
            return True
        # Numeric types are comparable
        numeric_types = {'Int', 'Float', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64', 'F32'}
        a_numeric = (isinstance(a, PrimitiveType) and a.name in numeric_types) or isinstance(a, RangeType)
        b_numeric = (isinstance(b, PrimitiveType) and b.name in numeric_types) or isinstance(b, RangeType)
        if a_numeric and b_numeric:
            return True
        # Pointer types are comparable (for == and !=)
        if isinstance(a, PtrType) and isinstance(b, PtrType):
            return True
        return False

    def _check_bool_operands(self, expr: SList):
        """Check that boolean operands are Bool"""
        for operand in expr.items[1:]:
            typ = self.infer_expr(operand)
            if not isinstance(typ, PrimitiveType) or typ.name != 'Bool':
                self.warning(f"Boolean operator expects Bool, got {typ}", operand)

    def _infer_map_literal(self, expr: SList) -> Type:
        """Infer type of map literal: (map (k1 v1) (k2 v2) ...)"""
        pairs = expr.items[1:]

        if not pairs:
            # Empty map - use type variables for key/value types
            return MapType(self.fresh_type_var('K'), self.fresh_type_var('V'))

        # Get types from first pair
        first = pairs[0]
        if not isinstance(first, SList) or len(first) < 2:
            self.error("Map literal requires (key value) pairs", first if isinstance(first, SList) else expr)
            return MapType(UNKNOWN, UNKNOWN)

        key_type = self.infer_expr(first[0])
        value_type = self.infer_expr(first[1])

        # Check remaining pairs for type consistency
        for pair in pairs[1:]:
            if not isinstance(pair, SList) or len(pair) < 2:
                self.error("Map literal requires (key value) pairs", pair if isinstance(pair, SList) else expr)
                continue

            k_type = self.infer_expr(pair[0])
            v_type = self.infer_expr(pair[1])

            if not k_type.equals(key_type):
                self.error(f"Map key type mismatch: expected {key_type}, got {k_type}", pair[0])
            if not v_type.equals(value_type):
                self.error(f"Map value type mismatch: expected {value_type}, got {v_type}", pair[1])

        return MapType(key_type, value_type)

    def _infer_if(self, expr: SList) -> Type:
        """Infer type of if expression with path-sensitive refinement."""
        if len(expr) < 3:
            return UNKNOWN

        cond = expr[1]

        # Check condition is Bool
        cond_type = self.infer_expr(cond)
        self._expect_type(cond_type, PrimitiveType('Bool'), "if condition", cond)

        # Extract constraint for path-sensitive refinement
        constraint = self._extract_constraint(cond)

        # Infer then-branch with refinement
        if constraint:
            # Get current type of the variable being constrained
            var_type = self._lookup_var_for_refinement(constraint.var)
            if var_type:
                refined_type = self._refine_type_with_constraint(var_type, constraint)
                self._push_refinements({constraint.var: refined_type})
            else:
                self._push_refinements({})
        else:
            self._push_refinements({})

        then_type = self.infer_expr(expr[2])
        self._pop_refinements()

        # Infer else-branch with negated refinement
        if constraint and len(expr) > 3:
            var_type = self._lookup_var_for_refinement(constraint.var)
            if var_type:
                negated = constraint.negate()
                refined_type = self._refine_type_with_constraint(var_type, negated)
                self._push_refinements({constraint.var: refined_type})
            else:
                self._push_refinements({})
        else:
            self._push_refinements({})

        else_type = self.infer_expr(expr[3]) if len(expr) > 3 else PrimitiveType('Unit')
        self._pop_refinements()

        # Result is union of branch types
        return self._unify_branch_types(then_type, else_type, expr)

    def _lookup_var_for_refinement(self, var_path: str) -> Optional[Type]:
        """Look up type of a variable (or field path) for refinement."""
        # Simple variable
        if '.' not in var_path:
            # Check existing refinements first
            refined = self._get_refined_type(var_path)
            if refined:
                return refined
            return self.env.lookup_var(var_path)

        # Field path like "obj.field"
        parts = var_path.split('.')
        var_type = self.env.lookup_var(parts[0])
        if not var_type:
            return None

        for field in parts[1:]:
            actual_type = var_type
            if isinstance(var_type, PtrType):
                actual_type = var_type.pointee

            if isinstance(actual_type, RecordType):
                field_type = actual_type.get_field(field)
                if field_type:
                    var_type = field_type
                else:
                    return None
            else:
                return None

        return var_type

    def _infer_match(self, expr: SList) -> Type:
        """Infer type of match expression"""
        if len(expr) < 2:
            return UNKNOWN

        scrutinee_type = self.infer_expr(expr[1])
        branch_types: List[Type] = []

        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                # Pattern is clause[0], body is rest
                # For now, just infer body type
                self.env.push_scope()
                # Bind pattern variables (simplified)
                self._bind_pattern(clause[0], scrutinee_type)
                body_type = self.infer_expr(clause[-1])
                branch_types.append(body_type)
                self.env.pop_scope()

        if not branch_types:
            return UNKNOWN

        result = branch_types[0]
        for t in branch_types[1:]:
            result = self._unify_branch_types(result, t, expr)
        return result

    def _bind_pattern(self, pattern: SExpr, scrutinee_type: Type):
        """Bind variables from pattern match"""
        if isinstance(pattern, Symbol):
            name = pattern.name
            if name != '_' and not name.startswith("'"):
                # Check if bare symbol matches a known enum variant
                enum_type = self.env.lookup_enum_variant(name)
                if enum_type:
                    self.warning(
                        f"Bare '{name}' in pattern matches enum {enum_type.name} - use '{name} for value match",
                        pattern,
                        hint=f"Change to '{name} to match the enum value, or use a different variable name"
                    )
                self.env.bind_var(name, scrutinee_type)
        elif isinstance(pattern, SList) and len(pattern) >= 1:
            # (tag var) pattern for union types
            if isinstance(pattern[0], Symbol):
                tag = pattern[0].name
                if isinstance(scrutinee_type, (UnionType, ResultType, OptionType)):
                    if isinstance(scrutinee_type, UnionType):
                        payload_type = scrutinee_type.get_variant(tag)
                    elif isinstance(scrutinee_type, ResultType):
                        if tag == 'ok':
                            payload_type = scrutinee_type.ok_type
                        elif tag == 'error':
                            payload_type = scrutinee_type.err_type
                        else:
                            payload_type = UNKNOWN
                    elif isinstance(scrutinee_type, OptionType):
                        if tag == 'some':
                            payload_type = scrutinee_type.inner
                        else:
                            payload_type = None
                    else:
                        payload_type = UNKNOWN

                    if len(pattern) > 1 and payload_type:
                        for var in pattern.items[1:]:
                            if isinstance(var, Symbol) and var.name != '_':
                                self.env.bind_var(var.name, payload_type)

    def _infer_let(self, expr: SList) -> Type:
        """Infer type of let expression"""
        if len(expr) < 2:
            return UNKNOWN

        self.env.push_scope()

        bindings = expr[1] if isinstance(expr[1], SList) else SList([])
        for binding in bindings:
            if isinstance(binding, SList) and len(binding) >= 2:
                var_name = binding[0].name if isinstance(binding[0], Symbol) else str(binding[0])
                var_type = self.infer_expr(binding[1])
                self.env.bind_var(var_name, var_type)

        # Infer body type
        body_type = PrimitiveType('Unit')
        for body_expr in expr.items[2:]:
            body_type = self.infer_expr(body_expr)

        self.env.pop_scope()
        return body_type

    def _infer_do(self, expr: SList) -> Type:
        """Infer type of do block - returns last expression's type"""
        result = PrimitiveType('Unit')
        for item in expr.items[1:]:
            result = self.infer_expr(item)
        return result

    def _infer_field_access(self, expr: SList) -> Type:
        """Infer type of field access: (. obj field)"""
        if len(expr) < 3:
            return UNKNOWN

        # Check for refined type of the full field path
        var_path = self._expr_to_var_path(expr)
        if var_path:
            refined = self._get_refined_type(var_path)
            if refined:
                return refined

        obj_type = self.infer_expr(expr[1])
        field_name = expr[2].name if isinstance(expr[2], Symbol) else str(expr[2])

        # Handle pointer dereference
        actual_type = obj_type
        if isinstance(obj_type, PtrType):
            actual_type = obj_type.pointee

        # Resolve forward references - if we have a PrimitiveType that's actually
        # a registered RecordType, use the registered type
        if isinstance(actual_type, PrimitiveType):
            resolved = self.env.lookup_type(actual_type.name)
            if resolved:
                actual_type = resolved

        if isinstance(actual_type, RecordType):
            field_type = actual_type.get_field(field_name)
            if field_type:
                return field_type
            self.error(f"Record '{actual_type.name}' has no field '{field_name}'", expr)
        else:
            # Might be FFI struct - allow it
            pass

        return UNKNOWN

    def _infer_index_access(self, expr: SList) -> Type:
        """Infer type of index access with bounds checking: (@ arr idx)"""
        if len(expr) < 3:
            return UNKNOWN

        array_type = self.infer_expr(expr[1])
        index_type = self.infer_expr(expr[2])

        # Check index is integral
        index_range = self._get_range_bounds(index_type)
        if index_range is None:
            self.warning("Array index should be an integer type", expr[2])

        # Bounds checking
        if isinstance(array_type, ArrayType):
            if index_range:
                if index_range.min_val is not None and index_range.min_val < 0:
                    self.error(f"Array index may be negative: {index_range.min_val}",
                              expr[2], hint="Add a bounds check before indexing")
                if index_range.max_val is not None and index_range.max_val >= array_type.size:
                    self.error(f"Array index may exceed bounds: {index_range.max_val} >= {array_type.size}",
                              expr[2], hint="Ensure index is less than array size")
            return array_type.element_type

        if isinstance(array_type, ListType):
            if index_range and array_type.length_bounds:
                if (index_range.max_val is not None and
                    array_type.length_bounds.max_val is not None and
                    index_range.max_val >= array_type.length_bounds.max_val):
                    self.warning(f"List index may exceed length bounds", expr[2])
            return array_type.element_type

        return UNKNOWN

    def _infer_set(self, expr: SList) -> Type:
        """Infer type of assignment: (set! target value) or (set! obj field value)"""
        if len(expr) == 3:
            # (set! var value) or (set! obj.field value)
            target = expr[1]
            value_type = self.infer_expr(expr[2])
            if isinstance(target, Symbol):
                name = target.name
                if '.' in name:
                    # Handle dotted field access: (set! obj.field value)
                    target_type = self._infer_symbol(target)
                    if not isinstance(target_type, UnknownType):
                        self._check_assignable(value_type, target_type, f"assignment to '{name}'", expr[2])
                else:
                    # Simple variable assignment
                    # Check parameter mode constraints
                    if name in self._param_modes:
                        mode = self._param_modes[name]
                        if mode == 'in':
                            self.error(f"Cannot assign to 'in' parameter '{name}'", target,
                                      hint="'in' parameters are read-only. Use 'mut' mode for read-write access.")
                        elif mode == 'out':
                            # Mark out parameter as initialized
                            self._out_initialized.add(name)
                    expected = self.env.lookup_var(name)
                    if expected:
                        self._check_assignable(value_type, expected, f"assignment to '{name}'", expr[2])
        elif len(expr) == 4:
            # (set! obj field value)
            obj_type = self.infer_expr(expr[1])
            field_name = expr[2].name if isinstance(expr[2], Symbol) else str(expr[2])
            value_type = self.infer_expr(expr[3])

            actual_type = obj_type
            if isinstance(obj_type, PtrType):
                actual_type = obj_type.pointee

            if isinstance(actual_type, RecordType):
                field_type = actual_type.get_field(field_name)
                if field_type:
                    self._check_assignable(value_type, field_type, f"field '{field_name}'", expr[3])

        return PrimitiveType('Unit')

    def _infer_hole(self, expr: SList) -> Type:
        """Infer type of hole expression and check for :must-use.

        (hole Type "prompt" :complexity tier-2 :must-use (fn1 fn2))
        """
        if len(expr) < 2:
            self.error("Hole requires a type", expr)
            return UNKNOWN

        # Parse the type (first argument)
        hole_type = self.parse_type_expr(expr[1])

        # Check for :must-use - warn if missing
        has_must_use = False
        for i, item in enumerate(expr.items):
            if isinstance(item, Symbol) and item.name == ':must-use':
                has_must_use = True
                break

        if not has_must_use:
            self.warning("Hole is missing :must-use - add it for better LLM context filtering", expr)

        return hole_type

    def _infer_with_arena(self, expr: SList) -> Type:
        """Infer type of with-arena block"""
        self.env.push_scope()
        self.env.bind_var('arena', PrimitiveType('Arena'))

        result = PrimitiveType('Unit')
        for item in expr.items[1:]:
            if isinstance(item, Number):
                continue  # Arena size
            result = self.infer_expr(item)

        self.env.pop_scope()
        return result

    def _infer_call(self, expr: SList) -> Type:
        """Infer type of function call and validate arguments"""
        head = expr[0]
        if not isinstance(head, Symbol):
            return UNKNOWN

        fn_name = head.name
        args = expr.items[1:]

        fn_type = self.env.lookup_function(fn_name)
        if fn_type is None:
            # Unknown function - might be FFI or runtime
            return UNKNOWN

        # Check argument count
        if len(args) != len(fn_type.param_types):
            self.error(f"Function '{fn_name}' expects {len(fn_type.param_types)} arguments, got {len(args)}", expr)

        # Check argument types
        for i, (arg, expected) in enumerate(zip(args, fn_type.param_types)):
            arg_type = self.infer_expr(arg)
            self._check_assignable(arg_type, expected, f"argument {i+1} to '{fn_name}'", arg)

        return fn_type.return_type

    # ========================================================================
    # Type Checking Helpers
    # ========================================================================

    def _expect_type(self, actual: Type, expected: Type, context: str, node: Optional[SExpr] = None):
        """Check that actual type matches expected"""
        if isinstance(actual, Type) and not isinstance(actual, UnknownType):
            self._check_assignable(actual, expected, context, node)

    def _get_type_name(self, t: Type) -> Optional[str]:
        """Get the name of a type for forward reference comparison"""
        if isinstance(t, PrimitiveType):
            return t.name
        if isinstance(t, RecordType):
            return t.name if t.name != '<anonymous>' else None
        if isinstance(t, EnumType):
            return t.name if t.name != '<anonymous>' else None
        if isinstance(t, UnionType):
            return t.name if t.name != '<anonymous>' else None
        return None

    def _types_same_by_name(self, a: Type, b: Type) -> bool:
        """Check if types are the same by name (for forward references)"""
        a_name = self._get_type_name(a)
        b_name = self._get_type_name(b)
        return a_name is not None and a_name == b_name

    def _range_fits_in_type(self, range_type: RangeType, target: str) -> bool:
        """Check if a range type fits within a fixed-width integer type"""
        type_ranges = {
            'U8': (0, 255), 'U16': (0, 65535), 'U32': (0, 2**32-1), 'U64': (0, 2**64-1),
            'I8': (-128, 127), 'I16': (-32768, 32767), 'I32': (-2**31, 2**31-1), 'I64': (-2**63, 2**63-1)
        }
        if target not in type_ranges:
            return False
        tmin, tmax = type_ranges[target]
        bounds = range_type.bounds
        rmin = bounds.min_val if bounds.min_val is not None else float('-inf')
        rmax = bounds.max_val if bounds.max_val is not None else float('inf')
        return rmin >= tmin and rmax <= tmax

    def _check_assignable(self, source: Type, target: Type, context: str, node: Optional[SExpr] = None):
        """Check if source type can be assigned to target type"""
        if isinstance(source, UnknownType) or isinstance(target, UnknownType):
            return  # Can't check unknown types

        if source.equals(target):
            return

        if source.is_subtype_of(target):
            return

        # Parameterized type compatibility with UNKNOWN inner types
        # (List ?) matches (List T), (Map ? ?) matches (Map K V), etc.
        if isinstance(source, ListType) and isinstance(target, ListType):
            if isinstance(source.element_type, UnknownType) or isinstance(target.element_type, UnknownType):
                return

        if isinstance(source, MapType) and isinstance(target, MapType):
            if (isinstance(source.key_type, UnknownType) or isinstance(target.key_type, UnknownType) or
                isinstance(source.value_type, UnknownType) or isinstance(target.value_type, UnknownType)):
                return

        if isinstance(source, OptionType) and isinstance(target, OptionType):
            if isinstance(source.inner, UnknownType) or isinstance(target.inner, UnknownType):
                return

        # Handle range subtyping
        if isinstance(source, RangeType) and isinstance(target, RangeType):
            if source.base == target.base and source.bounds.is_subrange_of(target.bounds):
                return
            # Check if source range fits in target range
            if source.base == target.base:
                if not source.bounds.is_subrange_of(target.bounds):
                    self.error(f"{context}: range {source} does not fit in {target}",
                              node, hint="Value may exceed target range bounds")
                    return

        # Range type assignable to unbounded base type
        if isinstance(source, RangeType) and isinstance(target, PrimitiveType):
            if source.base == target.name:
                return

        # Option wrapping
        if isinstance(target, OptionType) and source.equals(target.inner):
            return

        # Pointer compatibility
        if isinstance(source, PtrType) and isinstance(target, PtrType):
            # UNKNOWN pointee matches any type (polymorphic pointers like arena-alloc)
            if isinstance(source.pointee, UnknownType) or isinstance(target.pointee, UnknownType):
                return
            pointees_match = source.pointee.equals(target.pointee)
            # Forward reference fallback: compare by name
            if not pointees_match:
                pointees_match = self._types_same_by_name(source.pointee, target.pointee)
            if pointees_match:
                if target.nullable and not source.nullable:
                    return  # Non-null to nullable is OK
                if not target.nullable and source.nullable:
                    self.warning(f"{context}: assigning nullable pointer to non-nullable", node)
                    return
                return

        # Range type assignable to fixed-width integers
        if isinstance(source, RangeType) and isinstance(target, PrimitiveType):
            if target.name in ('U8', 'U16', 'U32', 'U64', 'I8', 'I16', 'I32', 'I64'):
                if self._range_fits_in_type(source, target.name):
                    return

        # Result type with type variables - allow if structure matches
        if isinstance(source, ResultType) and isinstance(target, ResultType):
            # If source has unresolved type variables, allow it (can't fully verify)
            if isinstance(source.ok_type, TypeVar) or isinstance(source.err_type, TypeVar):
                return
            # If target has anonymous record, be lenient
            if (isinstance(target.ok_type, RecordType) and
                target.ok_type.name == '<anonymous>'):
                # Just verify error types are compatible
                if source.err_type.equals(target.err_type) or source.err_type.is_subtype_of(target.err_type):
                    return

        self.error(f"{context}: expected {target}, got {source}", node)

    def _unify_branch_types(self, a: Type, b: Type, node: Optional[SExpr] = None) -> Type:
        """Unify types from different branches (if/match)"""
        # UNKNOWN can unify with anything - return the known type
        if isinstance(a, UnknownType):
            return b
        if isinstance(b, UnknownType):
            return a

        if a.equals(b):
            return a

        # Range union
        if isinstance(a, RangeType) and isinstance(b, RangeType) and a.base == b.base:
            return RangeType(a.base, a.bounds.union(b.bounds))

        # Result type unification with TypeVars
        # (Result Pet E) + (Result T ApiError) => (Result Pet ApiError)
        if isinstance(a, ResultType) and isinstance(b, ResultType):
            ok_type = self._unify_types(a.ok_type, b.ok_type)
            err_type = self._unify_types(a.err_type, b.err_type)
            return ResultType(ok_type, err_type)

        # Option type unification
        if isinstance(a, OptionType) and isinstance(b, OptionType):
            inner = self._unify_types(a.inner, b.inner)
            return OptionType(inner)

        # One is subtype of other
        if a.is_subtype_of(b):
            return b
        if b.is_subtype_of(a):
            return a

        # Different types - return union (simplified to first type with warning)
        self.warning(f"Branch types differ: {a} vs {b}", node)
        return a

    def _unify_types(self, a: Type, b: Type) -> Type:
        """Unify two types, resolving TypeVars to concrete types."""
        # If either is a TypeVar, prefer the concrete type
        if isinstance(a, TypeVar):
            if a.bound:
                return self._unify_types(a.bound, b)
            return b  # Unbound TypeVar takes the other type
        if isinstance(b, TypeVar):
            if b.bound:
                return self._unify_types(a, b.bound)
            return a  # Unbound TypeVar takes the other type

        # Both are concrete - check compatibility
        if a.equals(b):
            return a
        if a.is_subtype_of(b):
            return b
        if b.is_subtype_of(a):
            return a

        # Incompatible - return first (caller may warn)
        return a

    # ========================================================================
    # Module-Level Checking
    # ========================================================================

    def check_module(self, ast: List[SExpr]) -> List[TypeDiagnostic]:
        """Type check an entire module"""
        # Flatten module forms - extract contents from (module name ...)
        forms = []
        for form in ast:
            if is_form(form, 'module'):
                # Add all forms inside the module (skip name at index 1)
                forms.extend(form.items[2:])
            else:
                forms.append(form)

        # First pass: collect type definitions
        for form in forms:
            if is_form(form, 'type'):
                self._register_type_def(form)

        # Second pass: collect constants
        for form in forms:
            if is_form(form, 'const'):
                self._register_const(form)

        # Third pass: collect function signatures (including FFI)
        for form in forms:
            if is_form(form, 'fn'):
                self._register_function_sig(form)
            elif is_form(form, 'impl'):
                self._register_function_sig(form)
            elif is_form(form, 'ffi'):
                self._register_ffi_funcs(form)

        # Fourth pass: check function bodies
        for form in forms:
            if is_form(form, 'fn'):
                self._check_function(form)
            elif is_form(form, 'impl'):
                self._check_function(form)

        return self.diagnostics

    def _register_type_def(self, form: SList):
        """Register a type definition"""
        if len(form) < 3:
            return

        name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
        type_expr = form[2]

        if is_form(type_expr, 'record'):
            fields: Dict[str, Type] = {}
            for field in type_expr.items[1:]:
                if isinstance(field, SList) and len(field) >= 2:
                    field_name = field[0].name if isinstance(field[0], Symbol) else str(field[0])
                    field_type = self.parse_type_expr(field[1])
                    fields[field_name] = field_type
            self.env.register_type(name, RecordType(name, fields))

        elif is_form(type_expr, 'enum'):
            variants = []
            for v in type_expr.items[1:]:
                if isinstance(v, Symbol):
                    variants.append(v.name)
            self.env.register_type(name, EnumType(name, variants))

        elif is_form(type_expr, 'union'):
            variants: Dict[str, Optional[Type]] = {}
            for v in type_expr.items[1:]:
                if isinstance(v, SList) and len(v) >= 1:
                    tag = v[0].name if isinstance(v[0], Symbol) else str(v[0])
                    payload = self.parse_type_expr(v[1]) if len(v) > 1 else None
                    variants[tag] = payload
                elif isinstance(v, Symbol):
                    variants[v.name] = None
            self.env.register_type(name, UnionType(name, variants))

        else:
            # Type alias or range type
            parsed = self.parse_type_expr(type_expr)
            self.env.register_type(name, parsed)

    def _register_function_sig(self, form: SList):
        """Register function signature from @spec"""
        if len(form) < 2:
            return

        name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
        params = form[2] if len(form) > 2 and isinstance(form[2], SList) else SList([])

        # Extract parameter types (mode-aware)
        param_types: List[Type] = []
        for param in params:
            if isinstance(param, SList) and len(param) >= 2:
                mode, pname, ptype_expr = self._parse_parameter_mode(param)
                if ptype_expr:
                    param_type = self.parse_type_expr(ptype_expr)
                    param_types.append(param_type)

        # Extract return type from @spec
        return_type: Type = PrimitiveType('Unit')
        for item in form.items[3:]:
            if is_form(item, '@spec') and len(item) > 1:
                spec = item[1]
                if isinstance(spec, SList):
                    # Find -> and get return type
                    for i, s in enumerate(spec.items):
                        if isinstance(s, Symbol) and s.name == '->':
                            if i + 1 < len(spec):
                                return_type = self.parse_type_expr(spec[i + 1])
                            break

        fn_type = FnType(tuple(param_types), return_type)
        self.env.register_function(name, fn_type)

    def _register_ffi_funcs(self, form: SList):
        """Register FFI function signatures.

        FFI form: (ffi "header.h" (func-name ((param Type)...) ReturnType) ...)
        """
        if len(form) < 2:
            return

        # Skip header string, process function declarations
        for item in form.items[2:]:
            if isinstance(item, SList) and len(item) >= 3:
                # (func-name ((param Type)...) ReturnType)
                fn_name = item[0].name if isinstance(item[0], Symbol) else str(item[0])
                params_list = item[1] if isinstance(item[1], SList) else SList([])
                return_type_expr = item[2]

                # Parse parameter types
                param_types: List[Type] = []
                for param in params_list:
                    if isinstance(param, SList) and len(param) >= 2:
                        param_type = self.parse_type_expr(param[1])
                        param_types.append(param_type)

                # Parse return type
                return_type = self.parse_type_expr(return_type_expr)

                fn_type = FnType(tuple(param_types), return_type)
                self.env.register_function(fn_name, fn_type)

    def _register_const(self, form: SList):
        """Register a constant as a bound variable.

        Const form: (const NAME Type value)
        """
        if len(form) < 3:
            return
        name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
        const_type = self.parse_type_expr(form[2])
        self.env.bind_var(name, const_type)

    def _check_function(self, form: SList):
        """Type check function body"""
        if len(form) < 2:
            return

        name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
        params = form[2] if len(form) > 2 and isinstance(form[2], SList) else SList([])

        fn_type = self.env.lookup_function(name)
        if not fn_type:
            return

        self.env.push_scope()

        # Clear parameter mode tracking for this function
        self._param_modes = {}
        self._out_initialized = set()

        # Bind parameters with mode tracking
        param_types_iter = iter(fn_type.param_types)
        for param in params:
            if isinstance(param, SList) and len(param) >= 1:
                mode, param_name, _ = self._parse_parameter_mode(param)
                if param_name:
                    try:
                        param_type = next(param_types_iter)
                    except StopIteration:
                        param_type = UNKNOWN
                    self.env.bind_var(param_name, param_type)
                    # Track parameter mode
                    self._param_modes[param_name] = mode

        # Check body expressions
        body_exprs = [item for item in form.items[3:]
                     if not (is_form(item, '@intent') or is_form(item, '@spec') or
                            is_form(item, '@pre') or is_form(item, '@post') or
                            is_form(item, '@alloc') or is_form(item, '@pure'))]

        last_type: Type = PrimitiveType('Unit')
        for expr in body_exprs:
            last_type = self.infer_expr(expr)

        # Check return type
        if not isinstance(fn_type.return_type, UnknownType) and body_exprs:
            self._check_assignable(last_type, fn_type.return_type,
                                  f"return value of '{name}'", body_exprs[-1])

        self.env.pop_scope()


# ============================================================================
# Public API
# ============================================================================

def parse_type_expr(expr: SExpr, type_registry: Optional[Dict[str, 'Type']] = None) -> Type:
    """Parse an AST type expression into a Type object.

    This is a convenience function for external callers (like hole_filler)
    that need to parse type expressions without a full TypeChecker context.

    Args:
        expr: The type expression AST (Symbol or SList)
        type_registry: Optional dict of known type names -> Type objects.
                      If not provided, only built-in types are recognized.

    Returns:
        The parsed Type object

    Examples:
        parse_type_expr(Symbol('Int')) -> PrimitiveType('Int')
        parse_type_expr(SList([Symbol('Ptr'), Symbol('User')])) -> PtrType(...)
        parse_type_expr(SList([Symbol('Option'), Symbol('Int')])) -> OptionType(...)
    """
    # Create minimal checker just for type parsing
    checker = TypeChecker()
    if type_registry:
        checker.env.type_registry.update(type_registry)
    return checker.parse_type_expr(expr)


def check_file(path: str) -> List[TypeDiagnostic]:
    """Type check a SLOP file.

    If the file has imports, performs full module resolution to get
    imported function signatures for accurate type checking.
    """
    from slop.parser import parse_file, is_form
    from pathlib import Path

    ast = parse_file(path)

    # Check if file has imports
    has_imports = any(
        is_form(f, 'import') or
        (is_form(f, 'module') and any(is_form(i, 'import') for i in f.items))
        for f in ast
    )

    if has_imports:
        # Use full module resolution to get imported signatures
        from slop.resolver import ModuleResolver
        resolver = ModuleResolver()
        try:
            graph = resolver.build_dependency_graph(Path(path))
            order = resolver.topological_sort(graph)
            results = check_modules(graph.modules, order)
            # Return diagnostics for the requested file's module
            # Module name may differ from filename, so find by path
            for mod_name, mod_info in graph.modules.items():
                if mod_info.path == Path(path).resolve():
                    return results.get(mod_name, [])
            # Fallback to filename stem
            return results.get(Path(path).stem, [])
        except Exception:
            # Fall back to single-file check if resolution fails
            checker = TypeChecker(path)
            return checker.check_module(ast)
    else:
        # Simple single-file check
        checker = TypeChecker(path)
        return checker.check_module(ast)


def check_source(source: str, filename: str = "<string>") -> List[TypeDiagnostic]:
    """Type check SLOP source code"""
    from slop.parser import parse
    ast = parse(source)
    checker = TypeChecker(filename)
    return checker.check_module(ast)


def check_modules(modules: dict, order: list) -> Dict[str, List[TypeDiagnostic]]:
    """Type check multiple modules in dependency order.

    Args:
        modules: Dict mapping module name to ModuleInfo (from resolver)
        order: List of module names in topological order (dependencies first)

    Returns:
        Dict mapping module_name to list of diagnostics
    """
    from slop.parser import is_form, get_imports, parse_import

    results: Dict[str, List[TypeDiagnostic]] = {}
    # Track exported function signatures for cross-module checking
    exported_sigs: Dict[str, Dict[str, FnType]] = {}  # module_name -> {fn_name -> sig}
    exported_types: Dict[str, Dict[str, Type]] = {}  # module_name -> {type_name -> type}

    for mod_name in order:
        info = modules[mod_name]
        checker = TypeChecker(str(info.path))

        # Register imported functions from already-checked modules
        for imp in info.imports:
            source_mod = imp.module_name
            if source_mod in exported_sigs:
                for fn_name, _ in imp.symbols:
                    if fn_name in exported_sigs[source_mod]:
                        checker.env.register_import(fn_name, exported_sigs[source_mod][fn_name])

            # Register imported types
            if source_mod in exported_types:
                for type_name, typ in exported_types[source_mod].items():
                    checker.env.register_imported_type(type_name, typ)

        # Type check this module
        diagnostics = checker.check_module(info.ast)
        results[mod_name] = diagnostics

        # Collect exported signatures for dependent modules
        exported_sigs[mod_name] = {}
        exported_types[mod_name] = {}

        for fn_name, arity in info.exports.items():
            if fn_name in checker.env.function_sigs:
                exported_sigs[mod_name][fn_name] = checker.env.function_sigs[fn_name]

        # Export all types defined in this module
        for type_name, typ in checker.env.type_registry.items():
            if type_name not in checker.env.imported_types:
                exported_types[mod_name][type_name] = typ

    return results
