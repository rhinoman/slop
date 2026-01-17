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
    ChanType, ThreadType,
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
        self.deprecated_functions: Dict[str, str] = {}  # fn name -> deprecation message
        self._init_builtins()

    def _init_builtins(self):
        """Initialize built-in types"""
        # Scalar primitives
        for name in ['Int', 'Float', 'Bool', 'Unit', 'Void']:
            self.type_registry[name] = PrimitiveType(name)

        # String and Bytes are C structs with fields - register as RecordType
        # This enables constructor syntax (String data len) and proper field access
        # Use U64 for len/cap to match C's size_t
        self.type_registry['String'] = RecordType('String', {
            'data': PtrType(PrimitiveType('U8')),
            'len': PrimitiveType('U64'),
        })
        self.type_registry['Bytes'] = RecordType('Bytes', {
            'data': PtrType(PrimitiveType('U8')),
            'len': PrimitiveType('U64'),
            'cap': PrimitiveType('U64'),
        })

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

    def register_deprecated(self, name: str, message: str):
        """Mark a function as deprecated with a message."""
        self.deprecated_functions[name] = message

    def get_deprecation_message(self, name: str) -> Optional[str]:
        """Get deprecation message for a function, or None if not deprecated."""
        return self.deprecated_functions.get(name)

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

    def find_enum_variant_collisions(self) -> Dict[str, List[str]]:
        """Find enum variants that appear in multiple enum types.

        Returns:
            Dict mapping variant_name -> [enum_type_1, enum_type_2, ...]
            for variants that appear in more than one enum.
        """
        variant_to_enums: Dict[str, List[str]] = {}
        for type_name, typ in self.type_registry.items():
            if isinstance(typ, EnumType):
                for variant in typ.variants:
                    if variant not in variant_to_enums:
                        variant_to_enums[variant] = []
                    variant_to_enums[variant].append(type_name)

        # Filter to only collisions
        return {v: enums for v, enums in variant_to_enums.items() if len(enums) > 1}


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
        # Track which variables hold mutable collections (from list-new, map-new)
        # Immutable literals (list, map) are not added here
        self._mutable_collections: Set[str] = set()
        # Track which variables are declared as mutable (with 'mut' keyword in let)
        self._mutable_vars: Set[str] = set()
        # Track current function's return type (for ? operator validation)
        self._current_fn_return_type: Optional[Type] = None
        # Register built-in functions
        self._register_builtins()

    def fresh_type_var(self, name: Optional[str] = None) -> TypeVar:
        self._type_var_counter += 1
        return TypeVar(self._type_var_counter, name)

    def _register_builtins(self):
        """Register built-in function signatures."""
        # Common types - use registered String type for consistency
        STRING = self.env.lookup_type('String') or PrimitiveType('String')
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
        self.env.register_function('string-split', FnType((ARENA, STRING, STRING), ListType(STRING)))
        self.env.register_function('int-to-string', FnType((ARENA, INT), STRING))

        # Arena/memory operations
        # arena-alloc returns a polymorphic pointer; use UNKNOWN so it unifies with any Ptr type
        self.env.register_function('arena-new', FnType((INT,), ARENA))
        self.env.register_function('arena-alloc', FnType((ARENA, INT), PtrType(UNKNOWN)))
        self.env.register_function('arena-free', FnType((ARENA,), UNIT))

        # List operations - return types use UNKNOWN for polymorphism
        self.env.register_function('list-new', FnType((ARENA,), ListType(UNKNOWN)))
        self.env.register_function('list-push', FnType((ListType(UNKNOWN), UNKNOWN), UNIT))
        self.env.register_function('list-pop', FnType((ListType(UNKNOWN),), OptionType(UNKNOWN)))
        self.env.register_function('list-get', FnType((ListType(UNKNOWN), INT), OptionType(UNKNOWN)))
        self.env.register_function('list-len', FnType((ListType(UNKNOWN),), INT))

        # Map operations - use UNKNOWN for polymorphism
        self.env.register_function('map-new', FnType((ARENA,), MapType(UNKNOWN, UNKNOWN)))
        self.env.register_function('map-put', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN, UNKNOWN), UNIT))
        self.env.register_function('map-get', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN), OptionType(UNKNOWN)))
        self.env.register_function('map-has', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN), BOOL))
        self.env.register_function('map-keys', FnType((MapType(UNKNOWN, UNKNOWN),), ListType(UNKNOWN)))
        self.env.register_function('map-remove', FnType((MapType(UNKNOWN, UNKNOWN), UNKNOWN), UNIT))

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

    def _check_enum_variant_collisions(self):
        """Check for enum variants that appear in multiple enum types and report errors."""
        collisions = self.env.find_enum_variant_collisions()
        for variant, enum_names in collisions.items():
            self.error(
                f"Ambiguous enum variant '{variant}' exists in multiple types: {', '.join(sorted(enum_names))}",
                hint="Rename variants to be unique across enum types, e.g., 'api-not-found' vs 'http-not-found'"
            )

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

    def _refine_type_with_bounds(self, typ: Type, bounds: RangeBounds) -> Type:
        """Apply bounds directly to refine a type's range."""
        if isinstance(typ, RangeType):
            new_bounds = typ.bounds.intersect(bounds)
            return RangeType(typ.base, new_bounds)

        if isinstance(typ, PrimitiveType):
            if typ.name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64'):
                return RangeType(typ.name, bounds)

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

                if name == 'Chan':
                    element_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return ChanType(element_type)

                if name == 'Thread':
                    result_type = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return ThreadType(result_type)

                if name == 'Ptr':
                    pointee = self.parse_type_expr(expr[1]) if len(expr) > 1 else UNKNOWN
                    return PtrType(pointee)

                if name == 'ScopedPtr':
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

    def _set_type(self, expr: SExpr, typ: Type) -> Type:
        """Annotate expression with resolved type and return it.

        This allows the transpiler to read types directly from AST nodes
        instead of re-inferring them.
        """
        expr.resolved_type = typ
        return typ

    def infer_expr(self, expr: SExpr) -> Type:
        """Infer type of expression and annotate the AST node."""
        if isinstance(expr, Number):
            if isinstance(expr.value, float) or '.' in str(expr.value):
                return self._set_type(expr, PrimitiveType('Float'))
            val = int(expr.value)
            return self._set_type(expr, RangeType('Int', RangeBounds(val, val)))

        if isinstance(expr, String):
            # Return registered String type (RecordType) for proper field access
            typ = self.env.lookup_type('String') or PrimitiveType('String')
            return self._set_type(expr, typ)

        if isinstance(expr, Symbol):
            typ = self._infer_symbol(expr)
            return self._set_type(expr, typ)

        if isinstance(expr, SList):
            if len(expr) == 0:
                # Empty list () is Unit
                return self._set_type(expr, PrimitiveType('Unit'))
            typ = self._infer_compound(expr)
            return self._set_type(expr, typ)

        return self._set_type(expr, UNKNOWN)

    def _infer_symbol(self, sym: Symbol) -> Type:
        """Infer type of symbol reference"""
        name = sym.name

        # Handle dotted field access: foo.bar.baz
        # or enum value access: EnumName.variant
        if '.' in name:
            parts = name.split('.')
            # Check if base is an enum type - if so, this is enum value access
            base_enum_type = self.env.lookup_type(parts[0])
            if isinstance(base_enum_type, EnumType):
                # Enum value access: EnumName.variant
                variant = parts[1]
                if base_enum_type.has_variant(variant):
                    return base_enum_type
                else:
                    self.error(f"Enum '{parts[0]}' has no variant '{variant}'", sym)
                    return UNKNOWN
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
        if name == 'none':
            # Bare 'none' is Option type with unknown inner type
            return OptionType(self.fresh_type_var('T'))
        if name == 'unit':
            # Unit value for void results
            return PrimitiveType('Unit')
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

        # Bitwise operators - return Int
        if op in ('&', '|', '^', '<<', '>>'):
            # Type check operands as Int
            for arg in expr.items[1:]:
                arg_type = self.infer_expr(arg)
                self._expect_type(arg_type, PrimitiveType('Int'), f"bitwise {op} operand", arg)
            return PrimitiveType('Int')

        # Comparisons (= is an alias for ==)
        if op in ('==', '!=', '<', '<=', '>', '>=', '='):
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

        # When expression (if without else, returns Unit)
        if op == 'when':
            if len(expr) >= 2:
                cond_type = self.infer_expr(expr[1])
                self._expect_type(cond_type, PrimitiveType('Bool'), "when condition", expr[1])
            if len(expr) >= 3:
                self.infer_expr(expr[2])  # type check body but ignore result
            return UNIT

        # Cond expression (multi-way conditional) with path-sensitive refinement
        if op == 'cond':
            # (cond (test1 body1) (test2 body2) ... (else bodyN))
            # Each clause's body sees: its condition is true AND all previous conditions were false
            result_types: List[Type] = []
            accumulated_negations: Dict[str, RangeBounds] = {}  # var -> accumulated bounds

            for clause in expr.items[1:]:
                if not isinstance(clause, SList) or len(clause) < 2:
                    continue

                test = clause[0]
                body = clause[1]

                if isinstance(test, Symbol) and test.name == 'else':
                    # Else clause: apply all accumulated negations
                    refinements: Dict[str, Type] = {}
                    for var, bounds in accumulated_negations.items():
                        var_type = self._lookup_var_for_refinement(var)
                        if var_type:
                            refined = self._refine_type_with_bounds(var_type, bounds)
                            refinements[var] = refined

                    self._push_refinements(refinements)
                    result_types.append(self.infer_expr(body))
                    self._pop_refinements()
                else:
                    # Regular clause: type check condition
                    test_type = self.infer_expr(test)
                    self._expect_type(test_type, PrimitiveType('Bool'), "cond test", test)

                    # Extract constraint for path-sensitive refinement
                    constraint = self._extract_constraint(test)

                    refinements = {}
                    if constraint:
                        var_type = self._lookup_var_for_refinement(constraint.var)
                        if var_type:
                            # Start with accumulated bounds for this var (if any)
                            if constraint.var in accumulated_negations:
                                base_bounds = accumulated_negations[constraint.var]
                                var_type = self._refine_type_with_bounds(var_type, base_bounds)

                            # Apply this clause's condition
                            refined = self._refine_type_with_constraint(var_type, constraint)
                            refinements[constraint.var] = refined

                            # Update accumulated negations for subsequent clauses
                            negated = constraint.negate()
                            neg_bounds = negated.to_bounds()
                            if constraint.var in accumulated_negations:
                                accumulated_negations[constraint.var] = \
                                    accumulated_negations[constraint.var].intersect(neg_bounds)
                            else:
                                accumulated_negations[constraint.var] = neg_bounds

                    self._push_refinements(refinements)
                    result_types.append(self.infer_expr(body))
                    self._pop_refinements()

            # Unify all branch types
            if not result_types:
                return UNIT
            result = result_types[0]
            for t in result_types[1:]:
                result = self._unify_branch_types(result, t, expr)
            return result

        # Match expression
        if op == 'match':
            return self._infer_match(expr)

        # Let bindings
        if op in ('let', 'let*'):
            return self._infer_let(expr)

        # Do block
        if op == 'do':
            return self._infer_do(expr)

        # Control flow - break, continue, return
        if op == 'break':
            return UNIT
        if op == 'continue':
            return UNIT
        if op == 'return':
            if len(expr) >= 2:
                return self.infer_expr(expr[1])
            return UNIT

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

        # Try operator: (? expr) - early return on error
        if op == '?':
            if len(expr) < 2:
                self.error("? operator requires an expression", expr)
                return UNKNOWN
            inner_type = self.infer_expr(expr[1])
            # Check if we're in a void-returning function
            if self._current_fn_return_type is not None:
                if (isinstance(self._current_fn_return_type, PrimitiveType) and
                    self._current_fn_return_type.name in ('Unit', 'Void')):
                    self.error(
                        "Cannot use ? operator in function returning Unit",
                        expr,
                        hint="The ? operator performs early return on error, which requires the function to return a Result type. "
                             "Change the function's return type to (Result T E) or handle errors explicitly with match."
                    )
            # Return the ok type from the Result
            if isinstance(inner_type, ResultType):
                return inner_type.ok_type
            return UNKNOWN

        # List and Map constructors with type parameters
        if op == 'list-new':
            # (list-new arena Type) -> (List Type)
            if len(expr) >= 3:
                element_type = self.parse_type_expr(expr[2])
                return ListType(element_type)
            return ListType(UNKNOWN)

        if op == 'map-new':
            # (map-new arena KeyType ValueType) -> (Map KeyType ValueType)
            if len(expr) >= 4:
                key_type = self.parse_type_expr(expr[2])
                value_type = self.parse_type_expr(expr[3])
                return MapType(key_type, value_type)
            return MapType(UNKNOWN, UNKNOWN)

        # Map literal: (map [KeyType ValueType] (k1 v1) (k2 v2) ...)
        if op == 'map':
            return self._infer_map_literal(expr)

        # List literal: (list [Type] e1 e2 ...)
        if op == 'list':
            return self._infer_list_literal(expr)

        # Array literal: (array e1 e2 ...)
        if op == 'array':
            if len(expr) >= 2:
                elem_types = [self.infer_expr(e) for e in expr.items[1:]]
                if elem_types:
                    return ArrayType(elem_types[0], len(elem_types))
            return UNKNOWN

        # Union construction: (union-new Type Tag value)
        if op == 'union-new':
            if len(expr) >= 2:
                type_expr = expr[1]
                type_name = type_expr.name if isinstance(type_expr, Symbol) else str(type_expr)
                union_type = self.env.lookup_type(type_name)
                if union_type:
                    return union_type
            return UNKNOWN

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

        # Sizeof - returns Int
        if op == 'sizeof':
            # (sizeof Type) - just validate type exists, return Int
            return PrimitiveType('Int')

        # Address-of - returns pointer to type
        if op == 'addr':
            if len(expr) >= 2:
                inner_type = self.infer_expr(expr[1])
                return PtrType(inner_type)
            return UNKNOWN

        # Dereference - unwrap pointer
        if op == 'deref':
            if len(expr) >= 2:
                ptr_type = self.infer_expr(expr[1])
                if isinstance(ptr_type, PtrType):
                    return ptr_type.pointee
            return UNKNOWN

        # Record-new - create record with field values
        if op == 'record-new':
            # (record-new Type (field1 val1) (field2 val2) ...)
            if len(expr) >= 2:
                type_expr = expr[1]
                # Handle inline record type: (record-new (record (x Int) (y Int)) ...)
                if is_form(type_expr, 'record'):
                    record_type = self.parse_type_expr(type_expr)
                    # Type check field values
                    if isinstance(record_type, RecordType):
                        for field_init in expr.items[2:]:
                            if isinstance(field_init, SList) and len(field_init) >= 2:
                                field_name = field_init[0].name if isinstance(field_init[0], Symbol) else str(field_init[0])
                                field_val = field_init[1]
                                expected_type = record_type.fields.get(field_name)
                                if expected_type:
                                    actual_type = self.infer_expr(field_val)
                                    self._check_assignable(actual_type, expected_type, f"field '{field_name}'", field_val)
                    return record_type
                else:
                    # Named record type: (record-new Point (x 1) (y 2))
                    type_name = type_expr.name if isinstance(type_expr, Symbol) else str(type_expr)
                    record_type = self.env.lookup_type(type_name)
                    if record_type and isinstance(record_type, RecordType):
                        # Type check field values
                        for field_init in expr.items[2:]:
                            if isinstance(field_init, SList) and len(field_init) >= 2:
                                field_name = field_init[0].name if isinstance(field_init[0], Symbol) else str(field_init[0])
                                field_val = field_init[1]
                                expected_type = record_type.fields.get(field_name)
                                if expected_type:
                                    actual_type = self.infer_expr(field_val)
                                    self._check_assignable(actual_type, expected_type, f"field '{field_name}'", field_val)
                        return record_type
            return UNKNOWN

        # With-arena
        if op == 'with-arena':
            return self._infer_with_arena(expr)

        # Loop constructs
        if op == 'for':
            return self._infer_for(expr)
        if op == 'for-each':
            return self._infer_for_each(expr)
        if op == 'while':
            return self._infer_while(expr)

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

    def _infer_map_literal(self, expr: SList, expected_type: Optional[Type] = None) -> Type:
        """Infer type of map literal: (map [KeyType ValueType] (k1 v1) (k2 v2) ...)

        Type can be:
        1. Explicit: (map String Int ("a" 1)) - first two args are types
        2. Inferred from context: expected_type from binding/param
        3. Inferred from first pair: (map ("a" 1))
        4. Empty with no context: use type variables
        """
        items = expr.items[1:]  # Skip 'map' keyword

        # Check if first two items are explicit types
        explicit_key_type = None
        explicit_value_type = None
        pairs_start = 0

        if len(items) >= 2:
            first = items[0]
            second = items[1]
            # Both must be symbols that look like types
            if (isinstance(first, Symbol) and isinstance(second, Symbol)):
                first_name = first.name
                second_name = second.name
                first_is_type = first_name in self.env.type_registry or (first_name[0].isupper() and first_name not in ['nil', 'true', 'false'])
                second_is_type = second_name in self.env.type_registry or (second_name[0].isupper() and second_name not in ['nil', 'true', 'false'])
                if first_is_type and second_is_type:
                    explicit_key_type = self.parse_type_expr(first)
                    explicit_value_type = self.parse_type_expr(second)
                    pairs_start = 2

        pairs = items[pairs_start:]

        # Determine key/value types
        if explicit_key_type and explicit_value_type:
            key_type = explicit_key_type
            value_type = explicit_value_type
        elif expected_type and isinstance(expected_type, MapType):
            key_type = expected_type.key_type
            value_type = expected_type.value_type
        elif pairs:
            # Infer from first pair, but widen types for flexibility
            first = pairs[0]
            if not isinstance(first, SList) or len(first) < 2:
                self.error("Map literal requires (key value) pairs", first if isinstance(first, SList) else expr)
                return MapType(UNKNOWN, UNKNOWN)
            key_type = self._widen_type(self.infer_expr(first[0]))
            value_type = self._widen_type(self.infer_expr(first[1]))
        else:
            # Empty map with no explicit types - use type variables
            return MapType(self.fresh_type_var('K'), self.fresh_type_var('V'))

        # Validate all pairs match the types
        for pair in pairs:
            if not isinstance(pair, SList) or len(pair) < 2:
                self.error("Map literal requires (key value) pairs", pair if isinstance(pair, SList) else expr)
                continue

            k_type = self.infer_expr(pair[0])
            v_type = self.infer_expr(pair[1])

            if not isinstance(k_type, UnknownType):
                self._check_assignable(k_type, key_type, "map key", pair[0])
            if not isinstance(v_type, UnknownType):
                self._check_assignable(v_type, value_type, "map value", pair[1])

        return MapType(key_type, value_type)

    def _infer_list_literal(self, expr: SList, expected_type: Optional[Type] = None) -> Type:
        """Infer type of list literal: (list [Type] e1 e2 ...)

        Type can be:
        1. Explicit: (list Int 1 2 3) - first arg is a type
        2. Inferred from context: expected_type from binding/param
        3. Inferred from first element: (list 1 2 3)
        4. Empty with no context: error
        """
        items = expr.items[1:]  # Skip 'list' keyword

        # Check if first item is an explicit type
        explicit_type = None
        elements_start = 0

        if items and isinstance(items[0], Symbol):
            type_name = items[0].name
            # Check if it's a known type (in registry or starts with uppercase)
            if type_name in self.env.type_registry or (type_name[0].isupper() and type_name not in ['nil', 'true', 'false']):
                explicit_type = self.parse_type_expr(items[0])
                elements_start = 1

        elements = items[elements_start:]

        # Determine element type
        if explicit_type:
            element_type = explicit_type
        elif expected_type and isinstance(expected_type, ListType):
            element_type = expected_type.element_type
        elif elements:
            # Infer from first element, but widen to base type for flexibility
            inferred = self.infer_expr(elements[0])
            element_type = self._widen_type(inferred)
        else:
            # Empty list with no context
            self.error("Empty list literal requires explicit type: (list Type) or context from binding", expr)
            return ListType(UNKNOWN)

        # Validate all elements match the element type
        for i, elem in enumerate(elements):
            elem_type = self.infer_expr(elem)
            if not isinstance(elem_type, UnknownType):
                self._check_assignable(elem_type, element_type, f"list element at index {i + elements_start}", elem)

        return ListType(element_type)

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

        # Check exhaustiveness before processing branches
        self._check_match_exhaustiveness(expr, scrutinee_type)

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
                else:
                    # Scrutinee type not fully known (e.g., imported union type)
                    # Still bind pattern variables to UNKNOWN to avoid spurious errors
                    if len(pattern) > 1:
                        for var in pattern.items[1:]:
                            if isinstance(var, Symbol) and var.name != '_':
                                self.env.bind_var(var.name, UNKNOWN)

    def _get_required_variants(self, scrutinee_type: Type) -> Optional[Set[str]]:
        """Return the set of variant names that must be covered for exhaustiveness.

        Returns None if the type doesn't require exhaustiveness checking.
        """
        if isinstance(scrutinee_type, EnumType):
            return set(scrutinee_type.variants)
        elif isinstance(scrutinee_type, UnionType):
            return set(scrutinee_type.variants.keys())
        elif isinstance(scrutinee_type, OptionType):
            return {'some', 'none'}
        elif isinstance(scrutinee_type, ResultType):
            return {'ok', 'error'}
        return None

    def _extract_pattern_variant(self, pattern: SExpr, scrutinee_type: Type) -> Optional[str]:
        """Extract the variant name from a match pattern.

        Returns:
            - The variant name (e.g., 'ok', 'some', 'number')
            - '_' for wildcard patterns
            - None if pattern is not recognized as a variant
        """
        if isinstance(pattern, Symbol):
            name = pattern.name
            # Wildcard patterns
            if name == '_' or name == 'else':
                return '_'
            # Quoted symbol for enum values: 'ok -> ok
            if name.startswith("'"):
                return name[1:]
            # Bare symbol: check if it's an enum variant or Option's 'none'
            if isinstance(scrutinee_type, EnumType):
                if scrutinee_type.has_variant(name):
                    return name
            if isinstance(scrutinee_type, OptionType) and name == 'none':
                return 'none'
            # Bare symbol is a binding variable, not a variant
            return None

        elif isinstance(pattern, SList) and len(pattern) >= 1:
            # List pattern: (tag var) or (tag)
            if isinstance(pattern[0], Symbol):
                tag = pattern[0].name
                # Remove quote if present
                if tag.startswith("'"):
                    tag = tag[1:]
                return tag

        return None

    def _type_name_for_error(self, t: Type) -> str:
        """Get a human-readable type name for error messages."""
        if isinstance(t, EnumType):
            return f"enum '{t.name}'" if t.name != '<anonymous>' else "enum"
        elif isinstance(t, UnionType):
            return f"union '{t.name}'" if t.name != '<anonymous>' else "union"
        elif isinstance(t, OptionType):
            return f"(Option {t.inner})"
        elif isinstance(t, ResultType):
            return f"(Result {t.ok_type} {t.err_type})"
        return str(t)

    def _check_match_exhaustiveness(self, expr: SList, scrutinee_type: Type):
        """Check that a match expression covers all variants of the scrutinee type."""
        required = self._get_required_variants(scrutinee_type)
        if required is None:
            return  # Not a type that requires exhaustiveness checking

        covered: Set[str] = set()
        has_wildcard = False

        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                variant = self._extract_pattern_variant(pattern, scrutinee_type)

                if variant == '_':
                    has_wildcard = True
                elif variant is not None:
                    covered.add(variant)

        if has_wildcard:
            return  # Exhaustive via wildcard

        missing = required - covered
        if missing:
            missing_list = ', '.join(sorted(missing))
            type_name = self._type_name_for_error(scrutinee_type)
            self.error(
                f"Non-exhaustive match on {type_name}: missing variants {missing_list}",
                expr,
                hint=f"Add patterns for {missing_list} or use '_' / 'else' as a catch-all"
            )

    def _infer_let(self, expr: SList) -> Type:
        """Infer type of let expression.

        Syntax:
          (let ((x init)) body)           - immutable binding
          (let ((mut x init)) body)       - mutable binding (allows set!)
          (let ((mut x Type init)) body)  - mutable with explicit type
        """
        if len(expr) < 2:
            return UNKNOWN

        self.env.push_scope()
        # Track which mutable collections are bound in this scope
        scope_mutable_collections: Set[str] = set()
        # Track which mutable vars are bound in this scope (for cleanup)
        scope_mutable_vars: Set[str] = set()

        bindings = expr[1] if isinstance(expr[1], SList) else SList([])
        for binding in bindings:
            if isinstance(binding, SList) and len(binding) >= 2:
                is_mut = False
                idx = 0

                # Check for mut keyword at start of binding
                if isinstance(binding[0], Symbol) and binding[0].name == 'mut':
                    is_mut = True
                    idx = 1

                if idx >= len(binding):
                    continue  # Malformed binding

                var_name = binding[idx].name if isinstance(binding[idx], Symbol) else str(binding[idx])

                # Check for explicit type annotation: (mut x Type init) or (x Type init)
                if len(binding) > idx + 2:
                    # Has explicit type annotation
                    type_expr = binding[idx + 1]
                    init_expr = binding[idx + 2]
                    var_type = self.parse_type_expr(type_expr)
                    # Type check the init expr against declared type
                    init_type = self.infer_expr(init_expr)
                    self._check_assignable(init_type, var_type, f"binding '{var_name}'", init_expr)
                else:
                    # No explicit type - infer from init
                    init_expr = binding[idx + 1]
                    var_type = self.infer_expr(init_expr)

                    # Widen singleton range types for mut variables
                    # This allows (let ((mut total 0)) (set! total (+ total 1)))
                    if is_mut and isinstance(var_type, RangeType):
                        # Widen to the base type (Int, I32, etc.)
                        var_type = PrimitiveType(var_type.base)

                self.env.bind_var(var_name, var_type)

                # Track mutable variables
                if is_mut:
                    self._mutable_vars.add(var_name)
                    scope_mutable_vars.add(var_name)

                # Track if this is a mutable collection (list-new or map-new)
                if self._is_mutable_collection_constructor(init_expr):
                    self._mutable_collections.add(var_name)
                    scope_mutable_collections.add(var_name)

        # Infer body type
        body_type = PrimitiveType('Unit')
        for body_expr in expr.items[2:]:
            body_type = self.infer_expr(body_expr)

        # Clean up mutable collection tracking for this scope
        for var_name in scope_mutable_collections:
            self._mutable_collections.discard(var_name)

        # Clean up mutable var tracking for this scope
        for var_name in scope_mutable_vars:
            self._mutable_vars.discard(var_name)

        self.env.pop_scope()
        return body_type

    def _is_mutable_collection_constructor(self, expr: SExpr) -> bool:
        """Check if expression creates a mutable collection (list-new or map-new)."""
        if isinstance(expr, SList) and len(expr) >= 1:
            head = expr[0]
            if isinstance(head, Symbol):
                return head.name in ('list-new', 'map-new')
        return False

    def _infer_for(self, expr: SList) -> Type:
        """Infer type of for loop: (for (var start end) body...)

        The loop variable is bound to a range type based on the bounds.
        """
        if len(expr) < 2:
            return UNIT

        binding = expr[1]
        if not isinstance(binding, SList) or len(binding) < 3:
            self.error("for loop requires (var start end) binding", expr)
            return UNIT

        var_name = binding[0].name if isinstance(binding[0], Symbol) else str(binding[0])
        start_type = self.infer_expr(binding[1])
        end_type = self.infer_expr(binding[2])

        # Validate start and end are numeric
        if not self._is_numeric_type(start_type):
            self.error(f"for loop start must be numeric, got {start_type}", binding[1])
        if not self._is_numeric_type(end_type):
            self.error(f"for loop end must be numeric, got {end_type}", binding[2])

        # Compute loop variable type from bounds
        # For (for (i 0 count) ...) where count is (Int 0 .. 100),
        # the loop variable i ranges from start to end-1
        loop_var_type = self._compute_for_loop_var_type(start_type, end_type)

        # Type-check body with loop variable bound
        self.env.push_scope()
        self.env.bind_var(var_name, loop_var_type)

        for body_expr in expr.items[2:]:
            self.infer_expr(body_expr)

        self.env.pop_scope()
        return UNIT

    def _compute_for_loop_var_type(self, start_type: Type, end_type: Type) -> Type:
        """Compute the type of a for loop variable from its bounds.

        The loop variable ranges from start to end-1 (exclusive upper bound).
        """
        # Get the minimum value from start
        start_min = None
        if isinstance(start_type, RangeType):
            start_min = start_type.bounds.min_val
        elif isinstance(start_type, PrimitiveType) and start_type.name == 'Int':
            start_min = None  # Unbounded

        # Get the maximum value from end (subtract 1 for exclusive bound)
        end_max = None
        if isinstance(end_type, RangeType):
            if end_type.bounds.max_val is not None:
                end_max = end_type.bounds.max_val - 1
            else:
                end_max = None  # Unbounded
        elif isinstance(end_type, PrimitiveType) and end_type.name == 'Int':
            end_max = None  # Unbounded

        # If we have concrete bounds, create a range type
        if start_min is not None or end_max is not None:
            return RangeType('Int', RangeBounds(start_min, end_max))

        # Fall back to unbounded Int
        return INT

    def _infer_for_each(self, expr: SList) -> Type:
        """Infer type of for-each loop: (for-each (var collection) body...)

        The loop variable is bound to the element type of the collection.
        """
        if len(expr) < 2:
            return UNIT

        binding = expr[1]
        if not isinstance(binding, SList) or len(binding) < 2:
            self.error("for-each loop requires (var collection) binding", expr)
            return UNIT

        var_name = binding[0].name if isinstance(binding[0], Symbol) else str(binding[0])
        collection_type = self.infer_expr(binding[1])

        # Determine element type from collection
        element_type = UNKNOWN
        if isinstance(collection_type, ListType):
            element_type = collection_type.element_type
        elif isinstance(collection_type, PrimitiveType) and collection_type.name == 'String':
            element_type = PrimitiveType('U8')
        else:
            self.error(f"for-each requires a List or String, got {collection_type}", binding[1])

        # Type-check body with loop variable bound
        self.env.push_scope()
        self.env.bind_var(var_name, element_type)

        for body_expr in expr.items[2:]:
            self.infer_expr(body_expr)

        self.env.pop_scope()
        return UNIT

    def _infer_while(self, expr: SList) -> Type:
        """Infer type of while loop: (while condition body...)

        Condition must be Bool.
        """
        if len(expr) < 2:
            return UNIT

        cond_type = self.infer_expr(expr[1])
        if not isinstance(cond_type, PrimitiveType) or cond_type.name != 'Bool':
            self.error(f"while condition must be Bool, got {cond_type}", expr[1])

        # Type-check body
        for body_expr in expr.items[2:]:
            self.infer_expr(body_expr)

        return UNIT

    def _is_numeric_type(self, t: Type) -> bool:
        """Check if a type is numeric (Int, Float, or range type)"""
        if isinstance(t, RangeType):
            return True
        if isinstance(t, PrimitiveType):
            return t.name in ('Int', 'Float', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64')
        return False

    def _infer_do(self, expr: SList) -> Type:
        """Infer type of do block - returns last expression's type"""
        result = PrimitiveType('Unit')
        for item in expr.items[1:]:
            result = self.infer_expr(item)
        return result

    def _infer_field_access(self, expr: SList) -> Type:
        """Infer type of field access: (. obj field)"""
        if len(expr) != 3:
            self.error(f"Field access requires 2 arguments (obj field), got {len(expr) - 1}", expr)
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
                    # Check mutability: must be either a mut let binding or a mutable parameter
                    if name in self._param_modes:
                        # Parameter - check mode constraints
                        mode = self._param_modes[name]
                        if mode == 'in':
                            self.error(f"Cannot assign to 'in' parameter '{name}'", target,
                                      hint="'in' parameters are read-only. Use 'mut' mode for read-write access.")
                        elif mode == 'out':
                            # Mark out parameter as initialized
                            self._out_initialized.add(name)
                        # 'mut' and 'out' modes allow assignment
                    elif name not in self._mutable_vars:
                        # Not a mutable parameter and not a mut let binding
                        self.error(f"Cannot assign to immutable variable '{name}'", target,
                                  hint="Declare with 'mut' to allow mutation: (let ((mut x 0)) ...)")

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
        """Infer type of hole expression and check for :context.

        (hole Type "prompt" :complexity tier-2 :context (fn1 fn2) :required (fn1))
        """
        if len(expr) < 2:
            self.error("Hole requires a type", expr)
            return UNKNOWN

        # Parse the type (first argument)
        hole_type = self.parse_type_expr(expr[1])

        # Check for :context - warn if missing
        has_context = False
        for i, item in enumerate(expr.items):
            if isinstance(item, Symbol) and item.name == ':context':
                has_context = True
                break

        if not has_context:
            self.warning("Hole is missing :context - add it for better LLM guidance", expr)

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

        # Special handling for arena-alloc with type argument
        # (arena-alloc arena TypeName) returns (Ptr TypeName)
        if fn_name == 'arena-alloc' and len(args) == 2:
            arena_arg = args[0]
            size_arg = args[1]
            # Check arena argument
            arena_type = self.infer_expr(arena_arg)
            self._check_assignable(arena_type, ARENA, "argument 1 to 'arena-alloc'", arena_arg)
            # Check if second arg is a type name
            if isinstance(size_arg, Symbol):
                type_obj = self.env.lookup_type(size_arg.name)
                if type_obj is not None:
                    # Return pointer to that type
                    return PtrType(type_obj)
            # Otherwise infer as integer size
            size_type = self.infer_expr(size_arg)
            self._check_assignable(size_type, INT, "argument 2 to 'arena-alloc'", size_arg)
            return PtrType(UNKNOWN)

        # Special handling for make-scoped
        # (make-scoped Type) or (make-scoped Type count) -> (ScopedPtr Type)
        if fn_name == 'make-scoped' and len(args) >= 1:
            type_arg = args[0]
            # First arg should be a type name
            if isinstance(type_arg, Symbol):
                pointee_type = self.env.lookup_type(type_arg.name)
                if pointee_type is None:
                    pointee_type = self.parse_type_expr(type_arg)
            else:
                pointee_type = self.parse_type_expr(type_arg)

            # If count arg provided, validate it's an integer
            if len(args) >= 2:
                count_arg = args[1]
                count_type = self.infer_expr(count_arg)
                # Check if it's a numeric type
                if not isinstance(count_type, UnknownType):
                    is_numeric = (isinstance(count_type, RangeType) or
                                  (isinstance(count_type, PrimitiveType) and
                                   count_type.name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64')))
                    if not is_numeric:
                        self.error(f"make-scoped count must be integer, got {count_type}", count_arg)

            # Return owning pointer type (ScopedPtr)
            return PtrType(pointee_type, owning=True)

        # Special handling for list-push: requires mutable list
        # (list-push list item)
        if fn_name == 'list-push' and len(args) >= 2:
            list_arg = args[0]
            item_arg = args[1]
            list_type = self.infer_expr(list_arg)
            self.infer_expr(item_arg)

            # Check mutability - list literals are immutable
            # But pointers to lists (Ptr List) are always mutable through the pointer
            if isinstance(list_arg, Symbol):
                is_ptr_type = isinstance(list_type, PtrType)
                if not is_ptr_type and list_arg.name not in self._mutable_collections:
                    self.error(
                        f"cannot push to immutable list '{list_arg.name}'",
                        list_arg,
                        hint="Use (list-new arena Type) to create a mutable list"
                    )
            return UNIT

        # Special handling for list-pop: requires mutable list
        # (list-pop list) -> (Option T)
        if fn_name == 'list-pop' and len(args) >= 1:
            list_arg = args[0]
            list_type = self.infer_expr(list_arg)

            # Check mutability - list literals are immutable
            if isinstance(list_arg, Symbol):
                is_ptr_type = isinstance(list_type, PtrType)
                if not is_ptr_type and list_arg.name not in self._mutable_collections:
                    self.error(
                        f"cannot pop from immutable list '{list_arg.name}'",
                        list_arg,
                        hint="Use (list-new arena Type) to create a mutable list"
                    )

            # Return Option<element-type>
            if isinstance(list_type, ListType):
                return OptionType(list_type.element_type)
            elif isinstance(list_type, PtrType) and isinstance(list_type.pointee, ListType):
                return OptionType(list_type.pointee.element_type)
            return OptionType(UNKNOWN)

        # Special handling for map-put: requires mutable map
        # (map-put map key val)
        if fn_name == 'map-put' and len(args) == 3:
            map_arg = args[0]
            key_arg = args[1]
            val_arg = args[2]
            map_type = self.infer_expr(map_arg)

            # Check mutability - map literals are immutable
            # But pointers to maps (Ptr Map) are always mutable through the pointer
            if isinstance(map_arg, Symbol):
                is_ptr_type = isinstance(map_type, PtrType)
                if not is_ptr_type and map_arg.name not in self._mutable_collections:
                    self.error(
                        f"cannot put to immutable map '{map_arg.name}'",
                        map_arg,
                        hint="Use (map-new arena K V) to create a mutable map"
                    )

            # Accept either Map or Ptr<Map>
            if isinstance(map_type, PtrType) and isinstance(map_type.pointee, MapType):
                # Pointer to map is valid
                pass
            elif isinstance(map_type, MapType):
                # Direct map is valid
                pass
            elif not isinstance(map_type, UnknownType):
                self.error(f"argument 1 to 'map-put': expected (Map K V) or (Ptr (Map K V)), got {map_type}", map_arg)
            # Infer key and value types (no strict checking for polymorphic maps)
            self.infer_expr(key_arg)
            self.infer_expr(val_arg)
            return UNIT

        # Special handling for map-keys: (map-keys map) -> (List K)
        if fn_name == 'map-keys' and len(args) == 1:
            map_arg = args[0]
            map_type = self.infer_expr(map_arg)
            if isinstance(map_type, MapType):
                return ListType(map_type.key_type)
            elif isinstance(map_type, PtrType) and isinstance(map_type.pointee, MapType):
                return ListType(map_type.pointee.key_type)
            return ListType(UNKNOWN)

        # Special handling for map-remove: requires mutable map
        # (map-remove map key) -> Unit
        if fn_name == 'map-remove' and len(args) == 2:
            map_arg = args[0]
            key_arg = args[1]
            map_type = self.infer_expr(map_arg)

            # Check mutability - map literals are immutable
            if isinstance(map_arg, Symbol):
                is_ptr_type = isinstance(map_type, PtrType)
                if not is_ptr_type and map_arg.name not in self._mutable_collections:
                    self.error(
                        f"cannot remove from immutable map '{map_arg.name}'",
                        map_arg,
                        hint="Use (map-new arena K V) to create a mutable map"
                    )

            # Accept either Map or Ptr<Map>
            if isinstance(map_type, PtrType) and isinstance(map_type.pointee, MapType):
                pass
            elif isinstance(map_type, MapType):
                pass
            elif not isinstance(map_type, UnknownType):
                self.error(f"argument 1 to 'map-remove': expected (Map K V) or (Ptr (Map K V)), got {map_type}", map_arg)

            self.infer_expr(key_arg)
            return UNIT

        # Special handling for print/println - accept primitives (Int, Bool, String)
        if fn_name in ('print', 'println') and len(args) == 1:
            arg_type = self.infer_expr(args[0])
            # Accept Int (including range types), Bool, or String
            is_valid = False
            if isinstance(arg_type, RangeType):
                is_valid = True
            elif isinstance(arg_type, PrimitiveType):
                is_valid = arg_type.name in ('Int', 'Bool', 'String', 'Float',
                                              'I8', 'I16', 'I32', 'I64',
                                              'U8', 'U16', 'U32', 'U64')
            elif isinstance(arg_type, RecordType) and arg_type.name == 'String':
                is_valid = True
            if not is_valid and not isinstance(arg_type, UnknownType):
                self.error(f"{fn_name} requires a primitive type (Int, Bool, String), got {arg_type}", args[0])
            return UNIT

        fn_type = self.env.lookup_function(fn_name)
        if fn_type is None:
            # Check if this is a type constructor (TypeName arg1 arg2 ...)
            type_obj = self.env.lookup_type(fn_name)
            if type_obj is not None:
                if isinstance(type_obj, RecordType):
                    # Record constructor - validate arguments match fields
                    fields = list(type_obj.fields.items())
                    if len(args) != len(fields):
                        self.error(f"Record '{fn_name}' has {len(fields)} fields, got {len(args)} arguments", expr)
                    else:
                        for i, (arg, (field_name, field_type)) in enumerate(zip(args, fields)):
                            arg_type = self.infer_expr(arg)
                            self._check_assignable(arg_type, field_type, f"field '{field_name}' of '{fn_name}'", arg)
                    return type_obj
                elif isinstance(type_obj, EnumType):
                    # Enum used as type - just return the type
                    return type_obj
            # Check if it's a known builtin
            if fn_name in BUILTIN_FUNCTIONS:
                # Builtin functions - return UNKNOWN type but don't error
                return UNKNOWN
            # Undefined function - reject it
            self.error(f"Undefined function or operator: '{fn_name}'", expr)
            return UNKNOWN

        # Check if function is deprecated
        deprecation_msg = self.env.get_deprecation_message(fn_name)
        if deprecation_msg:
            self.warning(f"'{fn_name}' is deprecated: {deprecation_msg}", expr)

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

    def _widen_type(self, t: Type) -> Type:
        """Widen a type for collection element inference.

        Converts precise range types like (Int 1 .. 1) to their base type (Int).
        This allows collections to hold values with compatible types.
        """
        if isinstance(t, RangeType):
            # Widen to unbounded base type
            return PrimitiveType(t.base)
        return t

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
            # Fallback: compare element types by string representation (for forward references)
            if str(source.element_type) == str(target.element_type):
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
            # UNKNOWN or TypeVar pointee matches any type (polymorphic pointers like arena-alloc, nil)
            if isinstance(source.pointee, (UnknownType, TypeVar)) or isinstance(target.pointee, (UnknownType, TypeVar)):
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
            # Fallback: compare by string representation (for forward references)
            if str(source.ok_type) == str(target.ok_type) and str(source.err_type) == str(target.err_type):
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

    def _validate_module_structure(self, ast: List[SExpr]):
        """Validate that definitions are inside module forms when one exists."""
        has_module = any(is_form(form, 'module') for form in ast)

        if has_module:
            for form in ast:
                if isinstance(form, SList) and len(form) > 0:
                    form_type = form[0].name if isinstance(form[0], Symbol) else str(form[0])
                    if form_type in ('fn', 'impl', 'type', 'const'):
                        self.error(
                            f"'{form_type}' definition must be inside (module ...) form",
                            form
                        )

    def check_module(self, ast: List[SExpr]) -> List[TypeDiagnostic]:
        """Type check an entire module"""
        # Validate module structure first
        self._validate_module_structure(ast)

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

        # Check for ambiguous enum variants (collisions between local and imported types)
        self._check_enum_variant_collisions()

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

        # Extract return type from @spec and check for @deprecated
        return_type: Type = PrimitiveType('Unit')
        deprecated_msg: Optional[str] = None
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
            elif is_form(item, '@deprecated') and len(item) > 1:
                # Extract deprecation message
                if isinstance(item[1], String):
                    deprecated_msg = item[1].value
                else:
                    deprecated_msg = str(item[1])

        fn_type = FnType(tuple(param_types), return_type)
        self.env.register_function(name, fn_type)

        # Register deprecation if present
        if deprecated_msg is not None:
            self.env.register_deprecated(name, deprecated_msg)

    def _register_ffi_funcs(self, form: SList):
        """Register FFI function signatures and constants.

        FFI form: (ffi "header.h" (decl...) ...)

        Functions: (func-name ((param Type)...) ReturnType) - item[1] is SList
        Constants: (const-name Type) - item[1] is Symbol
        """
        if len(form) < 2:
            return

        # Skip header string, process declarations
        for item in form.items[2:]:
            if isinstance(item, SList) and len(item) >= 2:
                name = item[0].name if isinstance(item[0], Symbol) else str(item[0])
                second = item[1]

                if isinstance(second, SList):
                    # Function: (func-name ((param Type)...) ReturnType)
                    if len(item) >= 3:
                        params_list = second
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
                        self.env.register_function(name, fn_type)
                else:
                    # Constant: (const-name Type)
                    const_type = self.parse_type_expr(second)
                    self.env.bind_var(name, const_type)

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
        # Track current function's return type for ? operator validation
        self._current_fn_return_type = fn_type.return_type

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

        # Type check @pre conditions (params are in scope)
        for item in form.items[3:]:
            if is_form(item, '@pre') and len(item) > 1:
                pre_type = self.infer_expr(item[1])
                self._expect_type(pre_type, PrimitiveType('Bool'), "@pre condition", item[1])

        # Type check @post conditions ($result is in scope)
        # Bind $result to the function's return type for postcondition checking
        self.env.bind_var('$result', fn_type.return_type)
        for item in form.items[3:]:
            if is_form(item, '@post') and len(item) > 1:
                post_type = self.infer_expr(item[1])
                self._expect_type(post_type, PrimitiveType('Bool'), "@post condition", item[1])

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


def _find_project_config(start_path: 'Path') -> 'Optional[Path]':
    """Search upward from start_path for a slop.toml file."""
    current = start_path.resolve()
    # Search up to 10 parent directories
    for _ in range(10):
        config_path = current / "slop.toml"
        if config_path.exists():
            return config_path
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def check_file(path: str) -> List[TypeDiagnostic]:
    """Type check a SLOP file.

    If the file has imports, performs full module resolution to get
    imported function signatures for accurate type checking.
    Uses slop.toml configuration if found for module search paths.
    """
    from slop.parser import parse_file, is_form
    from pathlib import Path
    import sys

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
        from slop.providers import load_project_config

        file_path = Path(path).resolve()

        # Build search paths from multiple sources
        search_paths = []

        # 1. Try to find project-local slop.toml by searching upward
        project_config_path = _find_project_config(file_path.parent)
        if project_config_path:
            # Load config from the found slop.toml
            _, build_cfg = load_project_config(str(project_config_path))
            if build_cfg and build_cfg.include:
                # Include paths are relative to the config file's directory
                config_dir = project_config_path.parent
                for p in build_cfg.include:
                    search_paths.append((config_dir / p).resolve())

        # 2. Also try loading from CWD (for backward compatibility)
        if not search_paths:
            _, build_cfg = load_project_config()
            if build_cfg and build_cfg.include:
                for p in build_cfg.include:
                    search_paths.append(Path(p).resolve())

        # 3. Add parent directories as fallback
        search_paths.extend([file_path.parent, file_path.parent.parent])

        resolver = ModuleResolver(search_paths)
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
        except Exception as e:
            # Log error and fall back to single-file check
            print(f"warning: Module resolution failed ({e}), falling back to single-file check", file=sys.stderr)
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


def check_source_ast(ast: List, source_path: str = "<stdin>") -> List[TypeDiagnostic]:
    """Type check an already-parsed AST.

    This allows sharing AST between type checker and transpiler,
    so resolved_type annotations can flow through.
    """
    checker = TypeChecker(source_path)
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
                for fn_name in imp.symbols:
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

        for fn_name in info.exports:
            if fn_name in checker.env.function_sigs:
                exported_sigs[mod_name][fn_name] = checker.env.function_sigs[fn_name]

        # Export all types defined in this module
        for type_name, typ in checker.env.type_registry.items():
            if type_name not in checker.env.imported_types:
                exported_types[mod_name][type_name] = typ

    return results
