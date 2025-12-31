"""
SLOP Transpiler - Generate C code from SLOP AST

Handles:
- Type definitions → C structs/typedefs
- Functions → C functions
- Range types → runtime checks
- Contracts → SLOP_PRE/SLOP_POST macros
"""

from dataclasses import dataclass
from typing import List, Dict, Optional, Set
from slop.parser import SExpr, SList, Symbol, String, Number, is_form, parse, find_holes


class UnfilledHoleError(Exception):
    """Raised when transpilation is attempted with unfilled holes"""
    def __init__(self, holes: List[SList]):
        self.holes = holes
        count = len(holes)
        super().__init__(f"Cannot transpile: {count} unfilled hole(s) found")


class TranspileError(Exception):
    """Raised when transpilation encounters malformed input"""
    def __init__(self, message: str, expr=None):
        self.message = message
        self.expr = expr
        line_info = f" at line {expr.line}" if expr and hasattr(expr, 'line') and expr.line else ""
        super().__init__(f"{message}{line_info}")


@dataclass
class TypeInfo:
    """Information about a SLOP type"""
    name: str
    c_type: str
    is_range: bool = False
    min_val: Optional[int] = None
    max_val: Optional[int] = None
    is_array: bool = False  # True if this is an array type alias


class Transpiler:
    """Transpile SLOP to C"""

    # C reserved keywords that cannot be used as identifiers
    C_KEYWORDS = {
        'auto', 'break', 'case', 'char', 'const', 'continue', 'default', 'do',
        'double', 'else', 'enum', 'extern', 'float', 'for', 'goto', 'if',
        'int', 'long', 'register', 'return', 'short', 'signed', 'sizeof',
        'static', 'struct', 'switch', 'typedef', 'union', 'unsigned', 'void',
        'volatile', 'while', '_Bool', '_Complex', '_Imaginary',
        # C99/C11 additions
        'inline', 'restrict', '_Alignas', '_Alignof', '_Atomic', '_Generic',
        '_Noreturn', '_Static_assert', '_Thread_local',
    }

    # SLOP forms that should be transpiled as statements, not expressions
    STATEMENT_FORMS = {'for-each', 'for', 'while', 'set!', 'if', 'when', 'unless'}

    def __init__(self):
        self.types: Dict[str, TypeInfo] = {}
        self.enums: Dict[str, str] = {}  # value -> qualified name
        self.simple_enums: Set[str] = set()  # Type names that are simple enums
        self.ffi_funcs: Dict[str, dict] = {}  # FFI function registry
        self.ffi_structs: Dict[str, dict] = {}  # FFI struct registry
        self.ffi_struct_fields: Dict[str, Dict[str, dict]] = {}  # struct → field → type info
        self.ffi_includes: List[str] = []  # FFI headers to include
        self.output: List[str] = []
        self.indent = 0
        self.pointer_vars: Set[str] = set()  # Track variables that are pointers
        self.var_types: Dict[str, str] = {}  # Track variable types: var_name -> type_name
        self.record_fields: Dict[str, Dict[str, dict]] = {}  # type_name -> {field_name -> {'is_pointer': bool, 'type': SExpr}}
        self.func_returns_pointer: Dict[str, bool] = {}  # func_name -> returns_pointer
        self.current_return_type: Optional[SExpr] = None  # Current function's return type for context
        self.generated_option_types: Set[str] = set()  # Track generated Option<T> types
        self.generated_result_types: Set[str] = set()  # Track generated Result<T, E> types
        self.generated_list_types: Set[str] = set()  # Track generated List<T> types
        self.generated_map_types: Set[tuple] = set()  # Track generated Map<K, V> types: (type_name, key_c_type, value_c_type)
        self.generated_inline_records: Dict[str, str] = {}  # Track inline record types: type_name -> struct_body
        self.emitted_generated_types: Set[str] = set()  # Track which generated types have been emitted (to avoid duplicates)

        # ScopedPtr tracking - stack of scopes, each maps var_name -> pointee_c_type
        self.scoped_vars: List[Dict[str, str]] = [{}]
        self.records_needing_destructor: Dict[str, List[str]] = {}  # record_name -> [scoped_field_names]
        self.generated_destructors: Set[str] = set()  # Track which destructors have been generated

        # Module system support
        self.current_module: Optional[str] = None  # Current module being transpiled
        self.import_map: Dict[str, str] = {}  # local_name -> qualified_c_name
        self.local_functions: Set[str] = set()  # Functions defined in current module
        self.enable_prefixing: bool = False  # Enable module prefixing (for multi-module builds)

        # Built-in type mappings
        self.builtin_types = {
            'Int': 'int64_t',
            'I8': 'int8_t',
            'I16': 'int16_t',
            'I32': 'int32_t',
            'I64': 'int64_t',
            'U8': 'uint8_t',
            'U16': 'uint16_t',
            'U32': 'uint32_t',
            'U64': 'uint64_t',
            'Float': 'double',
            'F32': 'float',
            'Bool': 'uint8_t',
            'String': 'slop_string',
            'Bytes': 'slop_bytes',
            'Unit': 'void',
            'Void': 'void',
            'Arena': 'slop_arena*',
            'Milliseconds': 'int64_t',
        }

    def emit(self, line: str = ""):
        """Emit a line of C code"""
        if line:
            self.output.append("    " * self.indent + line)
        else:
            self.output.append("")

    def _unquote_symbol(self, name: str) -> str:
        """Strip leading quote from quoted symbols: 'Fizz -> Fizz"""
        return name[1:] if name.startswith("'") else name

    def _is_pointer_type(self, type_expr: SExpr) -> bool:
        """Check if a type expression is a pointer type"""
        if isinstance(type_expr, SList) and len(type_expr) >= 1:
            head = type_expr[0]
            if isinstance(head, Symbol) and head.name == 'Ptr':
                return True
        return False

    def _is_pointer_expr(self, expr: SExpr) -> bool:
        """Check if an expression is known to return a pointer"""
        if isinstance(expr, Symbol):
            name = expr.name
            # Check explicit pointer tracking
            if name in self.pointer_vars:
                return True
            # Check type flow: if we know the variable's type
            if name in self.var_types:
                var_type = self.var_types[name]
                # Type ending in * is a pointer
                if var_type.endswith('*'):
                    return True
                # Check if it's an array type alias (treated as pointer)
                if var_type in self.types and self.types[var_type].is_array:
                    return True
            return False
        if isinstance(expr, SList) and len(expr) >= 1:
            head = expr[0]
            if isinstance(head, Symbol):
                op = head.name
                # arena-alloc always returns a pointer
                if op == 'arena-alloc':
                    return True
                # deref yields a value, not a pointer
                if op == 'deref':
                    return False
                # addr yields a pointer
                if op == 'addr':
                    return True
                # cast to pointer type: (cast (Ptr T) ...)
                if op == 'cast' and len(expr) >= 2:
                    target_type = expr[1]
                    if is_form(target_type, 'Ptr'):
                        return True
                # Field access: (. obj field) - check if field is a pointer type
                if op == '.' and len(expr) >= 3:
                    obj_expr = expr[1]
                    field = expr[2]
                    if isinstance(field, Symbol):
                        field_name = field.name
                        # Look up field type in known record types
                        for type_name, fields in self.record_fields.items():
                            if field_name in fields and fields[field_name]['is_pointer']:
                                return True
                    return False
                # Function call: check if function returns a pointer
                if op in self.func_returns_pointer:
                    return self.func_returns_pointer[op]
        return False

    # ScopedPtr helper methods
    def _push_scoped_scope(self):
        """Push a new scope for scoped pointer tracking"""
        self.scoped_vars.append({})

    def _pop_scoped_scope(self) -> Dict[str, str]:
        """Pop scope, returning scoped vars that need cleanup"""
        return self.scoped_vars.pop()

    def _register_scoped(self, name: str, c_type: str):
        """Register a variable as holding a scoped pointer"""
        self.scoped_vars[-1][name] = c_type

    def _emit_scoped_cleanup(self, scoped: Dict[str, str]):
        """Emit cleanup code for scoped variables"""
        # Cleanup in reverse allocation order
        for name in reversed(list(scoped.keys())):
            c_name = self.to_c_name(name)
            self.emit(f"if ({c_name}) free({c_name});")

    def _is_scoped_ptr_type(self, type_expr: SExpr) -> bool:
        """Check if a type expression is ScopedPtr"""
        return is_form(type_expr, 'ScopedPtr')

    def _get_scoped_pointee_type(self, type_expr: SExpr) -> str:
        """Get C type of pointee from ScopedPtr type"""
        if is_form(type_expr, 'ScopedPtr') and len(type_expr) >= 2:
            return self.to_c_type(type_expr[1])
        return 'void'

    def _parse_parameter_mode(self, param: SExpr) -> tuple:
        """Extract (mode, name, type) from parameter form.

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

    def _apply_parameter_mode(self, mode: str, c_type: str) -> str:
        """Transform C type based on parameter mode.

        - 'in':  Pass by value (default, unchanged)
        - 'out': Always pointer (uninitialized)
        - 'mut': Always pointer (initialized/mutable)
        """
        if mode == 'in':
            # Pass by value - no change
            return c_type
        elif mode == 'out' or mode == 'mut':
            # Always pointer for out/mut
            if not c_type.endswith('*'):
                return f"{c_type}*"
            return c_type

        return c_type

    def _get_type_name(self, type_expr: SExpr) -> Optional[str]:
        """Extract the type name from a type expression for tracking"""
        if isinstance(type_expr, Symbol):
            return type_expr.name
        if isinstance(type_expr, SList) and len(type_expr) >= 1:
            head = type_expr[0]
            if isinstance(head, Symbol):
                op = head.name
                # (Ptr T) -> track as "T*"
                if op == 'Ptr' and len(type_expr) >= 2:
                    inner = self._get_type_name(type_expr[1])
                    if inner:
                        return f"{inner}*"
                # (Array T size) -> track as T (element type for indexing)
                if op == 'Array' and len(type_expr) >= 2:
                    return self._get_type_name(type_expr[1])
                # (Option T), (Result T E) -> track the wrapper type name
                if op in ('Option', 'Result'):
                    return op
                # (List T) -> track as "List[elem_c_type]" to preserve element type
                if op == 'List' and len(type_expr) >= 2:
                    elem_c_type = self.to_c_type(type_expr[1])
                    return f"List[{elem_c_type}]"
                # (Map K V) -> track as slop_map_K_V (C type name)
                if op == 'Map' and len(type_expr) >= 3:
                    key_c = self.to_c_type(type_expr[1])
                    val_c = self.to_c_type(type_expr[2])
                    key_id = self._type_to_identifier(key_c)
                    val_id = self._type_to_identifier(val_c)
                    # String-keyed maps use slop_map_string_X pattern
                    if key_c == 'slop_string':
                        return f"slop_map_string_{val_id}"
                    return f"slop_map_{key_id}_{val_id}"
        return None

    def _get_result_c_type(self) -> str:
        """Get the C type for the current function's Result return type.

        Used by ok/error constructors to generate proper compound literals.
        """
        if self.current_return_type is None:
            return "slop_result"

        ret = self.current_return_type
        if isinstance(ret, SList) and len(ret) >= 1:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'Result':
                ok_type = self.to_c_type(ret[1]) if len(ret) > 1 else "void"
                err_type = self.to_c_type(ret[2]) if len(ret) > 2 else "slop_error"
                ok_id = self._type_to_identifier(ok_type)
                err_id = self._type_to_identifier(err_type)
                return f"slop_result_{ok_id}_{err_id}"

        return "slop_result"

    def _transpile_statement_to_string(self, expr: SList) -> str:
        """Transpile a statement form to a string for use in GCC statement expressions.

        Temporarily captures output to a separate buffer, then restores.
        """
        saved_output = self.output
        saved_indent = self.indent
        self.output = []
        self.indent = 0
        self.transpile_statement(expr, is_return=False)
        result = ' '.join(self.output)
        self.output = saved_output
        self.indent = saved_indent
        return result

    def _transpile_option_constructor_with_type(self, expr: SExpr, option_c_type: str) -> str:
        """Transpile an expression, using the given Option C type for some/none constructors.

        This is used in match expressions where we've inferred the result type.
        """
        # Check if this is a (some val) constructor
        if isinstance(expr, SList) and len(expr) >= 1 and isinstance(expr[0], Symbol):
            if expr[0].name == 'some' and len(expr) >= 2:
                val = self.transpile_expr(expr[1])
                return f"(({option_c_type}){{ .has_value = true, .value = {val} }})"
            elif expr[0].name == 'none':
                return f"(({option_c_type}){{ .has_value = false }})"
        # Check if this is (none) as a standalone symbol
        elif isinstance(expr, Symbol) and expr.name == 'none':
            return f"(({option_c_type}){{ .has_value = false }})"
        # Otherwise use normal transpilation
        return self.transpile_expr(expr)

    def _get_option_c_type(self, expected_type: Optional[SExpr] = None) -> Optional[str]:
        """Get the C type for the current context's Option type.

        Used by none/some constructors to generate proper compound literals.
        Returns None if no Option type can be inferred.

        Args:
            expected_type: If provided, use this instead of current_return_type
        """
        ret = expected_type if expected_type is not None else self.current_return_type
        if ret is None:
            return None

        if isinstance(ret, SList) and len(ret) >= 1:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'Option':
                inner_type = self.to_c_type(ret[1]) if len(ret) > 1 else "void*"
                inner_id = self._type_to_identifier(inner_type)
                return f"slop_option_{inner_id}"

        return None

    def _get_list_c_type(self, expected_type: Optional[SExpr] = None) -> Optional[str]:
        """Get the C type for the current context's List type.

        Used by list-new to generate proper struct initialization.
        Returns None if no List type can be inferred.

        Args:
            expected_type: If provided, use this instead of current_return_type
        """
        ret = expected_type if expected_type is not None else self.current_return_type
        if ret is None:
            return None

        if isinstance(ret, SList) and len(ret) >= 1:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'List':
                return self.to_c_type(ret)  # Returns full list type like slop_list_http_Header

        return None

    def _get_list_element_c_type(self, expected_type: Optional[SExpr] = None) -> Optional[str]:
        """Get the element C type for the current context's List type.

        Returns None if no List type can be inferred.

        Args:
            expected_type: If provided, use this instead of current_return_type
        """
        ret = expected_type if expected_type is not None else self.current_return_type
        if ret is None:
            return None

        if isinstance(ret, SList) and len(ret) >= 2:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'List':
                return self.to_c_type(ret[1])  # Returns element type

        return None

    def _type_to_identifier(self, c_type: str) -> str:
        """Convert C type to valid identifier component.

        Handles pointer types and other special characters that are
        invalid in C identifiers. Also strips slop_ prefix from builtin
        types to avoid double-prefixing in container types.

        Examples:
            "NewPet*" -> "NewPet_ptr"
            "int64_t" -> "int"  (normalized for runtime compatibility)
            "void*" -> "void_ptr"
            "slop_string" -> "string"
            "struct { int64_t x; int64_t y; }" -> "anon_<hash>"
        """
        # Handle inline struct types - generate a hash-based identifier
        if c_type.startswith('struct {'):
            import hashlib
            hash_val = hashlib.md5(c_type.encode()).hexdigest()[:8]
            return f"anon_{hash_val}"

        result = c_type.replace('*', '_ptr').replace(' ', '_')
        # Strip slop_ prefix from builtin types for cleaner container names
        # e.g., slop_option_string instead of slop_option_slop_string
        if result.startswith('slop_'):
            result = result[5:]
        # Normalize integer types to match runtime function naming
        # e.g., slop_map_string_int_get (not slop_map_string_int64_t_get)
        if result == 'int64_t':
            result = 'int'
        return result

    def _get_map_value_type_from_context(self) -> Optional[str]:
        """Get the value type for map_get from current return type context.

        If return type is Option<T>, returns T's C type name.
        """
        if self.current_return_type is None:
            return None

        ret = self.current_return_type
        if isinstance(ret, SList) and len(ret) >= 2:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'Option':
                inner = ret[1]
                # Always convert to C type, even for simple symbols
                return self.to_c_type(inner)
        return None

    def _get_list_element_type_from_context(self) -> Optional[str]:
        """Get the element type for map_values from current return type context.

        If return type is List<T>, returns T's C type name.
        """
        if self.current_return_type is None:
            return None

        ret = self.current_return_type
        if isinstance(ret, SList) and len(ret) >= 2:
            head = ret[0]
            if isinstance(head, Symbol) and head.name == 'List':
                inner = ret[1]
                # Always convert to C type (includes module prefix)
                return self.to_c_type(inner)
        return None

    def _get_field_type(self, target_var: str, field_name: str) -> Optional[SExpr]:
        """Look up the type of a field given a variable and field name.

        Args:
            target_var: Variable name (e.g., 'resp')
            field_name: Field name (e.g., 'headers')

        Returns:
            The field type expression, or None if not found
        """
        # Get the type of the target variable from var_types
        if target_var not in self.var_types:
            return None

        var_type = self.var_types[target_var]

        # Remove pointer suffix for record lookup (e.g., "http_HttpResponse*" -> "http_HttpResponse")
        base_type = var_type.rstrip('*').strip()

        # Try stripping module prefix for field lookup
        type_name = base_type
        if '_' in base_type:
            # Could be module-prefixed like http_HttpResponse
            parts = base_type.split('_', 1)
            if len(parts) == 2:
                type_name = parts[1]  # e.g., "HttpResponse"

        # Look up in record_fields using unqualified name
        if type_name in self.record_fields:
            if field_name in self.record_fields[type_name]:
                return self.record_fields[type_name][field_name].get('type')

        # Also try the full/qualified name
        if base_type in self.record_fields:
            if field_name in self.record_fields[base_type]:
                return self.record_fields[base_type][field_name].get('type')

        return None

    def _get_expr_type(self, expr: SExpr) -> Optional[str]:
        """Get the type of an expression.

        Returns the type name as a string, or None if unknown.
        """
        if isinstance(expr, Symbol):
            # Variable - look up in var_types
            if expr.name in self.var_types:
                return self.var_types[expr.name]
            return None

        if isinstance(expr, SList) and len(expr) >= 1:
            if is_form(expr, '.') and len(expr) == 3:
                # Field access - get base type and find field type
                base = expr[1]
                field = expr[2]
                if isinstance(field, Symbol):
                    base_type = self._get_expr_type(base)
                    if base_type:
                        field_type = self._get_type_field(base_type, field.name)
                        if field_type:
                            return self.to_c_type(field_type)
        return None

    def _get_type_field(self, type_name: str, field_name: str) -> Optional[SExpr]:
        """Look up the type of a field given a type name.

        Args:
            type_name: Type name (e.g., 'Task*' or 'Task')
            field_name: Field name (e.g., 'next')

        Returns:
            The field type expression, or None if not found
        """
        # Remove pointer suffix
        base_type = type_name.rstrip('*').strip()

        # Try stripping module prefix
        lookup_name = base_type
        if '_' in base_type:
            parts = base_type.split('_', 1)
            if len(parts) == 2:
                lookup_name = parts[1]

        # Look up in record_fields
        if lookup_name in self.record_fields:
            if field_name in self.record_fields[lookup_name]:
                return self.record_fields[lookup_name][field_name].get('type')

        # Also try full name
        if base_type in self.record_fields:
            if field_name in self.record_fields[base_type]:
                return self.record_fields[base_type][field_name].get('type')

        return None

    def _infer_record_type_from_context(self) -> Optional[str]:
        """Infer record type from current context (return type, etc.)

        Used by bare record literals to determine the struct type.
        """
        if self.current_return_type is None:
            return None

        ret = self.current_return_type
        # Direct record type: (type Name (record ...)) results in Symbol name
        if isinstance(ret, Symbol):
            name = ret.name
            if name in self.types:
                return name
            return name  # Assume it's a valid type name

        # Result<RecordType, Error>: extract the ok type
        if isinstance(ret, SList) and len(ret) >= 2:
            head = ret[0]
            if isinstance(head, Symbol):
                if head.name == 'Result' and len(ret) >= 2:
                    ok_type = ret[1]
                    if isinstance(ok_type, Symbol):
                        return ok_type.name
                elif head.name == 'Option' and len(ret) >= 2:
                    inner = ret[1]
                    if isinstance(inner, Symbol):
                        return inner.name

        return None

    def _generate_inline_record_type(self, record_expr: SList) -> str:
        """Generate C struct type for inline record definition.

        Handles: (record (field1 Type1) (field2 Type2) ...)
        Returns: Named typedef (e.g., _anon_record_<hash>)
        """
        # Use to_c_type which now generates named typedefs for inline records
        return self.to_c_type(record_expr)

    def _transpile_match_expr(self, expr: SList) -> str:
        """Transpile match as an expression using GCC statement expression.

        Handles Option, Result, and enum matching.
        """
        scrutinee = self.transpile_expr(expr[1])
        clauses = expr.items[2:]

        # Detect pattern type from first clause
        first_clause = clauses[0] if clauses else None
        if first_clause and isinstance(first_clause, SList):
            pattern = first_clause[0]
            if isinstance(pattern, SList) and len(pattern) >= 1:
                tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                # Option/Result patterns
                if tag in ('some', 'none', 'ok', 'error'):
                    return self._transpile_option_result_match_expr(scrutinee, clauses, tag)

        # Check if it's a simple enum match - both quoted ('ok) and unquoted (ok)
        is_simple_enum = False
        for clause in clauses:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                # Check for enum values - both quoted and unquoted
                if isinstance(pattern, Symbol):
                    unquoted = self._unquote_symbol(pattern.name)
                    if unquoted in self.enums:
                        is_simple_enum = True
                        break
                if isinstance(pattern, SList) and len(pattern) >= 1:
                    raw_tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    if raw_tag:
                        tag = self._unquote_symbol(raw_tag)
                        if tag in self.enums:
                            is_simple_enum = True
                            break

        # Build switch as statement expression
        # Try to infer result type from first branch value
        result_c_type = None
        if clauses:
            first_clause = clauses[0]
            if isinstance(first_clause, SList) and len(first_clause) >= 2:
                first_body = first_clause.items[1:]
                if first_body:
                    last_expr = first_body[-1]
                    # Check for string literal
                    if isinstance(last_expr, String):
                        result_c_type = "slop_string"
                    # Check for number literal
                    elif isinstance(last_expr, Number):
                        result_c_type = "int64_t"
                    # Check for boolean literal (true/false)
                    elif isinstance(last_expr, Symbol) and last_expr.name in ('true', 'false'):
                        result_c_type = "bool"

        # Fall back to function's result type if we couldn't infer
        if result_c_type is None:
            result_c_type = self._get_result_c_type()

        parts = ["({ __auto_type _match_val = ", scrutinee, f"; {result_c_type} _match_result = {{0}}; "]

        if is_simple_enum:
            parts.append("switch (_match_val) { ")
        else:
            parts.append("switch (_match_val.tag) { ")

        for i, clause in enumerate(clauses):
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                body = clause.items[1:]

                if isinstance(pattern, Symbol):
                    if pattern.name == '_' or pattern.name == 'else':
                        parts.append("default: { ")
                    else:
                        # Check for enum value - both quoted ('ok) and unquoted (ok)
                        unquoted = self._unquote_symbol(pattern.name)
                        if unquoted in self.enums:
                            parts.append(f"case {self.enums[unquoted]}: {{ ")
                        else:
                            parts.append(f"case {i}: {{ ")

                    # Emit body - only the last expression becomes the result
                    for j, item in enumerate(body):
                        if j == len(body) - 1:
                            parts.append(f"_match_result = {self.transpile_expr(item)}; ")
                        else:
                            parts.append(f"{self.transpile_expr(item)}; ")
                    parts.append("break; } ")

                elif isinstance(pattern, SList) and len(pattern) >= 1:
                    raw_tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    # Only quoted tags match enum values
                    is_quoted_tag = raw_tag and raw_tag.startswith("'")
                    tag = self._unquote_symbol(raw_tag) if raw_tag else None
                    var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None

                    # Check for enum variant - allow both quoted ('ok) and unquoted (ok)
                    if is_simple_enum and tag in self.enums:
                        parts.append(f"case {self.enums[tag]}: {{ ")
                    else:
                        parts.append(f"case {i}: {{ ")

                    if var_name and not is_simple_enum:
                        parts.append(f"__auto_type {var_name} = _match_val.data.{tag}; ")

                    for j, item in enumerate(body):
                        if j == len(body) - 1:
                            parts.append(f"_match_result = {self.transpile_expr(item)}; ")
                        else:
                            parts.append(f"{self.transpile_expr(item)}; ")
                    parts.append("break; } ")

        parts.append("} _match_result; })")
        return ''.join(parts)

    def _transpile_option_result_match_expr(self, scrutinee: str, clauses: list, first_tag: str) -> str:
        """Transpile Option/Result match as expression.

        Option uses: has_value (bool), value field
        Result uses: is_ok (bool), data.ok/data.err fields
        """
        is_option = first_tag in ('some', 'none')
        is_result = first_tag in ('ok', 'error')

        # Try to infer result type from ANY branch value (check all branches)
        result_c_type = None
        for clause in clauses:
            if isinstance(clause, SList) and len(clause) >= 2:
                body = clause.items[1:]
                if body:
                    last_expr = body[-1]
                    # Check for string literal
                    if isinstance(last_expr, String):
                        result_c_type = "slop_string"
                        break
                    # Check for number literal
                    elif isinstance(last_expr, Number):
                        result_c_type = "int64_t"
                        break
                    # Check for boolean literal (true/false)
                    elif isinstance(last_expr, Symbol) and last_expr.name in ('true', 'false'):
                        result_c_type = "bool"
                        break
                    # Check for (none) - result is an Option type
                    elif isinstance(last_expr, Symbol) and last_expr.name == 'none':
                        result_c_type = "slop_option_int"  # Default, will be refined below
                        break
                    # Check for (some val) - result is an Option type
                    elif isinstance(last_expr, SList) and len(last_expr) >= 1:
                        if isinstance(last_expr[0], Symbol) and last_expr[0].name == 'some':
                            # Infer inner type from the value
                            if len(last_expr) >= 2:
                                inner = last_expr[1]
                                if isinstance(inner, Number):
                                    result_c_type = "slop_option_int"
                                elif isinstance(inner, String):
                                    result_c_type = "slop_option_string"
                                elif isinstance(inner, SList) and len(inner) >= 1:
                                    if isinstance(inner[0], Symbol) and inner[0].name == 'cast':
                                        # (some (cast Type val)) - use int for now
                                        result_c_type = "slop_option_int"
                                    else:
                                        result_c_type = "slop_option_ptr"
                                else:
                                    result_c_type = "slop_option_ptr"
                            else:
                                result_c_type = "slop_option_ptr"
                            break
                        elif isinstance(last_expr[0], Symbol) and last_expr[0].name == 'none':
                            result_c_type = "slop_option_int"
                            break
                    # For symbols/variables, continue checking other branches

        # If still None and this is an Option match with bound variable,
        # the result type is the Option's inner type - use __auto_type workaround
        if result_c_type is None and is_option:
            # Use slop_string as a reasonable default for Option matches
            # that return bound variables (common pattern: (some x) x, (none) "")
            for clause in clauses:
                if isinstance(clause, SList) and len(clause) >= 2:
                    pattern = clause[0]
                    if isinstance(pattern, SList) and len(pattern) >= 2:
                        tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                        if tag == 'some':
                            # Check if the body returns the bound variable
                            body = clause.items[1:]
                            if body and isinstance(body[-1], Symbol):
                                bound_var = pattern[1].name if isinstance(pattern[1], Symbol) else None
                                if bound_var and body[-1].name == bound_var:
                                    # The some branch returns the bound variable
                                    # Check other branches for type hints
                                    for other in clauses:
                                        if other is not clause and isinstance(other, SList) and len(other) >= 2:
                                            other_body = other.items[1:]
                                            if other_body and isinstance(other_body[-1], String):
                                                result_c_type = "slop_string"
                                                break

        # Fall back to function's result type if we couldn't infer
        if result_c_type is None:
            result_c_type = self._get_result_c_type()

        # Build the match as if-else chain (since we're checking bools)
        parts = ["({ __auto_type _match_val = ", scrutinee, f"; {result_c_type} _match_result = {{0}}; "]

        # Build condition and body for each case
        some_ok_clause = None
        none_err_clause = None

        for clause in clauses:
            if not isinstance(clause, SList) or len(clause) < 2:
                continue

            pattern = clause[0]
            body = clause.items[1:]
            tag = None
            var_name = None

            if isinstance(pattern, SList) and len(pattern) >= 1:
                tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None
            elif isinstance(pattern, Symbol):
                tag = pattern.name

            if tag in ('some', 'ok'):
                some_ok_clause = (var_name, body, tag)
            elif tag in ('none', 'error'):
                none_err_clause = (var_name, body, tag)

        # Generate if-else for Option (has_value) or Result (is_ok)
        if is_option:
            check_field = "_match_val.has_value"
            value_field = "_match_val.value"
        else:  # is_result
            check_field = "_match_val.is_ok"

        if some_ok_clause:
            var_name, body, tag = some_ok_clause
            parts.append(f"if ({check_field}) {{ ")
            if var_name and var_name != '_':
                if is_option:
                    parts.append(f"__auto_type {var_name} = {value_field}; ")
                else:
                    parts.append(f"__auto_type {var_name} = _match_val.data.ok; ")
            for j, item in enumerate(body):
                if j == len(body) - 1:
                    # Special handling for (some val) and (none) constructors
                    expr_str = self._transpile_option_constructor_with_type(item, result_c_type)
                    parts.append(f"_match_result = {expr_str}; ")
                else:
                    parts.append(f"{self.transpile_expr(item)}; ")
            parts.append("} ")

        if none_err_clause:
            var_name, body, tag = none_err_clause
            if some_ok_clause:
                parts.append("else { ")
            else:
                parts.append(f"if (!{check_field}) {{ ")
            if var_name and var_name != '_' and is_result:
                parts.append(f"__auto_type {var_name} = _match_val.data.err; ")
            for j, item in enumerate(body):
                if j == len(body) - 1:
                    # Special handling for (some val) and (none) constructors
                    expr_str = self._transpile_option_constructor_with_type(item, result_c_type)
                    parts.append(f"_match_result = {expr_str}; ")
                else:
                    parts.append(f"{self.transpile_expr(item)}; ")
            parts.append("} ")

        parts.append("_match_result; })")
        return ''.join(parts)

    def _infer_type(self, expr: SExpr) -> Optional[str]:
        """Infer the type name of an expression for type flow analysis"""
        if isinstance(expr, Symbol):
            name = expr.name
            # Check if it's a known variable with tracked type
            if name in self.var_types:
                return self.var_types[name]
            return None

        if isinstance(expr, SList) and len(expr) >= 1:
            head = expr[0]
            if isinstance(head, Symbol):
                op = head.name

                # Array index: (@ arr idx) -> element type (not pointer to element)
                if op == '@' and len(expr) >= 2:
                    arr_expr = expr[1]
                    if isinstance(arr_expr, Symbol):
                        arr_type = self.var_types.get(arr_expr.name)
                        if arr_type:
                            # Handle pointer to array type: "Storage*" -> look up "Storage"
                            base_type = arr_type.rstrip('*')
                            if base_type in self.types:
                                type_info = self.types[base_type]
                                # If it's an array alias, return the element type
                                if type_info.is_array and type_info.c_type.endswith('*'):
                                    # c_type is like "Invoice*", return "Invoice"
                                    elem_type = type_info.c_type[:-1].strip()
                                    return elem_type
                            # If arr_type is directly an array alias
                            if arr_type in self.types:
                                type_info = self.types[arr_type]
                                if type_info.is_array and type_info.c_type.endswith('*'):
                                    elem_type = type_info.c_type[:-1].strip()
                                    return elem_type
                            # If it's a pointer type like "Invoice*", return "Invoice"
                            if arr_type.endswith('*'):
                                return arr_type[:-1].strip()
                    return None

                # Cast: (cast Type expr) -> Type
                if op == 'cast' and len(expr) >= 2:
                    return self.to_c_type(expr[1])

                # Record construction: (record-new Type ...) -> Type
                if op == 'record-new' and len(expr) >= 2:
                    if isinstance(expr[1], Symbol):
                        return expr[1].name

                # List construction: (list-new arena Type) -> List[C_Type]
                if op == 'list-new' and len(expr) > 2:
                    elem_type_expr = expr[2]
                    elem_c_type = self.to_c_type(elem_type_expr)
                    return f"List[{elem_c_type}]"

                # Map construction: (map-new arena KeyType ValueType) -> slop_map_X_Y
                if op == 'map-new' and len(expr) >= 4:
                    key_type_expr = expr[2]
                    value_type_expr = expr[3]
                    key_c_type = self.to_c_type(key_type_expr)
                    value_c_type = self.to_c_type(value_type_expr)
                    key_id = self._type_to_identifier(key_c_type)
                    value_id = self._type_to_identifier(value_c_type)
                    if isinstance(key_type_expr, Symbol) and key_type_expr.name == 'String':
                        return f"slop_map_string_{value_id}"
                    return f"slop_map_{key_id}_{value_id}"

                # Map get: (map-get map key) -> Option[value_type]
                if op == 'map-get' and len(expr) >= 3:
                    map_expr = expr[1]
                    map_type = self._infer_type(map_expr)
                    if map_type:
                        # Extract value type from map type: slop_map_string_X -> X
                        if map_type.startswith('slop_map_string_'):
                            value_id = map_type[len('slop_map_string_'):]
                            return f"slop_option_{value_id}"
                        elif map_type.startswith('slop_map_'):
                            # slop_map_K_V -> V
                            parts = map_type[len('slop_map_'):].split('_', 1)
                            if len(parts) == 2:
                                value_id = parts[1]
                                return f"slop_option_{value_id}"
                    return None

                # Field access: (. obj field) -> field's type
                if op == '.' and len(expr) >= 3:
                    base = expr[1]
                    field = expr[2]
                    if isinstance(field, Symbol):
                        # Get the base object's type
                        base_type = self._infer_type(base)
                        if base_type:
                            # Look up the field type
                            field_type = self._get_type_field(base_type, field.name)
                            if field_type:
                                # For list types, return List[elem_c_type] format
                                if is_form(field_type, 'List') and len(field_type) >= 2:
                                    elem_c_type = self.to_c_type(field_type[1])
                                    return f"List[{elem_c_type}]"
                                return self.to_c_type(field_type)
                    return None

        return None

    def _is_string_expr(self, expr: SExpr) -> bool:
        """Check if expression is a string type"""
        if isinstance(expr, String):
            return True
        if isinstance(expr, Symbol):
            name = expr.name
            # Parameters with String type would need type tracking
            # For now, check common patterns
            if name in ('path', 'body', 'customer', 'name', 'message', 'msg'):
                return True
            return False
        if isinstance(expr, SList) and len(expr) > 0:
            head = expr[0]
            if isinstance(head, Symbol):
                # String-returning functions
                if head.name in ('string-concat', 'int-to-string', 'format-invoice',
                                 'string-eq', '.'):
                    # For field access, check if field name suggests string
                    if head.name == '.' and len(expr) >= 3:
                        field = expr[2]
                        if isinstance(field, Symbol):
                            if field.name in ('path', 'body', 'customer', 'name', 'message'):
                                return True
                    elif head.name != '.':
                        return True
        return False

    def transpile(self, ast: List[SExpr]) -> str:
        """Transpile SLOP AST to C code.

        Raises UnfilledHoleError if any holes remain in the AST.
        """
        self.output = []
        self.ffi_funcs = {}
        self.ffi_structs = {}
        self.ffi_struct_fields = {}
        self.ffi_includes = []

        # Check for unfilled holes - cannot transpile incomplete code
        all_holes = []
        for form in ast:
            all_holes.extend(find_holes(form))
        if all_holes:
            raise UnfilledHoleError(all_holes)

        # First pass: collect FFI declarations from all modules
        for form in ast:
            if is_form(form, 'module'):
                self._collect_ffi(form)
            elif is_form(form, 'ffi'):
                self._register_ffi(form)
            elif is_form(form, 'ffi-struct'):
                self._register_ffi_struct(form)

        # Header
        self.emit("/* Generated by SLOP transpiler */")

        # Emit FFI includes first (before runtime)
        for header in self.ffi_includes:
            self.emit(f"#include <{header}>")
        if self.ffi_includes:
            self.emit("")

        self.emit("#include \"slop_runtime.h\"")
        self.emit("#include <stdint.h>")
        self.emit("#include <stdbool.h>")
        self.emit("")

        # Collect types, constants, and functions separately
        types = []
        constants = []
        functions = []
        modules = []

        for form in ast:
            if is_form(form, 'module'):
                modules.append(form)
            elif is_form(form, 'const'):
                constants.append(form)
            elif is_form(form, 'type'):
                types.append(form)
            elif is_form(form, 'record'):
                types.append(form)
            elif is_form(form, 'enum'):
                types.append(form)
            elif is_form(form, 'fn'):
                functions.append(form)

        # Process modules (they have their own type/function handling)
        for form in modules:
            self.transpile_module(form)

        # Process constants first (before types, since types may reference them)
        for form in constants:
            self.transpile_const(form)

        # Pre-scan type definitions to discover needed generic types
        # This must happen BEFORE emitting struct definitions
        for form in types:
            self._scan_type_definition(form)

        # Pre-scan functions to discover needed generic types
        for form in functions:
            self._scan_function_types(form)

        # Emit generated Option/List/Result types BEFORE struct definitions
        self._emit_generated_types()

        # Process all type definitions
        for form in types:
            if is_form(form, 'type'):
                self.transpile_type(form)
            elif is_form(form, 'record'):
                name = form[1].name if len(form) > 1 and isinstance(form[1], Symbol) else "unnamed"
                self.transpile_record(name, form)
            elif is_form(form, 'enum'):
                name = form[1].name if len(form) > 1 and isinstance(form[1], Symbol) else "unnamed"
                self.transpile_enum(name, form)

        # Process all functions
        for form in functions:
            self.transpile_function(form)

        return '\n'.join(self.output)

    def _collect_ffi(self, form: SList):
        """Collect FFI declarations from module"""
        for item in form.items[2:]:
            if is_form(item, 'ffi'):
                self._register_ffi(item)
            elif is_form(item, 'ffi-struct'):
                self._register_ffi_struct(item)

    def _register_ffi(self, form: SList):
        """Register FFI declarations: (ffi "header.h" (decl...) ...)

        Functions: (func-name ((param Type)...) ReturnType) - decl[1] is SList
        Constants: (const-name Type) - decl[1] is Symbol (no code emitted)
        """
        if len(form) < 2:
            return

        header = form[1].value if isinstance(form[1], String) else None
        if header and header not in self.ffi_includes:
            self.ffi_includes.append(header)

        # Register each declaration
        for decl in form.items[2:]:
            if isinstance(decl, SList) and len(decl) >= 2:
                name = decl[0].name
                second = decl[1]

                if isinstance(second, SList):
                    # Function: (func-name ((param Type)...) ReturnType)
                    params = second
                    return_type = decl[2] if len(decl) > 2 else Symbol('Void')
                    self.ffi_funcs[name] = {
                        'c_name': name.replace('-', '_'),
                        'params': params,
                        'return_type': return_type
                    }
                # else: Constant - no code emitted, symbol passes through to C

    def _register_ffi_struct(self, form: SList, inline: bool = False):
        """Register FFI struct: (ffi-struct "header.h" name (field type) ...)"""
        if len(form) < 2:
            return

        # Determine if first arg is header or name
        if isinstance(form[1], String):
            header = form[1].value
            name = form[2].name if len(form) > 2 else None
            fields_start = 3
        else:
            header = None
            name = form[1].name
            fields_start = 2

        # Check for :c-name keyword argument
        c_name_override = None
        if fields_start < len(form):
            for i in range(fields_start, len(form)):
                item = form[i]
                if isinstance(item, Symbol) and item.name == ':c-name':
                    if i + 1 < len(form) and isinstance(form[i + 1], String):
                        c_name_override = form[i + 1].value
                        # Skip :c-name and its value when processing fields
                        fields_start = i + 2
                    break
                # Stop if we hit a field (SList)
                if isinstance(item, SList):
                    break

        if header and header not in self.ffi_includes:
            self.ffi_includes.append(header)

        if name:
            fields = []
            self.ffi_struct_fields[name] = {}

            for f in form.items[fields_start:]:
                if isinstance(f, SList) and len(f) >= 2:
                    field_name = f[0].name
                    field_type = f[1]
                    fields.append((field_name, field_type))

                    # Build field type info for nested access
                    if isinstance(field_type, SList) and len(field_type) >= 1:
                        type_head = field_type[0].name if isinstance(field_type[0], Symbol) else None
                        if type_head == 'ffi-struct':
                            # Inline nested struct - register recursively
                            nested_name = field_type[1].name
                            self._register_ffi_struct(field_type, inline=True)
                            self.ffi_struct_fields[name][field_name] = {
                                'type': nested_name,
                                'is_pointer': False,
                                'is_struct': True
                            }
                        elif type_head == 'Ptr':
                            # Pointer field
                            inner = field_type[1].name if len(field_type) > 1 and isinstance(field_type[1], Symbol) else 'void'
                            self.ffi_struct_fields[name][field_name] = {
                                'type': inner,
                                'is_pointer': True,
                                'is_struct': False  # Will update later if needed
                            }
                        else:
                            # Other complex type (Array, etc)
                            self.ffi_struct_fields[name][field_name] = {
                                'type': str(field_type),
                                'is_pointer': False,
                                'is_struct': False
                            }
                    elif isinstance(field_type, Symbol):
                        # Simple type (U16, U32, etc)
                        self.ffi_struct_fields[name][field_name] = {
                            'type': field_type.name,
                            'is_pointer': False,
                            'is_struct': field_type.name in self.ffi_structs
                        }

            # Use :c-name override if provided, otherwise use heuristic
            # Heuristic: types ending in _t are usually typedefs (no struct prefix)
            # Other types are usually C structs (need struct prefix)
            if c_name_override:
                # Apply same heuristic to the override
                if c_name_override.endswith('_t'):
                    c_name = c_name_override
                else:
                    c_name = f"struct {c_name_override}"
            elif name.endswith('_t'):
                c_name = name
            else:
                c_name = f"struct {name}"
            self.ffi_structs[name] = {
                'c_name': c_name,
                'fields': fields
            }
            # Register as known type
            self.types[name] = TypeInfo(name, c_name)

    def _record_uses_generated_types(self, form: SList) -> bool:
        """Check if a record type uses generated types (List, Map, Option, Result) as fields.

        These records need to be emitted AFTER generated types.
        """
        if not is_form(form, 'type') or len(form) < 3:
            return False

        type_expr = form[2]
        if not is_form(type_expr, 'record'):
            return False

        generated_type_heads = {'List', 'Map', 'Option', 'Result'}

        for field in type_expr.items[1:]:
            if isinstance(field, SList) and len(field) >= 2:
                field_type = field[1]
                if isinstance(field_type, SList) and len(field_type) >= 1:
                    head = field_type[0]
                    if isinstance(head, Symbol) and head.name in generated_type_heads:
                        return True
        return False

    def transpile_module(self, form: SList):
        """Transpile module definition"""
        module_name = form[1].name if len(form) > 1 else "main"
        self.emit(f"/* Module: {module_name} */")
        self.emit("")

        # Collect constants, types and functions
        constants = []
        types = []
        functions = []
        for item in form.items[2:]:
            if is_form(item, 'export'):
                continue  # Skip exports, handled at link time
            elif is_form(item, 'import'):
                continue  # Skip imports, handled at link time
            elif is_form(item, 'ffi') or is_form(item, 'ffi-struct'):
                continue  # Already processed in first pass
            elif is_form(item, 'const'):
                constants.append(item)
            elif is_form(item, 'type'):
                types.append(item)
            elif is_form(item, 'fn'):
                functions.append(item)

        # Emit constants first (before types, since types may reference them)
        for c in constants:
            self.transpile_const(c)
        if constants:
            self.emit("")

        # Emit forward declarations for record types (needed for mutual references)
        record_types = []
        enum_types = []
        other_types = []  # Range types, aliases, etc.
        for t in types:
            if len(t) > 2:
                type_expr = t[2]
                if is_form(type_expr, 'record'):
                    type_name = t[1].name
                    record_types.append(t)
                    self.emit(f"typedef struct {type_name} {type_name};")
                elif is_form(type_expr, 'enum'):
                    enum_types.append(t)
                else:
                    other_types.append(t)
        if record_types:
            self.emit("")

        # Emit enum types FIRST (they may be referenced in generated result types)
        for t in enum_types:
            self.transpile_type(t)

        # Emit other types (range types, aliases) BEFORE generated types
        for t in other_types:
            self.transpile_type(t)

        # Split records: simple ones (no generated types) first, complex ones (with List/Map/Option) later
        simple_records = []
        complex_records = []
        for t in record_types:
            if self._record_uses_generated_types(t):
                complex_records.append(t)
            else:
                simple_records.append(t)

        # Emit simple record types (e.g., Pet, NewPet - used by generated types)
        for t in simple_records:
            self.transpile_type(t)

        # Pre-scan types and functions to discover needed generic types
        for t in types:
            self._scan_type_definition(t)
        for fn in functions:
            self._scan_function_types(fn)

        # Emit generated Option/List/Result types AFTER simple records
        self._emit_generated_types()

        # Emit complex record types (e.g., State - uses generated types)
        for t in complex_records:
            self.transpile_type(t)

        # Pre-pass for functions: track which return pointers
        for fn in functions:
            raw_name = fn[1].name
            ret_type_expr = self._get_return_type_expr(fn)
            if ret_type_expr and self._is_pointer_type(ret_type_expr):
                self.func_returns_pointer[raw_name] = True

        # Second pass: emit forward declarations for functions
        if functions:
            self.emit("/* Forward declarations */")
            for fn in functions:
                self.emit_forward_declaration(fn)
            self.emit("")

        # Third pass: emit function bodies
        for fn in functions:
            self.transpile_function(fn)

    def emit_forward_declaration(self, form: SList):
        """Emit function forward declaration"""
        raw_name = form[1].name
        name = self.to_qualified_c_name(raw_name)
        params = form[2] if len(form) > 2 else SList([])
        return_type = self._get_return_type(form)

        # C requires main to return int
        if raw_name == 'main':
            return_type = 'int'

        param_strs = []
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                pname = self.to_c_name(p[0].name)
                ptype = self.to_c_type(p[1])
                param_strs.append(f"{ptype} {pname}")

        self.emit(f"{return_type} {name}({', '.join(param_strs) or 'void'});")

    def _get_return_type(self, form: SList) -> str:
        """Extract return type from function form"""
        for item in form.items[3:]:
            if is_form(item, '@spec'):
                spec = item[1] if len(item) > 1 else None
                if spec and isinstance(spec, SList) and len(spec) >= 3:
                    return self.to_c_type(spec[-1])
        return 'void'

    def _get_return_type_expr(self, form: SList) -> SExpr:
        """Extract return type expression from function form (for pointer detection)"""
        for item in form.items[3:]:
            if is_form(item, '@spec'):
                spec = item[1] if len(item) > 1 else None
                if spec and isinstance(spec, SList) and len(spec) >= 3:
                    return spec[-1]
        return None

    def transpile_const(self, form: SList):
        """Transpile constant definition: (const NAME Type value)

        Integers emit as #define, others as static const.
        """
        # Validate arity: (const NAME TYPE VALUE) = 4 elements
        if len(form) != 4:
            raise TranspileError(
                f"const requires exactly 3 arguments (name, type, value), got {len(form) - 1}",
                form
            )

        name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
        type_expr = form[2]
        value_expr = form[3]

        c_name = self.to_c_name(name)
        c_type = self.to_c_type(type_expr)
        c_value = self._eval_const_expr(value_expr)

        # Check if it's an integer type for #define
        type_name = type_expr.name if isinstance(type_expr, Symbol) else None
        is_int_type = type_name in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64')

        if is_int_type:
            self.emit(f"#define {c_name} ({c_value})")
        elif type_name == 'String' and isinstance(value_expr, String):
            # String constants need SLOP_STR() wrapper to initialize slop_string struct
            self.emit(f"static const {c_type} {c_name} = SLOP_STR({c_value});")
        else:
            self.emit(f"static const {c_type} {c_name} = {c_value};")

    def _eval_const_expr(self, expr: SExpr) -> str:
        """Evaluate a constant expression to C code.

        Handles: literals, other constants, arithmetic/bitwise ops, sizeof.
        """
        if isinstance(expr, Number):
            return str(expr.value)

        if isinstance(expr, String):
            # Escape the string properly
            escaped = expr.value.replace('\\', '\\\\').replace('"', '\\"')
            return f'"{escaped}"'

        if isinstance(expr, Symbol):
            name = expr.name
            if name == 'true':
                return '1'
            elif name == 'false':
                return '0'
            else:
                # Reference to another constant
                return self.to_c_name(name)

        if isinstance(expr, SList) and len(expr) >= 1:
            head = expr[0]
            if isinstance(head, Symbol):
                op = head.name

                # sizeof
                if op == 'sizeof' and len(expr) >= 2:
                    type_arg = expr[1]
                    c_type = self.to_c_type(type_arg)
                    return f"sizeof({c_type})"

                # Binary arithmetic/bitwise ops
                if op in ('+', '-', '*', '/', '%', '&', '|', '^', '<<', '>>') and len(expr) >= 3:
                    left = self._eval_const_expr(expr[1])
                    right = self._eval_const_expr(expr[2])
                    return f"({left} {op} {right})"

                # Unary not
                if op == 'not' and len(expr) >= 2:
                    arg = self._eval_const_expr(expr[1])
                    return f"(!{arg})"

                # Unary minus (- x)
                if op == '-' and len(expr) == 2:
                    arg = self._eval_const_expr(expr[1])
                    return f"(-{arg})"

        # Fallback: stringify
        return str(expr)

    def transpile_type(self, form: SList):
        """Transpile type definition"""
        raw_name = form[1].name
        qualified_name = self.to_qualified_type_name(raw_name)
        type_expr = form[2]

        if is_form(type_expr, 'record'):
            self.transpile_record(raw_name, qualified_name, type_expr)
        elif is_form(type_expr, 'enum'):
            # Check if this is actually a tagged union (enum with payload variants)
            # e.g., (enum (literal String) (param String)) has SList variants
            has_payloads = any(isinstance(v, SList) for v in type_expr.items[1:])
            if has_payloads:
                self.transpile_union(raw_name, qualified_name, type_expr)
            else:
                self.transpile_enum(raw_name, qualified_name, type_expr)
        elif is_form(type_expr, 'union'):
            self.transpile_union(raw_name, qualified_name, type_expr)
        else:
            # Range type or alias
            self.transpile_range_type(raw_name, qualified_name, type_expr)

    def transpile_record(self, raw_name: str, qualified_name: str, form: SList):
        """Transpile record to C struct

        Handles both syntaxes:
        - Wrapped: (record (field Type) ...) - called from (type Name (record ...))
        - Bare: (record Name (field Type) ...) - top-level form
        """
        # Use struct tag name so it works with forward declarations
        self.emit(f"struct {qualified_name} {{")
        self.indent += 1

        # Track field types for pointer detection
        self.record_fields[raw_name] = {}

        # Track ScopedPtr fields for destructor generation
        scoped_fields = []

        # Determine start index: skip name if bare form
        start_idx = 1
        if len(form) > 1 and isinstance(form[1], Symbol):
            # First element is a symbol (the name in bare form), skip it
            start_idx = 2

        for field in form.items[start_idx:]:
            if isinstance(field, SList) and len(field) >= 2:
                raw_field_name = field[0].name
                field_name = self.to_c_name(raw_field_name)
                field_type_expr = field[1]
                field_type = self.to_c_type(field_type_expr)
                self.emit(f"{field_type} {field_name};")
                # Track if this field is a pointer
                self.record_fields[raw_name][raw_field_name] = {
                    'is_pointer': self._is_pointer_type(field_type_expr),
                    'type': field_type_expr
                }
                # Track ScopedPtr fields
                if self._is_scoped_ptr_type(field_type_expr):
                    scoped_fields.append((raw_field_name, field_type_expr))

        self.indent -= 1
        self.emit(f"}};")
        # Add typedef so we can use just 'Name' instead of 'struct Name'
        self.emit(f"typedef struct {qualified_name} {qualified_name};")
        self.emit("")

        # Track with raw name for lookup, but store qualified C type
        self.types[raw_name] = TypeInfo(raw_name, qualified_name)

        # Generate destructor if record has ScopedPtr fields
        if scoped_fields:
            self.records_needing_destructor[qualified_name] = scoped_fields
            self._generate_record_destructor(qualified_name, scoped_fields)

    def _generate_record_destructor(self, type_name: str, scoped_fields: list):
        """Generate destructor function for record with ScopedPtr fields"""
        if type_name in self.generated_destructors:
            return
        self.generated_destructors.add(type_name)

        self.emit(f"void {type_name}_free({type_name}* ptr) {{")
        self.indent += 1
        self.emit("if (!ptr) return;")

        for raw_field_name, field_type_expr in scoped_fields:
            c_field = self.to_c_name(raw_field_name)
            # Get the pointee type to check if it needs a destructor
            pointee_type = self._get_scoped_pointee_type(field_type_expr)
            # Check if pointee has its own destructor
            if pointee_type.rstrip('*') in self.records_needing_destructor:
                self.emit(f"if (ptr->{c_field}) {pointee_type.rstrip('*')}_free(ptr->{c_field});")
            else:
                self.emit(f"if (ptr->{c_field}) free(ptr->{c_field});")

        self.emit("free(ptr);")
        self.indent -= 1
        self.emit("}")
        self.emit("")

    def transpile_enum(self, raw_name: str, qualified_name: str, form: SList):
        """Transpile enum

        Handles both syntaxes:
        - Wrapped: (enum val1 val2 ...) - called from (type Name (enum ...))
        - Bare: (enum Name val1 val2 ...) - top-level form
        """
        self.emit(f"typedef enum {{")
        self.indent += 1

        # Determine start index: skip name if bare form
        start_idx = 1
        if len(form) > 1 and isinstance(form[1], Symbol) and form[1].name == raw_name:
            # First element is the name in bare form, skip it
            start_idx = 2

        values = form.items[start_idx:]
        for i, val in enumerate(values):
            if not isinstance(val, Symbol):
                continue
            val_c_name = self.to_c_name(val.name)
            qualified_variant = f"{qualified_name}_{val_c_name}"
            comma = "," if i < len(values) - 1 else ""
            self.emit(f"{qualified_variant}{comma}")
            # Store enum value for lookup
            self.enums[val.name] = qualified_variant
            self.enums[val_c_name] = qualified_variant

        self.indent -= 1
        self.emit(f"}} {qualified_name};")
        self.emit("")

        # Track with raw name for lookup, but store qualified C type
        self.types[raw_name] = TypeInfo(raw_name, qualified_name)
        self.simple_enums.add(raw_name)

    def transpile_union(self, raw_name: str, qualified_name: str, form: SList):
        """Transpile tagged union"""
        self.emit(f"typedef struct {{")
        self.indent += 1
        self.emit("uint8_t tag;")
        self.emit("union {")
        self.indent += 1

        for i, variant in enumerate(form.items[1:]):
            if isinstance(variant, SList) and len(variant) >= 1:
                tag = variant[0].name
                if len(variant) >= 2:
                    var_type = self.to_c_type(variant[1])
                    self.emit(f"{var_type} {tag};")
                # else: empty variant, no field needed

        self.indent -= 1
        self.emit("} data;")
        self.indent -= 1
        self.emit(f"}} {qualified_name};")
        self.emit("")

        # Generate tag constants with qualified name
        for i, variant in enumerate(form.items[1:]):
            if isinstance(variant, SList):
                tag = variant[0].name
                self.emit(f"#define {qualified_name}_{tag}_TAG {i}")
        self.emit("")

        # Track with raw name for lookup, but store qualified C type
        self.types[raw_name] = TypeInfo(raw_name, qualified_name)

    def transpile_range_type(self, raw_name: str, qualified_name: str, type_expr: SExpr):
        """Transpile range type to typedef + constructor"""
        min_val, max_val = None, None
        base_type = 'int64_t'

        if isinstance(type_expr, SList):
            head = type_expr[0].name if isinstance(type_expr[0], Symbol) else ''

            # Handle Array type: (Array T size)
            if head == 'Array' and len(type_expr) >= 3:
                inner_type = self.to_c_type(type_expr[1])
                size = int(type_expr[2].value) if isinstance(type_expr[2], Number) else 100
                # For array types, create a typedef to the array
                self.emit(f"typedef {inner_type} {qualified_name}[{size}];")
                self.emit("")
                self.types[raw_name] = TypeInfo(raw_name, f"{inner_type}*", is_array=True)
                return

            # Parse (Int min .. max) or similar
            if len(type_expr) >= 1:
                base = type_expr[0].name
                base_type = self.builtin_types.get(base, 'int64_t')

            # Find range bounds
            for i, item in enumerate(type_expr.items[1:]):
                if isinstance(item, Number):
                    if min_val is None:
                        min_val = int(item.value)
                    else:
                        max_val = int(item.value)
                elif isinstance(item, Symbol) and item.name == '..':
                    continue
        elif isinstance(type_expr, Symbol):
            # Simple alias
            base_type = self.builtin_types.get(type_expr.name, type_expr.name)

        # Choose smallest C type that fits
        if min_val is not None and max_val is not None:
            if min_val >= 0 and max_val <= 255:
                base_type = 'uint8_t'
            elif min_val >= 0 and max_val <= 65535:
                base_type = 'uint16_t'
            elif min_val >= -128 and max_val <= 127:
                base_type = 'int8_t'
            elif min_val >= -32768 and max_val <= 32767:
                base_type = 'int16_t'

        # Emit typedef (simple typedef, not wrapped struct)
        self.emit(f"typedef {base_type} {qualified_name};")
        self.emit("")

        # Emit constructor with range check
        self.emit(f"static inline {qualified_name} {qualified_name}_new(int64_t v) {{")
        self.indent += 1

        if min_val is not None and max_val is not None:
            self.emit(f'SLOP_PRE(v >= {min_val} && v <= {max_val}, "{qualified_name} in range {min_val}..{max_val}");')
        elif min_val is not None:
            self.emit(f'SLOP_PRE(v >= {min_val}, "{qualified_name} >= {min_val}");')
        elif max_val is not None:
            self.emit(f'SLOP_PRE(v <= {max_val}, "{qualified_name} <= {max_val}");')

        self.emit(f"return ({qualified_name})v;")
        self.indent -= 1
        self.emit("}")
        self.emit("")

        self.types[raw_name] = TypeInfo(raw_name, qualified_name, True, min_val, max_val)

    def _parse_range_bounds(self, type_expr: SList) -> tuple:
        """Parse range bounds from inline range type like (Int 1 ..) or (Int 0 .. 100).

        Returns (min_val, max_val) tuple, with None for unbounded sides.
        """
        min_val, max_val = None, None
        for item in type_expr.items[1:]:
            if isinstance(item, Number):
                if min_val is None:
                    min_val = int(item.value)
                else:
                    max_val = int(item.value)
            elif isinstance(item, Symbol) and item.name == '..':
                continue
        return (min_val, max_val)

    def transpile_function(self, form: SList):
        """Transpile function definition"""
        raw_name = form[1].name
        name = self.to_qualified_c_name(raw_name)
        self.local_functions.add(raw_name)  # Track for local call resolution
        params = form[2] if len(form) > 2 else SList([])

        # Clear pointer and type tracking for this function scope
        self.pointer_vars = set()
        self.var_types = {}

        # Register parameter types and track pointers (mode-aware)
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                mode, pname, ptype = self._parse_parameter_mode(p)
                if pname and ptype:
                    # Track type for type flow analysis
                    type_name = self._get_type_name(ptype)
                    if type_name:
                        self.var_types[pname] = type_name
                    # Track pointers: explicit Ptr types, or out/mut modes
                    if self._is_pointer_type(ptype) or mode in ('out', 'mut'):
                        self.pointer_vars.add(pname)

        # Track if this function returns a pointer
        ret_type_expr = self._get_return_type_expr(form)
        if ret_type_expr and self._is_pointer_type(ret_type_expr):
            self.func_returns_pointer[raw_name] = True

        # Track current return type for context (used by ok/error/record)
        self.current_return_type = ret_type_expr

        # Extract annotations and body
        annotations = {}
        body_exprs = []

        for item in form.items[3:]:
            if is_form(item, '@intent'):
                annotations['intent'] = item[1].value if len(item) > 1 else ""
            elif is_form(item, '@spec'):
                annotations['spec'] = item[1] if len(item) > 1 else None
            elif is_form(item, '@pre'):
                annotations.setdefault('pre', []).append(item[1] if len(item) > 1 else None)
            elif is_form(item, '@post'):
                annotations.setdefault('post', []).append(item[1] if len(item) > 1 else None)
            elif is_form(item, '@alloc'):
                annotations['alloc'] = item[1].name if len(item) > 1 else None
            elif is_form(item, '@example'):
                pass  # Examples are for documentation/testing, not compiled
            elif is_form(item, '@pure'):
                pass  # Purity annotation, not compiled
            else:
                body_exprs.append(item)

        # Determine return type from @spec
        return_type = 'void'
        if 'spec' in annotations and annotations['spec']:
            spec = annotations['spec']
            # spec is ((ParamTypes) -> ReturnType)
            if isinstance(spec, SList) and len(spec) >= 3:
                return_type = self.to_c_type(spec[-1])

        # C requires main to return int
        if name == 'main':
            return_type = 'int'

        # Emit function comment
        if 'intent' in annotations:
            self.emit(f"/* {annotations['intent']} */")

        # Emit function signature and track pointer parameters (mode-aware)
        param_strs = []
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                mode, raw_pname, ptype_expr = self._parse_parameter_mode(p)
                if raw_pname and ptype_expr:
                    pname = self.to_c_name(raw_pname)
                    ptype = self.to_c_type(ptype_expr)
                    # Apply mode transformation
                    ptype = self._apply_parameter_mode(mode, ptype)
                    param_strs.append(f"{ptype} {pname}")
                    # Track pointer parameters (including out/mut)
                    if self._is_pointer_type(ptype_expr) or mode in ('out', 'mut'):
                        self.pointer_vars.add(raw_pname)

        self.emit(f"{return_type} {name}({', '.join(param_strs) or 'void'}) {{")
        self.indent += 1

        # Emit preconditions
        for pre in annotations.get('pre', []):
            if pre:
                cond = self.transpile_expr(pre)
                self.emit(f'SLOP_PRE({cond}, "{self.expr_to_str(pre)}");')

        # Check if we have postconditions
        has_post = bool(annotations.get('post'))
        needs_retval = has_post and return_type != 'void'

        # Declare return value variable if needed for postconditions
        if needs_retval:
            self.emit(f"{return_type} _retval;")

        # Emit body
        if body_exprs:
            for i, expr in enumerate(body_exprs):
                is_last = (i == len(body_exprs) - 1)
                if is_last and needs_retval:
                    # Capture final expression into _retval instead of returning
                    # Check if it's a statement-like form that needs special handling
                    if is_form(expr, 'let') or is_form(expr, 'let*'):
                        self.transpile_let_with_capture(expr, '_retval')
                    elif is_form(expr, 'if'):
                        self.transpile_if_with_capture(expr, '_retval')
                    elif is_form(expr, 'do'):
                        # Emit all but last, then capture last
                        for item in expr.items[1:-1]:
                            self.transpile_statement(item, False)
                        if len(expr.items) > 1:
                            last = expr.items[-1]
                            if is_form(last, 'let') or is_form(last, 'let*'):
                                self.transpile_let_with_capture(last, '_retval')
                            elif is_form(last, 'if'):
                                self.transpile_if_with_capture(last, '_retval')
                            else:
                                code = self.transpile_expr(last)
                                self.emit(f"_retval = {code};")
                    else:
                        code = self.transpile_expr(expr)
                        self.emit(f"_retval = {code};")
                else:
                    self.transpile_statement(expr, is_last and return_type != 'void' and not has_post)

        # Emit postconditions
        for post in annotations.get('post', []):
            if post:
                cond = self.transpile_expr(post)
                # Replace $result with _retval
                cond = cond.replace('_result', '_retval')
                self.emit(f'SLOP_POST({cond}, "{self.expr_to_str(post)}");')

        # Emit return after postconditions
        if needs_retval:
            self.emit("return _retval;")

        self.indent -= 1
        self.emit("}")
        self.emit("")

    def transpile_statement(self, expr: SExpr, is_return: bool = False):
        """Transpile a statement"""
        if is_form(expr, 'let'):
            self.transpile_let(expr, is_return)
        elif is_form(expr, 'let*'):
            self.transpile_let(expr, is_return)  # Same as let in C
        elif is_form(expr, 'if'):
            self.transpile_if(expr, is_return)
        elif is_form(expr, 'when'):
            self.transpile_when(expr)
        elif is_form(expr, 'set!'):
            self.transpile_set(expr)
        elif is_form(expr, 'do'):
            for item in expr.items[1:]:
                is_last = (item == expr.items[-1])
                self.transpile_statement(item, is_return and is_last)
        elif is_form(expr, 'while'):
            self.transpile_while(expr)
        elif is_form(expr, 'for'):
            self.transpile_for(expr)
        elif is_form(expr, 'for-each'):
            self.transpile_for_each(expr)
        elif is_form(expr, 'match'):
            self.transpile_match(expr, is_return)
        elif is_form(expr, 'cond'):
            self.transpile_cond(expr, is_return)
        elif is_form(expr, 'return'):
            val = self.transpile_expr(expr[1]) if len(expr) > 1 else ""
            self.emit(f"return {val};" if val else "return;")
        elif is_form(expr, 'break'):
            self.emit("break;")
        elif is_form(expr, 'continue'):
            self.emit("continue;")
        elif is_form(expr, 'try'):
            self.transpile_try(expr, is_return)
        elif is_form(expr, 'with-arena'):
            self.transpile_with_arena(expr, is_return)
        else:
            # Pass expected type for return expressions so Option/Result constructors work
            expected = self.current_return_type if is_return else None
            code = self.transpile_expr(expr, expected)
            if is_return:
                self.emit(f"return {code};")
            else:
                self.emit(f"{code};")

    def transpile_let(self, expr: SList, is_return: bool):
        """Transpile let binding"""
        bindings = expr[1]
        body = expr.items[2:]

        # Push scope for ScopedPtr tracking
        self._push_scoped_scope()

        # Emit bindings
        for binding in bindings:
            raw_name = binding[0].name
            var_name = self.to_c_name(raw_name)

            # Handle typed bindings: (name Type init) vs untyped: (name init)
            if len(binding) >= 3:
                # Typed binding: (name Type init)
                type_expr = binding[1]
                init_expr = binding[2]

                # Check if this is a ScopedPtr binding
                if self._is_scoped_ptr_type(type_expr):
                    pointee_c_type = self._get_scoped_pointee_type(type_expr)
                    self._register_scoped(raw_name, pointee_c_type)
            else:
                # Untyped binding: (name init)
                init_expr = binding[1]

            # Track variable type for type flow analysis
            inferred_type = self._infer_type(init_expr)
            if inferred_type:
                self.var_types[raw_name] = inferred_type

            # Register pointer variables
            if self._is_pointer_expr(init_expr):
                self.pointer_vars.add(raw_name)

            # Special case: (array Type) - need explicit type for array init
            if is_form(init_expr, 'array') and len(init_expr) == 2:
                type_arg = init_expr[1]
                if isinstance(type_arg, Symbol):
                    type_name = type_arg.name
                    # Check if it's a known array type alias
                    if type_name in self.types and self.types[type_name].is_array:
                        self.emit(f"{type_name} {var_name} = {{0}};")
                        self.var_types[raw_name] = type_name  # Track type
                        continue
                    # Otherwise use the type directly with a default size
                    c_type = self.to_c_type(type_arg)
                    self.emit(f"{c_type} {var_name}[100] = {{0}};")
                    self.var_types[raw_name] = type_name  # Track type
                    continue

            var_expr = self.transpile_expr(init_expr)
            # Skip binding for _ wildcards - just evaluate for side effects
            if raw_name == "_":
                self.emit(f"(void){var_expr};")
            else:
                # Type inference would go here; for now use auto
                self.emit(f"__auto_type {var_name} = {var_expr};")

        # Pop scope now to check if we need special return handling
        scoped = self._pop_scoped_scope()

        # If returning and have scoped vars, need to capture value before cleanup
        if is_return and scoped and body:
            # Emit all but last statement normally
            for item in body[:-1]:
                self.transpile_statement(item, False)

            # For last statement, capture into temp, cleanup, then return
            last_expr = body[-1]
            result_var = f"__scoped_result_{id(last_expr)}"
            result_code = self.transpile_expr(last_expr)
            self.emit(f"__auto_type {result_var} = {result_code};")
            self._emit_scoped_cleanup(scoped)
            self.emit(f"return {result_var};")
        else:
            # Normal case: emit body, then cleanup
            for i, item in enumerate(body):
                is_last = (i == len(body) - 1)
                self.transpile_statement(item, is_return and is_last)

            if scoped:
                self._emit_scoped_cleanup(scoped)

    def transpile_let_with_capture(self, expr: SList, capture_var: str):
        """Transpile let binding, capturing the final value in a variable"""
        bindings = expr[1]
        body = expr.items[2:]

        # Push scope for ScopedPtr tracking
        self._push_scoped_scope()

        # Emit bindings
        for binding in bindings:
            raw_name = binding[0].name
            var_name = self.to_c_name(raw_name)

            # Handle typed bindings: (name Type init) vs untyped: (name init)
            if len(binding) >= 3:
                type_expr = binding[1]
                init_expr = binding[2]

                # Check if this is a ScopedPtr binding
                if self._is_scoped_ptr_type(type_expr):
                    pointee_c_type = self._get_scoped_pointee_type(type_expr)
                    self._register_scoped(raw_name, pointee_c_type)
            else:
                init_expr = binding[1]

            # Register pointer variables
            if self._is_pointer_expr(init_expr):
                self.pointer_vars.add(raw_name)

            var_expr = self.transpile_expr(init_expr)
            # Skip binding for _ wildcards - just evaluate for side effects
            if raw_name == "_":
                self.emit(f"(void){var_expr};")
            else:
                self.emit(f"__auto_type {var_name} = {var_expr};")

        # Emit body, capturing the last expression
        for i, item in enumerate(body):
            is_last = (i == len(body) - 1)
            if is_last:
                # Capture the final value
                if is_form(item, 'let') or is_form(item, 'let*'):
                    self.transpile_let_with_capture(item, capture_var)
                elif is_form(item, 'if'):
                    self.transpile_if_with_capture(item, capture_var)
                else:
                    code = self.transpile_expr(item)
                    self.emit(f"{capture_var} = {code};")
            else:
                self.transpile_statement(item, False)

        # Pop scope and emit cleanup for ScopedPtr variables
        scoped = self._pop_scoped_scope()
        if scoped:
            self._emit_scoped_cleanup(scoped)

    def transpile_if(self, expr: SList, is_return: bool):
        """Transpile if expression"""
        cond = self.transpile_expr(expr[1])
        then_branch = expr[2]
        else_branch = expr[3] if len(expr) > 3 else None

        # Check if branches are simple expressions (can use ternary)
        def is_simple(e):
            if isinstance(e, (Number, String, Symbol)):
                return True
            if isinstance(e, SList) and len(e) > 0:
                head = e[0]
                if isinstance(head, Symbol):
                    # These are complex statements, not expressions
                    if head.name in ('do', 'let', 'let*', 'if', 'when', 'while',
                                     'for', 'for-each', 'match', 'cond', 'set!'):
                        return False
                return True
            return False

        if is_return and is_simple(then_branch) and (else_branch is None or is_simple(else_branch)):
            # Use ternary for simple returns - pass expected type for Option/Result
            expected = self.current_return_type
            then_expr = self.transpile_expr(then_branch, expected)
            else_expr = self.transpile_expr(else_branch, expected) if else_branch else "0"
            self.emit(f"return ({cond}) ? {then_expr} : {else_expr};")
        else:
            self.emit(f"if ({cond}) {{")
            self.indent += 1
            self.transpile_statement(then_branch, is_return)
            self.indent -= 1
            if else_branch:
                self.emit("} else {")
                self.indent += 1
                self.transpile_statement(else_branch, is_return)
                self.indent -= 1
            self.emit("}")

    def transpile_if_with_capture(self, expr: SList, capture_var: str):
        """Transpile if expression, capturing the result in a variable"""
        cond = self.transpile_expr(expr[1])
        then_branch = expr[2]
        else_branch = expr[3] if len(expr) > 3 else None

        def capture_branch(branch):
            if is_form(branch, 'let') or is_form(branch, 'let*'):
                self.transpile_let_with_capture(branch, capture_var)
            elif is_form(branch, 'if'):
                self.transpile_if_with_capture(branch, capture_var)
            else:
                code = self.transpile_expr(branch)
                self.emit(f"{capture_var} = {code};")

        self.emit(f"if ({cond}) {{")
        self.indent += 1
        capture_branch(then_branch)
        self.indent -= 1
        if else_branch:
            self.emit("} else {")
            self.indent += 1
            capture_branch(else_branch)
            self.indent -= 1
        self.emit("}")

    def transpile_when(self, expr: SList):
        """Transpile when (if without else)"""
        cond = self.transpile_expr(expr[1])
        self.emit(f"if ({cond}) {{")
        self.indent += 1
        for item in expr.items[2:]:
            self.transpile_statement(item, False)
        self.indent -= 1
        self.emit("}")

    def transpile_set(self, expr: SList):
        """Transpile set! mutation

        Forms:
        - (set! target field value) - field mutation: target->field = value
        - (set! var value) - simple assignment: *var = value or var = value
        """
        if len(expr) == 4:
            # (set! target field value) - field access
            target = expr[1]
            field = expr[2].name

            # Look up the field type to pass as expected_type
            expected_type = None
            if isinstance(target, Symbol):
                expected_type = self._get_field_type(target.name, field)
            elif is_form(target, '.') and len(target) == 3:
                # Target is a nested field access like (. queue tail)
                # Get the type of that expression, then look up the field on it
                inner_type = self._get_expr_type(target)
                if inner_type:
                    expected_type = self._get_type_field(inner_type, field)

            value = self.transpile_expr(expr[3], expected_type)
            target_code = self.transpile_expr(target)
            # Use -> for pointers, . for values
            if self._is_pointer_expr(target):
                self.emit(f"{target_code}->{self.to_c_name(field)} = {value};")
            else:
                self.emit(f"({target_code}).{self.to_c_name(field)} = {value};")
        elif len(expr) == 3:
            # (set! var value) - simple assignment
            target = expr[1]

            # Check if target is a field access expression: (. base field)
            if is_form(target, '.') and len(target) == 3:
                base = target[1]
                field = target[2]
                if isinstance(field, Symbol):
                    field_name = field.name
                    # Look up expected type for the field
                    expected_type = None
                    if isinstance(base, Symbol):
                        expected_type = self._get_field_type(base.name, field_name)
                    value = self.transpile_expr(expr[2], expected_type)
                    base_code = self.transpile_expr(base)
                    # Check if base is a pointer (use -> instead of .)
                    if isinstance(base, Symbol) and base.name in self.pointer_vars:
                        self.emit(f"{base_code}->{self.to_c_name(field_name)} = {value};")
                    else:
                        # Check if base expression is a pointer type
                        if self._is_pointer_expr(base):
                            self.emit(f"{base_code}->{self.to_c_name(field_name)} = {value};")
                        else:
                            self.emit(f"{base_code}.{self.to_c_name(field_name)} = {value};")
                    return

            value = self.transpile_expr(expr[2])

            # Check if target is a symbol with dot notation (e.g., addr.sin_addr.s_addr)
            if isinstance(target, Symbol) and '.' in target.name:
                parts = target.name.split('.')
                resolved = self._resolve_field_chain(parts)
                self.emit(f"{resolved} = {value};")
            # Check if target is an array access: (@ arr i)
            elif is_form(target, '@'):
                arr = self.transpile_expr(target[1])
                idx = self.transpile_expr(target[2])
                self.emit(f"{arr}[{idx}] = {value};")
            # Check if target is a deref: (deref ptr)
            elif is_form(target, 'deref'):
                ptr = self.transpile_expr(target[1])
                self.emit(f"(*{ptr}) = {value};")
            # Simple variable assignment
            else:
                target_code = self.transpile_expr(target)
                # No dereference - direct assignment
                self.emit(f"{target_code} = {value};")

    def transpile_while(self, expr: SList):
        """Transpile while loop"""
        cond = self.transpile_expr(expr[1])
        self.emit(f"while ({cond}) {{")
        self.indent += 1
        for item in expr.items[2:]:
            self.transpile_statement(item, False)
        self.indent -= 1
        self.emit("}")

    def transpile_for(self, expr: SList):
        """Transpile for loop: (for (i start end) body)"""
        binding = expr[1]
        var_name = self.to_c_name(binding[0].name)
        start = self.transpile_expr(binding[1])
        end = self.transpile_expr(binding[2])
        self.emit(f"for (int64_t {var_name} = {start}; {var_name} < {end}; {var_name}++) {{")
        self.indent += 1
        for item in expr.items[2:]:
            self.transpile_statement(item, False)
        self.indent -= 1
        self.emit("}")

    def transpile_for_each(self, expr: SList):
        """Transpile for-each loop: (for-each (item collection) body)"""
        binding = expr[1]
        var_name = self.to_c_name(binding[0].name)
        collection = self.transpile_expr(binding[1])
        self.emit(f"for (size_t _i = 0; _i < {collection}.len; _i++) {{")
        self.indent += 1
        self.emit(f"__auto_type {var_name} = {collection}.data[_i];")
        for item in expr.items[2:]:
            self.transpile_statement(item, False)
        self.indent -= 1
        self.emit("}")

    def transpile_match(self, expr: SList, is_return: bool):
        """Transpile match expression"""
        scrutinee = self.transpile_expr(expr[1])

        # Check for Option/Result match patterns (some/none/ok/error)
        # BUT first check if these are actually registered enum values
        is_option_match = False
        is_result_match = False
        first_tag = None

        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                tag = None
                if isinstance(pattern, Symbol):
                    tag = pattern.name
                elif isinstance(pattern, SList) and len(pattern) >= 1 and isinstance(pattern[0], Symbol):
                    tag = pattern[0].name

                if tag:
                    # Check if pattern is a list (ok val) or bare symbol ok
                    pattern_is_list = isinstance(pattern, SList)

                    # Option/Result patterns: only if the pattern is a list like (ok val) or (some val)
                    # Bare symbols like ok or error could be enum values
                    if pattern_is_list:
                        if tag in ('some', 'none'):
                            is_option_match = True
                            if first_tag is None:
                                first_tag = tag
                            break
                        elif tag in ('ok', 'error'):
                            is_result_match = True
                            if first_tag is None:
                                first_tag = tag
                            break

                    # Check if this tag is a registered enum value - if so, don't treat as Option/Result
                    unquoted_tag = self._unquote_symbol(tag)
                    if unquoted_tag in self.enums:
                        # This is an enum value, not Option/Result pattern
                        break

                    # Bare symbol none/some without being in a list - still treat as Option
                    if not pattern_is_list and tag in ('some', 'none'):
                        is_option_match = True
                        if first_tag is None:
                            first_tag = tag
                        break

        # Handle Option/Result matches with if-else (bool check)
        if is_option_match or is_result_match:
            self._transpile_option_result_match_stmt(scrutinee, expr[1], expr.items[2:], is_option_match, is_return)
            return

        # Determine if this is a simple enum match (only quoted symbols like 'Fizz)
        is_simple_enum = False
        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                # Check for enum values - both quoted ('ok) and unquoted (ok)
                if isinstance(pattern, Symbol):
                    unquoted = self._unquote_symbol(pattern.name)
                    if unquoted in self.enums:
                        is_simple_enum = True
                        break
                if isinstance(pattern, SList) and len(pattern) >= 1:
                    raw_tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    if raw_tag:
                        tag = self._unquote_symbol(raw_tag)
                        if tag in self.enums:
                            is_simple_enum = True
                            break

        if is_simple_enum:
            self.emit(f"switch ({scrutinee}) {{")
        else:
            self.emit(f"switch ({scrutinee}.tag) {{")
        self.indent += 1

        for i, clause in enumerate(expr.items[2:]):
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                body = clause.items[1:]

                if isinstance(pattern, Symbol):
                    if pattern.name == '_' or pattern.name == 'else':
                        # Wildcard/default case
                        self.emit("default: {")
                    else:
                        # Check for enum value - both quoted ('ok) and unquoted (ok)
                        unquoted = self._unquote_symbol(pattern.name)
                        if unquoted in self.enums:
                            enum_const = self.enums[unquoted]
                            self.emit(f"case {enum_const}: {{")
                        else:
                            # Unknown pattern, use index as fallback
                            self.emit(f"case {i}: {{")
                    self.indent += 1
                    for j, item in enumerate(body):
                        is_last = (j == len(body) - 1)
                        self.transpile_statement(item, is_return and is_last)
                    if not is_return:
                        self.emit("break;")
                    self.indent -= 1
                    self.emit("}")
                elif isinstance(pattern, SList) and len(pattern) >= 1:
                    # Pattern with binding like ('Number n) or (some x)
                    raw_tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    is_quoted_tag = raw_tag and raw_tag.startswith("'")
                    tag = self._unquote_symbol(raw_tag) if raw_tag else None
                    var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None

                    # Check for enum variant - allow both quoted ('ok) and unquoted (ok)
                    if is_simple_enum and tag in self.enums:
                        enum_const = self.enums[tag]
                        self.emit(f"case {enum_const}: {{")
                    else:
                        self.emit(f"case {i}: {{")
                    self.indent += 1
                    if var_name and not is_simple_enum:
                        self.emit(f"__auto_type {var_name} = {scrutinee}.data.{tag};")
                    for j, item in enumerate(body):
                        is_last = (j == len(body) - 1)
                        self.transpile_statement(item, is_return and is_last)
                    if not is_return:
                        self.emit("break;")
                    self.indent -= 1
                    self.emit("}")

        self.indent -= 1
        self.emit("}")

    def _transpile_option_result_match_stmt(self, scrutinee: str, scrutinee_expr: SExpr, clauses: list, is_option: bool, is_return: bool):
        """Transpile Option/Result match as statement using if-else."""
        # Infer type of scrutinee for tracking bound variables
        scrutinee_type = self._infer_type(scrutinee_expr)

        # Store scrutinee in temp variable to avoid evaluating side effects twice
        # (e.g., file reads should only happen once)
        self.emit(f"__auto_type _match_scrutinee = {scrutinee};")
        scrutinee = "_match_scrutinee"

        # Collect clauses
        some_ok_clause = None
        none_err_clause = None

        for clause in clauses:
            if not isinstance(clause, SList) or len(clause) < 2:
                continue

            pattern = clause[0]
            body = clause.items[1:]
            tag = None
            var_name = None

            if isinstance(pattern, SList) and len(pattern) >= 1:
                tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None
            elif isinstance(pattern, Symbol):
                tag = pattern.name

            if tag in ('some', 'ok'):
                some_ok_clause = (var_name, body, tag)
            elif tag in ('none', 'error'):
                none_err_clause = (var_name, body, tag)

        # Generate if-else
        check_field = f"{scrutinee}.has_value" if is_option else f"{scrutinee}.is_ok"

        if some_ok_clause:
            var_name, body, tag = some_ok_clause
            self.emit(f"if ({check_field}) {{")
            self.indent += 1
            if var_name and var_name != '_':
                if is_option:
                    self.emit(f"__auto_type {var_name} = {scrutinee}.value;")
                    # Track the type of the bound variable for type flow analysis
                    if scrutinee_type and scrutinee_type.startswith('slop_option_'):
                        # Extract inner type: slop_option_string -> slop_string
                        inner_type = 'slop_' + scrutinee_type[len('slop_option_'):]
                        self.var_types[var_name] = inner_type
                else:
                    self.emit(f"__auto_type {var_name} = {scrutinee}.data.ok;")
            for j, item in enumerate(body):
                is_last = (j == len(body) - 1)
                self.transpile_statement(item, is_return and is_last)
            self.indent -= 1

        if none_err_clause:
            var_name, body, tag = none_err_clause
            if some_ok_clause:
                self.emit("} else {")
            else:
                self.emit(f"if (!{check_field}) {{")
            self.indent += 1
            if var_name and var_name != '_' and not is_option:
                self.emit(f"__auto_type {var_name} = {scrutinee}.data.err;")
            for j, item in enumerate(body):
                is_last = (j == len(body) - 1)
                self.transpile_statement(item, is_return and is_last)
            self.indent -= 1
            self.emit("}")
        elif some_ok_clause:
            self.emit("}")

    def transpile_cond(self, expr: SList, is_return: bool):
        """Transpile cond expression"""
        first = True
        for clause in expr.items[1:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                test = clause[0]
                body = clause.items[1:]

                if isinstance(test, Symbol) and test.name == 'else':
                    self.emit("} else {")
                    self.indent += 1
                    for j, item in enumerate(body):
                        is_last = (j == len(body) - 1)
                        self.transpile_statement(item, is_return and is_last)
                    self.indent -= 1
                else:
                    cond = self.transpile_expr(test)
                    if first:
                        self.emit(f"if ({cond}) {{")
                        first = False
                    else:
                        self.emit(f"}} else if ({cond}) {{")
                    self.indent += 1
                    for j, item in enumerate(body):
                        is_last = (j == len(body) - 1)
                        self.transpile_statement(item, is_return and is_last)
                    self.indent -= 1
        self.emit("}")

    def transpile_try(self, expr: SList, is_return: bool):
        """Transpile try/catch: (try expr (catch pattern body))"""
        try_expr = self.transpile_expr(expr[1])
        self.emit("{")
        self.indent += 1
        self.emit(f"__auto_type _result = {try_expr};")
        self.emit("if (_result.tag != 0) {")
        self.indent += 1

        # Handle catch clause
        if len(expr) > 2 and is_form(expr[2], 'catch'):
            catch_clause = expr[2]
            pattern = catch_clause[1] if len(catch_clause) > 1 else None
            catch_body = catch_clause.items[2:]

            if pattern and isinstance(pattern, Symbol):
                var_name = self.to_c_name(pattern.name)
                self.emit(f"__auto_type {var_name} = _result.data.err;")

            for j, item in enumerate(catch_body):
                is_last = (j == len(catch_body) - 1)
                self.transpile_statement(item, is_return and is_last)

        self.indent -= 1
        self.emit("}")
        self.indent -= 1
        self.emit("}")

    def transpile_with_arena(self, expr: SList, is_return: bool):
        """Transpile with-arena: (with-arena size body)"""
        size = self.transpile_expr(expr[1])
        self.emit("{")
        self.indent += 1
        self.emit(f"slop_arena _arena = slop_arena_new({size});")
        self.emit(f"slop_arena* arena = &_arena;")

        for j, item in enumerate(expr.items[2:]):
            is_last = (j == len(expr.items) - 3)
            self.transpile_statement(item, is_return and is_last)

        self.emit("slop_arena_free(arena);")
        self.indent -= 1
        self.emit("}")

    def transpile_expr(self, expr: SExpr, expected_type: Optional[SExpr] = None) -> str:
        """Transpile expression to C

        Args:
            expr: The expression to transpile
            expected_type: If provided, the expected type for type-aware constructs (none, some, list-new, etc.)
        """
        if isinstance(expr, Number):
            return str(expr.value)

        if isinstance(expr, String):
            # Escape the string for C output
            escaped = expr.value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
            return f'SLOP_STR("{escaped}")'

        if isinstance(expr, Symbol):
            name = expr.name
            if name == 'nil':
                return 'NULL'
            if name == 'true':
                return '1'
            if name == 'false':
                return '0'
            # Handle 'none' as Option constructor (requires expected_type)
            if name == 'none':
                option_type = self._get_option_c_type(expected_type)
                if option_type:
                    return f"(({option_type}){{ .has_value = false }})"
                # Fallback for unknown type context
                return "(slop_option_ptr){ .has_value = false }"
            if name.startswith("'"):
                # Enum value - look up qualified name
                enum_val = name[1:]
                if enum_val in self.enums:
                    return self.enums[enum_val]
                return self.to_c_name(enum_val)
            # Check if bare identifier is a known enum value
            if name in self.enums:
                return self.enums[name]
            # Handle dot notation for field access (e.g., resp.data, addr.sin_addr.s_addr)
            if '.' in name:
                parts = name.split('.')
                base_is_ptr = parts[0] in self.pointer_vars
                return self._resolve_field_chain(parts, base_is_pointer=base_is_ptr)
            return self.to_c_name(name)

        if isinstance(expr, SList):
            if len(expr) == 0:
                return "NULL"

            head = expr[0]
            if isinstance(head, Symbol):
                op = head.name

                # Arithmetic/comparison operators (variadic)
                if op in ['+', '-', '*', '/', '%', '&', '|', '^', '<<', '>>']:
                    args = [self.transpile_expr(e) for e in expr.items[1:]]
                    if len(args) == 1:
                        return args[0]
                    result = args[0]
                    for arg in args[1:]:
                        result = f"({result} {op} {arg})"
                    return result

                if op == '==' or op == '=':
                    a = self.transpile_expr(expr[1])
                    b = self.transpile_expr(expr[2])
                    # Use string_eq for string comparisons
                    if self._is_string_expr(expr[1]) or self._is_string_expr(expr[2]):
                        return f"string_eq({a}, {b})"
                    return f"({a} == {b})"

                if op == '!=':
                    a = self.transpile_expr(expr[1])
                    b = self.transpile_expr(expr[2])
                    return f"({a} != {b})"

                if op in ['<', '<=', '>', '>=']:
                    a = self.transpile_expr(expr[1])
                    b = self.transpile_expr(expr[2])
                    return f"({a} {op} {b})"

                if op == 'and':
                    # Handle variadic and: (and a b c ...) -> (a && b && c && ...)
                    parts = [self.transpile_expr(e) for e in expr.items[1:]]
                    return f"({' && '.join(parts)})"

                if op == 'or':
                    # Handle variadic or: (or a b c ...) -> (a || b || c || ...)
                    parts = [self.transpile_expr(e) for e in expr.items[1:]]
                    return f"({' || '.join(parts)})"

                if op == 'not':
                    a = self.transpile_expr(expr[1])
                    return f"(!{a})"

                # If as expression (ternary)
                if op == 'if':
                    cond = self.transpile_expr(expr[1])
                    then_expr = self.transpile_expr(expr[2], expected_type)
                    else_expr = self.transpile_expr(expr[3], expected_type) if len(expr) > 3 else "0"
                    return f"(({cond}) ? ({then_expr}) : ({else_expr}))"

                # Let as expression (GCC statement expression)
                if op == 'let' or op == 'let*':
                    bindings = expr[1]
                    body_items = expr.items[2:]
                    parts = ["({"]

                    # Emit bindings
                    for binding in bindings.items:
                        if isinstance(binding, SList) and len(binding) >= 2:
                            var_name = self.to_c_name(binding[0].name)
                            # Handle typed bindings: (name Type init) vs untyped: (name init)
                            init_expr = binding[2] if len(binding) >= 3 else binding[1]
                            val = self.transpile_expr(init_expr)
                            parts.append(f" __auto_type {var_name} = {val};")

                    # Emit body items (all but last as statements)
                    if body_items:
                        for item in body_items[:-1]:
                            # Check if this is a statement form that needs special handling
                            if isinstance(item, SList) and len(item) >= 1:
                                item_op = item[0].name if isinstance(item[0], Symbol) else None
                                if item_op in self.STATEMENT_FORMS:
                                    parts.append(f" {self._transpile_statement_to_string(item)}")
                                    continue
                            # Otherwise treat as expression
                            parts.append(f" {self.transpile_expr(item)};")

                        # Last item is the result
                        last = body_items[-1]
                        parts.append(f" {self.transpile_expr(last)}; }})")
                    else:
                        parts.append(" 0; }})")

                    return ''.join(parts)

                # for-each as expression (returns 0, used for side effects)
                if op == 'for-each':
                    stmt_code = self._transpile_statement_to_string(expr)
                    return f"({{ {stmt_code} 0; }})"

                # set! as expression (returns 0)
                if op == 'set!':
                    stmt_code = self._transpile_statement_to_string(expr)
                    return f"({{ {stmt_code} 0; }})"

                # for as expression (returns 0, used for side effects)
                if op == 'for':
                    stmt_code = self._transpile_statement_to_string(expr)
                    return f"({{ {stmt_code} 0; }})"

                # while as expression (returns 0, used for side effects)
                if op == 'while':
                    stmt_code = self._transpile_statement_to_string(expr)
                    return f"({{ {stmt_code} 0; }})"

                # With-arena as expression
                if op == 'with-arena':
                    size = self.transpile_expr(expr[1])
                    body = expr[2] if len(expr) > 2 else Number(0)
                    body_code = self.transpile_expr(body)
                    return f"({{ slop_arena _arena = slop_arena_new({size}); slop_arena* arena = &_arena; __auto_type _result = {body_code}; slop_arena_free(&_arena); _result; }})"

                # Do as expression (sequence, returns last)
                if op == 'do':
                    parts = [self.transpile_expr(e) for e in expr.items[1:]]
                    if len(parts) == 0:
                        return "0"
                    if len(parts) == 1:
                        return parts[0]
                    # Use GCC statement expression for proper sequencing
                    stmts = '; '.join(parts[:-1])
                    return f"({{ {stmts}; {parts[-1]}; }})"

                # Match as expression (GCC statement expression with switch)
                if op == 'match':
                    return self._transpile_match_expr(expr)

                # Cond as expression (GCC statement expression)
                if op == 'cond':
                    parts = ["({ int64_t _cond_result; "]
                    first = True
                    for clause in expr.items[1:]:
                        if isinstance(clause, SList) and len(clause) >= 2:
                            test = clause[0]
                            body = clause.items[-1]  # Last item is the result
                            if isinstance(test, Symbol) and test.name == 'else':
                                parts.append(f"}} else {{ _cond_result = {self.transpile_expr(body)}; ")
                            elif first:
                                parts.append(f"if ({self.transpile_expr(test)}) {{ _cond_result = {self.transpile_expr(body)}; ")
                                first = False
                            else:
                                parts.append(f"}} else if ({self.transpile_expr(test)}) {{ _cond_result = {self.transpile_expr(body)}; ")
                    parts.append("} _cond_result; })")
                    return ''.join(parts)

                # Field access
                if op == '.':
                    if len(expr) != 3:
                        raise TranspileError(f"Field access requires exactly 2 arguments, got {len(expr) - 1}", expr)
                    if not isinstance(expr[2], Symbol):
                        raise TranspileError(f"Field name must be a symbol, got {type(expr[2]).__name__}: {expr[2]}", expr)
                    obj_expr = expr[1]
                    field = self.to_c_name(expr[2].name)
                    obj = self.transpile_expr(obj_expr)
                    # Use -> for pointer types, . for value types
                    if self._is_pointer_expr(obj_expr):
                        return f"{obj}->{field}"
                    return f"({obj}).{field}"

                # Index access
                if op == '@':
                    arr = self.transpile_expr(expr[1])
                    idx = self.transpile_expr(expr[2])
                    # Check if indexing a string - use .data[i] for struct access
                    arr_expr = expr[1]
                    arr_type = None
                    if isinstance(arr_expr, Symbol) and arr_expr.name in self.var_types:
                        arr_type = self.var_types[arr_expr.name]
                    if arr_type in ('slop_string', 'string', 'String'):
                        return f"({arr}).data[{idx}]"
                    return f"{arr}[{idx}]"

                # Address-of operator: (addr expr) -> &expr
                if op == 'addr':
                    inner = self.transpile_expr(expr[1])
                    return f"(&{inner})"

                # Built-in functions
                if op == 'arena-alloc':
                    arena = self.transpile_expr(expr[1])
                    size_expr = expr[2]

                    # Try to extract type from the size expression for casting
                    def extract_type_from_sizeof(e):
                        """Extract type name from sizeof or n*sizeof expressions"""
                        if is_form(e, 'sizeof'):
                            return e[1]
                        if is_form(e, '*') and len(e) >= 3:
                            # Check (* n (sizeof T)) or (* (sizeof T) n)
                            if is_form(e[1], 'sizeof'):
                                return e[1][1]
                            if is_form(e[2], 'sizeof'):
                                return e[2][1]
                        return None

                    type_ref = extract_type_from_sizeof(size_expr)
                    if type_ref:
                        # Get the C type
                        c_type = self.to_c_type(type_ref)
                        size = self.transpile_expr(size_expr)
                        return f"({c_type}*)slop_arena_alloc({arena}, {size})"
                    elif isinstance(size_expr, Symbol) and size_expr.name in self.types:
                        # Known type name: (arena-alloc arena TypeName) -> alloc sizeof(Type)
                        c_type = self.to_c_type(size_expr)
                        return f"(({c_type}*)slop_arena_alloc({arena}, sizeof({c_type})))"
                    else:
                        size = self.transpile_expr(size_expr)
                        return f"slop_arena_alloc({arena}, {size})"

                if op == 'sizeof':
                    type_expr = expr[1]
                    # Handle both simple and complex type expressions
                    c_type = self.to_c_type(type_expr)
                    return f"sizeof({c_type})"

                if op == 'deref':
                    # Dereference a pointer: (deref p) -> (*p)
                    ptr = self.transpile_expr(expr[1])
                    return f"(*{ptr})"

                if op == 'now-ms':
                    return "slop_now_ms()"

                if op == 'min':
                    a = self.transpile_expr(expr[1])
                    b = self.transpile_expr(expr[2])
                    return f"(({a}) < ({b}) ? ({a}) : ({b}))"

                if op == 'max':
                    a = self.transpile_expr(expr[1])
                    b = self.transpile_expr(expr[2])
                    return f"(({a}) > ({b}) ? ({a}) : ({b}))"

                # Map operations (call runtime functions)
                if op == 'map-empty':
                    return "map_empty()"

                if op == 'map-get':
                    m = self.transpile_expr(expr[1])
                    key = self.transpile_expr(expr[2])
                    # Check if map variable has a known string-keyed map type
                    map_var = expr[1]
                    map_type = None

                    if isinstance(map_var, Symbol) and map_var.name in self.var_types:
                        map_type = self.var_types[map_var.name]
                    elif is_form(map_var, '.') and len(map_var) >= 3:
                        # Field access: (. obj field) - infer map type from field
                        inferred = self._infer_type(map_var)
                        if inferred:
                            map_type = inferred

                    if map_type and map_type.startswith('slop_map_string_'):
                        # Use typed getter: slop_map_string_X_get(&map, key)
                        return f"{map_type}_get(&{m}, {key})"

                    # Get value type from context
                    value_type = self._get_map_value_type_from_context()
                    if value_type:
                        value_id = self._type_to_identifier(value_type)
                        # Check if the key is a string (String variable or literal)
                        key_expr = expr[2]
                        key_is_string = False
                        if isinstance(key_expr, String):
                            key_is_string = True
                        elif isinstance(key_expr, Symbol) and key_expr.name in self.var_types:
                            var_type = self.var_types[key_expr.name]
                            key_is_string = var_type in ('slop_string', 'string', 'String')

                        if key_is_string:
                            # String-keyed map - use typed string map getter
                            # slop_map_string_{value_id}_get(&map, key)
                            return f"slop_map_string_{value_id}_get(&{m}, {key})"
                        else:
                            # Non-string-keyed maps (int-keyed), use the generated typed wrapper
                            # map_get_ValueType(map, key) from SLOP_MAP_GET_DEFINE macro
                            return f"map_get_{value_type}({m}, {key})"

                    # Fallback: try inline with slop_map_get for string-keyed
                    option_type = self._get_option_c_type(expected_type)
                    if option_type and value_type:
                        return f"({{ void* _v = slop_map_get(&{m}, {key}); {option_type} _r = {{ .has_value = (_v != NULL) }}; if (_v) _r.value = *({value_type}*)_v; _r; }})"
                    # Fallback to generic map_get
                    return f"slop_map_get(&{m}, {key})"

                if op == 'map-put':
                    # (map-put map key val) - 3 args
                    m = self.transpile_expr(expr[1])
                    key = self.transpile_expr(expr[2])
                    val = self.transpile_expr(expr[3])
                    # Check if map variable has a known string-keyed map type
                    map_var = expr[1]
                    if isinstance(map_var, Symbol) and map_var.name in self.var_types:
                        var_type = self.var_types[map_var.name]
                        # Handle pointer to string-keyed map: slop_map_string_X*
                        if var_type.endswith('*') and 'slop_map_string_' in var_type:
                            base_type = var_type.rstrip('*')
                            # For pointer, pass map directly (already a pointer)
                            return f"{base_type}_put(arena, {m}, {key}, {val})"
                        # Handle value type string-keyed map
                        elif var_type.startswith('slop_map_string_'):
                            # Use typed putter: slop_map_string_X_put(arena, &map, key, val)
                            return f"{var_type}_put(arena, &{m}, {key}, {val})"
                    # Fallback for generic maps
                    return f"map_put({m}, {key}, {val})"

                if op == 'map-has':
                    m = self.transpile_expr(expr[1])
                    key = self.transpile_expr(expr[2])
                    return f"map_has({m}, {key})"

                if op == 'map-remove':
                    m = self.transpile_expr(expr[1])
                    key = self.transpile_expr(expr[2])
                    return f"map_remove({m}, {key})"

                if op == 'map-values':
                    m = self.transpile_expr(expr[1])
                    # Use typed version: map_values_ValueType
                    value_type = self._get_list_element_type_from_context()
                    if value_type:
                        return f"map_values_{value_type}({m})"
                    return f"map_values({m})"

                # List operations
                if op == 'take':
                    n = self.transpile_expr(expr[1])
                    lst = self.transpile_expr(expr[2])
                    return f"take({n}, {lst})"

                if op == 'len':
                    lst = self.transpile_expr(expr[1])
                    return f"({lst}).len"

                # Error handling - Result type constructors
                if op == 'ok':
                    val = self.transpile_expr(expr[1]) if len(expr) > 1 else "NULL"
                    result_type = self._get_result_c_type()
                    # For void/Unit result, use 0 instead of NULL for ok value
                    if val == "NULL" and "_void_" in result_type:
                        val = "0"
                    return f"(({result_type}){{ .is_ok = true, .data.ok = {val} }})"

                if op == 'error':
                    val = self.transpile_expr(expr[1])
                    result_type = self._get_result_c_type()
                    return f"(({result_type}){{ .is_ok = false, .data.err = {val} }})"

                if op == '?':
                    # Early return on error
                    val = self.transpile_expr(expr[1])
                    return f"({{ __auto_type _tmp = {val}; if (!_tmp.is_ok) return _tmp; _tmp.data.ok; }})"

                if op == 'is-ok':
                    val = self.transpile_expr(expr[1])
                    return f"({val}.is_ok)"

                if op == 'unwrap':
                    inner_expr = expr[1]
                    val = self.transpile_expr(inner_expr)
                    # Detect if this is an Option or Result to use correct accessor
                    # Options use .value, Results use .data.ok
                    inner_type = self._infer_type(inner_expr)
                    if inner_type and inner_type == 'Option':
                        return f"({val}.value)"
                    # Also check if the generated code is an Option (from list-get, etc.)
                    if 'slop_option_' in val or '.has_value' in val:
                        return f"({val}.value)"
                    # Default to Result accessor
                    return f"({val}.data.ok)"

                # Option type constructors
                if op == 'none':
                    option_type = self._get_option_c_type(expected_type)
                    if option_type:
                        return f"(({option_type}){{ .has_value = false }})"
                    # Fallback for unknown type context
                    return "(slop_option_ptr){ .has_value = false }"

                if op == 'some':
                    val = self.transpile_expr(expr[1])
                    option_type = self._get_option_c_type(expected_type)
                    if option_type:
                        return f"(({option_type}){{ .has_value = true, .value = {val} }})"
                    # Fallback for unknown type context
                    return f"(slop_option_ptr){{ .has_value = true, .value = {val} }}"

                # List construction: (list-new arena Type)
                if op == 'list-new':
                    arena = self.transpile_expr(expr[1])
                    # Get element type from explicit type parameter (new syntax)
                    if len(expr) > 2:
                        elem_type_expr = expr[2]
                        elem_c_type = self.to_c_type(elem_type_expr)
                        elem_id = self._type_to_identifier(elem_c_type)
                        list_type = f"slop_list_{elem_id}"
                        return f"(({list_type}){{ .data = ({elem_c_type}*)slop_arena_alloc({arena}, 16 * sizeof({elem_c_type})), .len = 0, .cap = 16 }})"
                    # Fallback: try to infer from expected_type (legacy support)
                    list_type = self._get_list_c_type(expected_type)
                    elem_type = self._get_list_element_c_type(expected_type)
                    if list_type and elem_type:
                        return f"(({list_type}){{ .data = ({elem_type}*)slop_arena_alloc({arena}, 16 * sizeof({elem_type})), .len = 0, .cap = 16 }})"
                    # Ultimate fallback for unknown type context
                    return f"(slop_list_ptr){{ .data = slop_arena_alloc({arena}, 16 * sizeof(void*)), .len = 0, .cap = 16 }}"

                # List push - inline implementation
                if op == 'list-push':
                    lst = self.transpile_expr(expr[1])
                    item = self.transpile_expr(expr[2])
                    # Generate inline push: list.data[list.len++] = item
                    return f"(({lst}).data[({lst}).len++] = {item})"

                # List get - returns Option<T> for bounds-checked access
                if op == 'list-get':
                    lst_expr = expr[1]
                    lst = self.transpile_expr(lst_expr)
                    idx = self.transpile_expr(expr[2])

                    # Try to infer element type from the list expression
                    option_type = None
                    elem_c_type = None
                    list_type = self._infer_type(lst_expr)

                    if list_type and list_type.startswith('List[') and list_type.endswith(']'):
                        # Extract element type from List[elem_type]
                        elem_c_type = list_type[5:-1]  # Remove "List[" and "]"
                        elem_id = self._type_to_identifier(elem_c_type)
                        option_type = f"slop_option_{elem_id}"
                        # Register option type so it gets emitted
                        self.generated_option_types.add((option_type, elem_c_type))
                    elif list_type and list_type.startswith('slop_list_'):
                        # Handle slop_list_X format from function parameters
                        elem_id = list_type[10:]  # Remove "slop_list_"
                        option_type = f"slop_option_{elem_id}"
                        # Register option type (elem_c_type may be derived from elem_id)
                        self.generated_option_types.add((option_type, elem_id))

                    # Fallback to expected_type if still no option_type
                    if not option_type:
                        option_type = self._get_option_c_type(expected_type)

                    if option_type:
                        # Generate bounds-checked access returning Option
                        return f"({{ __auto_type _lst = {lst}; size_t _idx = (size_t){idx}; {option_type} _r; if (_idx < _lst.len) {{ _r.has_value = true; _r.value = _lst.data[_idx]; }} else {{ _r.has_value = false; }} _r; }})"
                    # Fallback - direct access without Option wrapper (use with care)
                    return f"(({lst}).data[{idx}])"

                # List length
                if op == 'list-len':
                    lst = self.transpile_expr(expr[1])
                    return f"(({lst}).len)"

                # Map construction: (map-new arena KeyType ValueType)
                if op == 'map-new':
                    arena = self.transpile_expr(expr[1])
                    # Get key and value types from explicit type parameters (new syntax)
                    if len(expr) >= 4:
                        key_type_expr = expr[2]
                        value_type_expr = expr[3]
                        key_c_type = self.to_c_type(key_type_expr)
                        value_c_type = self.to_c_type(value_type_expr)
                        key_id = self._type_to_identifier(key_c_type)
                        value_id = self._type_to_identifier(value_c_type)
                        # String-keyed maps use specialized runtime functions
                        if isinstance(key_type_expr, Symbol) and key_type_expr.name == 'String':
                            map_type = f"slop_map_string_{value_id}"
                            return f"{map_type}_new({arena}, 16)"
                        # Non-string-keyed maps use generic void* map
                        return f"slop_map_new({arena}, 16)"
                    # Fallback: try to infer from expected_type (legacy support)
                    if expected_type and is_form(expected_type, 'Map') and len(expected_type) >= 3:
                        key_type = expected_type[1]
                        if isinstance(key_type, Symbol) and key_type.name == 'String':
                            value_type = self.to_c_type(expected_type[2])
                            value_id = self._type_to_identifier(value_type)
                            map_type = f"slop_map_string_{value_id}"
                            return f"{map_type}_new({arena}, 16)"
                    return f"slop_map_new({arena}, 16)"

                # Memory management
                if op == 'arena-new':
                    size = self.transpile_expr(expr[1])
                    return f"slop_arena_new({size})"

                if op == 'arena-free':
                    arena = self.transpile_expr(expr[1])
                    return f"slop_arena_free({arena})"

                # I/O
                if op == 'println':
                    arg = self.transpile_expr(expr[1])
                    return f'printf("%s\\n", ({arg}).data)'

                if op == 'print':
                    arg = self.transpile_expr(expr[1])
                    return f'printf("%s", ({arg}).data)'

                # Data construction
                if op == 'list':
                    # Check for explicit type: (list Type e1 e2 ...)
                    items = expr.items[1:]
                    elements_start = 0
                    explicit_elem_type = None

                    if items and isinstance(items[0], Symbol):
                        name = items[0].name
                        if name[0].isupper() or name in self.types or name in self.builtin_types:
                            # First arg is a type, skip it
                            explicit_elem_type = self.to_c_type(items[0])
                            elements_start = 1

                    elements = [self.transpile_expr(e) for e in items[elements_start:]]
                    n = len(elements)
                    list_type = self._get_list_c_type(expected_type)
                    elem_type = explicit_elem_type or self._get_list_element_c_type(expected_type)

                    # If we have explicit element type but no list type, compute list type
                    if explicit_elem_type and not list_type:
                        inner_id = self._type_to_identifier(explicit_elem_type)
                        list_type = f"slop_list_{inner_id}"
                        self.generated_list_types.add((list_type, explicit_elem_type))
                    if n == 0:
                        if list_type:
                            return f"(({list_type}){{ .data = NULL, .len = 0, .cap = 0 }})"
                        return "(slop_list_ptr){ .data = NULL, .len = 0, .cap = 0 }"
                    elems_str = ", ".join(elements)
                    if list_type and elem_type:
                        return f"(({list_type}){{ .data = ({elem_type}[]){{{elems_str}}}, .len = {n}, .cap = {n} }})"
                    elif elem_type:
                        # Have explicit element type but no list type context
                        return f"(slop_list_ptr){{ .data = ({elem_type}[]){{{elems_str}}}, .len = {n}, .cap = {n} }}"
                    # Fall back to void* array with casted elements
                    casted_elems = ", ".join([f"(void*)(intptr_t)({e})" for e in elements])
                    return f"(slop_list_ptr){{ .data = (void*[]){{{casted_elems}}}, .len = {n}, .cap = {n} }}"

                if op == 'array':
                    # (array Type) with just a type = empty array, zero-initialized
                    # (array elem1 elem2 ...) = array with elements
                    if len(expr) == 2:
                        # Check if it's a type name (starts with uppercase or in types)
                        arg = expr[1]
                        if isinstance(arg, Symbol):
                            name = arg.name
                            if name[0].isupper() or name in self.types or name in self.builtin_types:
                                # It's a type, create zero-initialized array
                                return "{0}"
                    elements = [self.transpile_expr(e) for e in expr.items[1:]]
                    if len(elements) == 0:
                        return "{0}"
                    elems_str = ", ".join(elements)
                    return f"{{{elems_str}}}"

                # Record construction: (record-new Type (field1 val1) (field2 val2) ...)
                if op == 'record-new':
                    # Handle both named types and inline record definitions
                    if isinstance(expr[1], Symbol):
                        # Use qualified type name for proper module prefixing
                        type_name = self.to_qualified_type_name(expr[1].name)
                    elif isinstance(expr[1], SList) and is_form(expr[1], 'record'):
                        # Inline record type - generate anonymous struct
                        type_name = self._generate_inline_record_type(expr[1])
                    else:
                        # Fallback - try to convert to C type
                        type_name = self.to_c_type(expr[1])

                    fields = []
                    for i in range(2, len(expr)):
                        field = expr[i]
                        if isinstance(field, SList) and len(field) >= 2:
                            fname = self.to_c_name(field[0].name)
                            fval = self.transpile_expr(field[1])
                            fields.append(f".{fname} = {fval}")
                    return f"(({type_name}){{{', '.join(fields)}}})"

                # Bare record literal: (record (field1 val1) (field2 val2) ...)
                # Type is inferred from context (return type, assignment, etc.)
                if op == 'record':
                    fields = []
                    for field in expr.items[1:]:
                        if isinstance(field, SList) and len(field) >= 2:
                            fname = self.to_c_name(field[0].name)
                            fval = self.transpile_expr(field[1])
                            fields.append(f".{fname} = {fval}")
                    # Try to infer type from current context
                    type_name = self._infer_record_type_from_context()
                    if type_name:
                        return f"(({type_name}){{{', '.join(fields)}}})"
                    # Fallback: use compound literal without type (requires assignment context)
                    return f"{{{', '.join(fields)}}}"

                # Map literal: (map [KeyType ValueType] (k1 v1) (k2 v2) ...)
                if op == 'map':
                    items = expr.items[1:]
                    pairs_start = 0

                    # Check for explicit types: (map KeyType ValueType (k1 v1) ...)
                    if len(items) >= 2:
                        first = items[0]
                        second = items[1]
                        if isinstance(first, Symbol) and isinstance(second, Symbol):
                            first_name = first.name
                            second_name = second.name
                            first_is_type = first_name[0].isupper() or first_name in self.types or first_name in self.builtin_types
                            second_is_type = second_name[0].isupper() or second_name in self.types or second_name in self.builtin_types
                            if first_is_type and second_is_type:
                                # Skip the type arguments
                                pairs_start = 2

                    pairs = []
                    for item in items[pairs_start:]:
                        if isinstance(item, SList) and len(item) >= 2:
                            key_expr = self.transpile_expr(item[0])
                            value_expr = self.transpile_expr(item[1])
                            pairs.append((key_expr, value_expr))

                    n = len(pairs)
                    if n == 0:
                        # Empty map
                        return "(slop_map){ .entries = NULL, .len = 0, .cap = 0 }"

                    # Generate compound literal with entries
                    # Cast values to void* for slop_map_entry compatibility
                    entries = ", ".join([f"{{ .key = {k}, .value = (void*)(intptr_t)({v}), .occupied = true }}" for k, v in pairs])
                    return f"(slop_map){{ .entries = (slop_map_entry[]){{{entries}}}, .len = {n}, .cap = {n} }}"

                # Union construction: (union-new Type Tag value?)
                if op == 'union-new':
                    raw_type_name = expr[1].name
                    c_type_name = self.to_c_type(expr[1])  # Qualified type name
                    tag = self.to_c_name(expr[2].name)
                    tag_const = f"{c_type_name}_{tag}_TAG"
                    if len(expr) > 3:
                        value = self.transpile_expr(expr[3])
                        return f"(({c_type_name}){{ .tag = {tag_const}, .data.{tag} = {value} }})"
                    return f"(({c_type_name}){{ .tag = {tag_const} }})"

                if op == 'put':
                    # Functional update: (put expr field value)
                    obj = self.transpile_expr(expr[1])
                    field = self.to_c_name(expr[2].name)
                    value = self.transpile_expr(expr[3])
                    return f"({{ __auto_type _tmp = {obj}; _tmp.{field} = {value}; _tmp; }})"

                # Quote (enum value)
                if op == 'quote':
                    inner = expr[1]
                    if isinstance(inner, Symbol):
                        if inner.name in self.enums:
                            return self.enums[inner.name]
                        return self.to_c_name(inner.name)
                    return self.transpile_expr(inner)

                # C inline escape hatch
                if op == 'c-inline':
                    return expr[1].value

                # Type cast
                if op == 'cast':
                    target_type = self.to_c_type(expr[1])
                    inner_expr = expr[2]

                    # Special case: string literal cast to pointer type
                    # Emit raw C string instead of SLOP_STR() which returns a struct
                    if isinstance(inner_expr, String) and is_form(expr[1], 'Ptr'):
                        escaped = inner_expr.value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
                        return f'(({target_type})"{escaped}")'

                    value = self.transpile_expr(inner_expr)
                    # Check if target is a named range type with a _new constructor
                    if isinstance(expr[1], Symbol) and expr[1].name in self.types:
                        # Named type - use constructor for range checking
                        return f"{target_type}_new({value})"
                    elif isinstance(expr[1], SList) and len(expr[1]) >= 3:
                        # Inline range type like (Int 1 ..) - generate inline check
                        head = expr[1][0].name if isinstance(expr[1][0], Symbol) else ''
                        if head in ('Int', 'I8', 'I16', 'I32', 'I64', 'U8', 'U16', 'U32', 'U64'):
                            min_val, max_val = self._parse_range_bounds(expr[1])
                            if min_val is not None or max_val is not None:
                                # Generate inline range check
                                check_parts = []
                                if min_val is not None:
                                    check_parts.append(f"({value}) >= {min_val}")
                                if max_val is not None:
                                    check_parts.append(f"({value}) <= {max_val}")
                                check = " && ".join(check_parts)
                                return f"(SLOP_PRE({check}, \"range check\"), ({target_type})({value}))"
                    return f"(({target_type})({value}))"

                # Sizeof
                if op == 'sizeof':
                    type_name = expr[1]
                    if isinstance(type_name, Symbol) and type_name.name in self.ffi_structs:
                        # FFI struct - use the C name which includes "struct " prefix
                        c_type = self.ffi_structs[type_name.name]['c_name']
                    else:
                        # Regular SLOP type
                        c_type = self.to_c_type(type_name)
                    return f"sizeof({c_type})"

                # FFI function call
                if op in self.ffi_funcs:
                    ffi = self.ffi_funcs[op]
                    params = ffi['params']
                    raw_args = expr.items[1:]
                    args = []

                    for i, arg in enumerate(raw_args):
                        # Get expected parameter type if available
                        param_type = None
                        if isinstance(params, SList) and i < len(params):
                            param_spec = params[i]
                            if isinstance(param_spec, SList) and len(param_spec) >= 2:
                                param_type = param_spec[1]  # Type is second element

                        # Check if param expects a C string (pointer type)
                        expects_c_string = self._is_c_string_param(param_type)

                        if expects_c_string and isinstance(arg, String):
                            # String literal → bare C string
                            escaped = arg.value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
                            args.append(f'"{escaped}"')
                        else:
                            # Non-string or non-C-string param → transpile normally
                            args.append(self.transpile_expr(arg))

                    return f"{ffi['c_name']}({', '.join(args)})"

                # Check if it's an enum value being called like a constructor
                if op in self.enums:
                    # Just return the enum value, ignore any "arguments"
                    return self.enums[op]

                # Check if it's a builtin struct constructor: (String data len) or (Bytes data len cap)
                if op == 'String' and len(expr.items) == 3:
                    data = self.transpile_expr(expr[1])
                    length = self.transpile_expr(expr[2])
                    return f"((slop_string){{.len = {length}, .data = {data}}})"

                if op == 'Bytes' and len(expr.items) == 4:
                    data = self.transpile_expr(expr[1])
                    length = self.transpile_expr(expr[2])
                    cap = self.transpile_expr(expr[3])
                    return f"((slop_bytes){{.len = {length}, .cap = {cap}, .data = {data}}})"

                # Check if it's a record type constructor: (TypeName val1 val2 ...)
                # Record types are in self.types and have fields in record_fields
                if op in self.record_fields:
                    # Get the C type name and field names
                    c_type = self.to_c_type(Symbol(op))
                    fields = list(self.record_fields[op].keys())
                    args = expr.items[1:]
                    if len(args) == len(fields):
                        # Positional constructor: (Point x y) -> ((math_Point){.x = x, .y = y})
                        field_inits = []
                        for i, (fname, arg) in enumerate(zip(fields, args)):
                            fval = self.transpile_expr(arg)
                            field_inits.append(f".{self.to_c_name(fname)} = {fval}")
                        return f"(({c_type}){{{', '.join(field_inits)}}})"

                # Function call (user-defined or imported)
                fn_name = self.to_qualified_c_name(op)
                args = [self.transpile_expr(a) for a in expr.items[1:]]
                return f"{fn_name}({', '.join(args)})"

        return "/* unknown */"

    def _is_c_string_param(self, param_type) -> bool:
        """Check if parameter type expects a C string (pointer to bytes)"""
        if param_type is None:
            return False
        if isinstance(param_type, SList) and len(param_type) >= 2:
            if param_type[0].name == 'Ptr':
                inner = param_type[1]
                if isinstance(inner, Symbol):
                    # Ptr to U8, I8, Void all accept C strings
                    return inner.name in ('U8', 'I8', 'Void')
        return False

    def _resolve_field_chain(self, parts: list, base_is_pointer: bool = True) -> str:
        """Resolve a.b.c to correct C syntax like a->b.c

        For nested struct access:
        - First access from pointer uses ->
        - Subsequent accesses into embedded struct fields use .
        - If a field is itself a pointer, use -> again
        """
        if len(parts) == 1:
            return self.to_c_name(parts[0])

        result = self.to_c_name(parts[0])
        current_struct = None  # We don't know the base var's struct type

        for i, field in enumerate(parts[1:]):
            c_field = self.to_c_name(field)

            if i == 0:
                # First field access - base is assumed to be a pointer
                op = '->' if base_is_pointer else '.'
                # Try to find struct type from field registries
                for struct_name, fields in self.ffi_struct_fields.items():
                    if field in fields:
                        current_struct = struct_name
                        field_info = fields[field]
                        if field_info['is_struct']:
                            current_struct = field_info['type']
                        break
            else:
                # Subsequent accesses - check if previous field was a pointer
                if current_struct and current_struct in self.ffi_struct_fields:
                    field_info = self.ffi_struct_fields[current_struct].get(field)
                    if field_info:
                        if field_info['is_pointer']:
                            op = '->'
                        else:
                            op = '.'
                        # Update current_struct for next iteration
                        if field_info['is_struct']:
                            current_struct = field_info['type']
                        else:
                            current_struct = None
                    else:
                        op = '.'  # Default for unknown fields
                else:
                    op = '.'  # Default: embedded struct fields use .

            result += f"{op}{c_field}"

        return result

    def to_c_type(self, type_expr: SExpr) -> str:
        """Convert SLOP type to C type"""
        if isinstance(type_expr, Symbol):
            name = type_expr.name
            if name in self.builtin_types:
                return self.builtin_types[name]
            if name in self.types:
                return self.types[name].c_type
            if name in self.ffi_structs:
                return self.ffi_structs[name]['c_name']
            # Check if this is an imported type (from import_map)
            if name in self.import_map:
                return self.import_map[name]
            # For unknown types in multi-module builds, add module prefix
            if self.enable_prefixing and self.current_module:
                return f"{self.to_c_name(self.current_module)}_{self.to_c_name(name)}"
            return name

        if isinstance(type_expr, SList) and len(type_expr) >= 1:
            head = type_expr[0].name

            if head == 'Ptr':
                # Check if inner type is an array type alias
                inner_arg = type_expr[1]
                if isinstance(inner_arg, Symbol) and inner_arg.name in self.types:
                    type_info = self.types[inner_arg.name]
                    if type_info.is_array:
                        # For array type alias, use element pointer type directly
                        # (Ptr Storage) where Storage is Invoice[100] -> Invoice*
                        return type_info.c_type
                inner = self.to_c_type(type_expr[1])
                return f"{inner}*"

            if head == 'ScopedPtr':
                # ScopedPtr generates same C type as Ptr, cleanup is semantic
                inner = self.to_c_type(type_expr[1]) if len(type_expr) > 1 else 'void'
                return f"{inner}*"

            if head == 'Option':
                inner = self.to_c_type(type_expr[1])
                inner_id = self._type_to_identifier(inner)
                type_name = f"slop_option_{inner_id}"
                self.generated_option_types.add((type_name, inner))
                return type_name

            if head == 'Result':
                ok_type = self.to_c_type(type_expr[1])
                err_type = self.to_c_type(type_expr[2]) if len(type_expr) > 2 else 'slop_error'
                ok_id = self._type_to_identifier(ok_type)
                err_id = self._type_to_identifier(err_type)
                type_name = f"slop_result_{ok_id}_{err_id}"
                self.generated_result_types.add((type_name, ok_type, err_type))
                return type_name

            if head == 'List':
                inner = self.to_c_type(type_expr[1])
                inner_id = self._type_to_identifier(inner)
                type_name = f"slop_list_{inner_id}"
                self.generated_list_types.add((type_name, inner))
                return type_name

            if head == 'Map':
                key_type = self.to_c_type(type_expr[1])
                value_type = self.to_c_type(type_expr[2]) if len(type_expr) > 2 else 'void*'

                # Check if string-keyed map (uses typed wrapper around slop_map)
                key_sym = type_expr[1]
                if isinstance(key_sym, Symbol) and key_sym.name == 'String':
                    key_id = self._type_to_identifier(key_type)
                    value_id = self._type_to_identifier(value_type)
                    type_name = f"slop_map_{key_id}_{value_id}"
                    self.generated_map_types.add((type_name, key_type, value_type))
                    return type_name
                else:
                    # Non-string-keyed maps use runtime's generic slop_gmap_t via void*
                    # Still track for SLOP_MAP_GET_DEFINE generation
                    key_id = self._type_to_identifier(key_type)
                    value_id = self._type_to_identifier(value_type)
                    type_name = f"slop_map_{key_id}_{value_id}"
                    self.generated_map_types.add((type_name, key_type, value_type))
                    return "void*"

            if head == 'Array':
                # (Array T size) -> T* (pointer to first element)
                inner = self.to_c_type(type_expr[1])
                return f"{inner}*"

            if head == 'record':
                # Inline record: (record (field1 Type1) (field2 Type2) ...)
                # Generate a named typedef to ensure type compatibility
                fields = []
                for field in type_expr.items[1:]:
                    if isinstance(field, SList) and len(field) >= 2:
                        field_name = field[0].name if isinstance(field[0], Symbol) else str(field[0])
                        field_c_name = self.to_c_name(field_name)
                        field_type = self.to_c_type(field[1])
                        fields.append(f"{field_type} {field_c_name}")
                struct_body = "{ " + "; ".join(fields) + "; }"
                # Generate unique name based on hash of fields
                import hashlib
                hash_val = hashlib.md5(struct_body.encode()).hexdigest()[:8]
                type_name = f"_anon_record_{hash_val}"
                # Track for later emission
                self.generated_inline_records[type_name] = struct_body
                return type_name

            if head in self.builtin_types:
                return self.builtin_types[head]

            # Range type reference
            if head in self.types:
                return self.types[head].c_type

        return "void*"

    def _scan_type_definition(self, form: SList):
        """Pre-scan type definition to discover needed generic types.

        This is called before struct emission to ensure generated
        Option/List/Result types are emitted before they're used in structs.
        """
        if is_form(form, 'type'):
            type_expr = form[2] if len(form) > 2 else None
            if type_expr and is_form(type_expr, 'record'):
                self._scan_record_fields(type_expr)
        elif is_form(form, 'record'):
            self._scan_record_fields(form)

    def _scan_record_fields(self, form: SList):
        """Scan record fields for generic types (List, Option, Result, Map)."""
        # Determine start index: skip name if bare form
        start_idx = 1
        if len(form) > 1 and isinstance(form[1], Symbol):
            start_idx = 2

        for field in form.items[start_idx:]:
            if isinstance(field, SList) and len(field) >= 2:
                field_type_expr = field[1]
                self.to_c_type(field_type_expr)  # This populates generated_*_types sets
                # For List<T> fields, also register Option<T> since list-get returns Option
                if is_form(field_type_expr, 'List') and len(field_type_expr) >= 2:
                    elem_type = field_type_expr[1]
                    elem_c_type = self.to_c_type(elem_type)
                    elem_id = self._type_to_identifier(elem_c_type)
                    option_type = f"slop_option_{elem_id}"
                    self.generated_option_types.add((option_type, elem_c_type))

    def _scan_function_types(self, form: SList):
        """Pre-scan function to discover needed generic types.

        This is called before function transpilation to ensure generated
        Option/List/Result types are emitted before they're used.
        """
        # Scan parameter types
        params = form[2] if len(form) > 2 else SList([])
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                param_type = p[1]
                self.to_c_type(param_type)  # This populates generated_*_types sets
                # For List<T> parameters, also register Option<T> since list-get returns Option
                if is_form(param_type, 'List') and len(param_type) >= 2:
                    elem_type = param_type[1]
                    elem_c_type = self.to_c_type(elem_type)
                    elem_id = self._type_to_identifier(elem_c_type)
                    option_type = f"slop_option_{elem_id}"
                    self.generated_option_types.add((option_type, elem_c_type))

        # Scan return type from @spec
        for item in form.items[3:]:
            if is_form(item, '@spec'):
                spec = item[1] if len(item) > 1 else None
                if spec and isinstance(spec, SList) and len(spec) >= 3:
                    self.to_c_type(spec[-1])  # Return type

    # Types pre-defined in slop_runtime.h - don't re-emit these
    RUNTIME_PREDEFINED_TYPES = {
        'slop_list_int', 'slop_list_float', 'slop_list_string', 'slop_list_ptr',
        'slop_option_int', 'slop_option_float', 'slop_option_string', 'slop_option_ptr',
        'slop_result_int', 'slop_result_ptr', 'slop_result_string',
        'slop_map_string_string', 'slop_map_string_int',  # Typed string-keyed maps
    }

    def _type_guard_name(self, type_name: str) -> str:
        """Generate include guard name for a generated type."""
        return type_name.upper().replace('*', '_PTR') + '_DEFINED'

    def _emit_guarded_typedef(self, type_name: str, typedef_body: str):
        """Emit a typedef wrapped with include guards to prevent duplicate definitions."""
        guard = self._type_guard_name(type_name)
        self.emit(f"#ifndef {guard}")
        self.emit(f"#define {guard}")
        self.emit(typedef_body)
        self.emit("#endif")

    def _is_record_type(self, c_type: str) -> bool:
        """Check if a C type name refers to a record (struct) type."""
        # Remove pointer suffix for checking
        base_type = c_type.rstrip('*').strip()
        # Check if it's in our known record types
        for type_info in self.types.values():
            if type_info.c_type == base_type:
                return True
        return False

    def _emit_generated_types(self, phase: str = 'all'):
        """Emit type definitions for generated Option/List/Result/Map types.

        Args:
            phase: 'pointer' = emit List/Map (use pointers)
                   'value' = emit Option/Result that use record values
                   'all' = emit everything (default, for backward compat)
        """
        has_types = (self.generated_option_types or self.generated_list_types or
                     self.generated_result_types or self.generated_map_types or
                     self.generated_inline_records)
        if not has_types:
            return

        emitted_any = False

        # Emit inline record typedefs FIRST (other types may depend on them)
        if phase in ('pointer', 'all'):
            for type_name, struct_body in sorted(self.generated_inline_records.items()):
                if type_name not in self.emitted_generated_types:
                    if not emitted_any:
                        self.emit("/* Generated generic type definitions */")
                        emitted_any = True
                    guard = self._type_guard_name(type_name)
                    self.emit(f"#ifndef {guard}")
                    self.emit(f"#define {guard}")
                    self.emit(f"typedef struct {struct_body} {type_name};")
                    self.emit("#endif")
                    self.emitted_generated_types.add(type_name)

        # Emit Option types (skip pre-defined and already emitted ones)
        # Uses same layout as SLOP_OPTION_DEFINE: { bool has_value; T value; }
        for type_name, inner in sorted(self.generated_option_types):
            if type_name not in self.RUNTIME_PREDEFINED_TYPES and type_name not in self.emitted_generated_types:
                # Option uses inner by value. If inner is a record (not pointer), defer to 'value' phase
                uses_record_by_value = '*' not in inner and self._is_record_type(inner)
                if phase == 'pointer' and uses_record_by_value:
                    continue  # Defer to value phase
                if phase == 'value' and not uses_record_by_value:
                    continue  # Already emitted in pointer phase
                if not emitted_any:
                    self.emit("/* Generated generic type definitions */")
                    emitted_any = True
                self._emit_guarded_typedef(type_name,
                    f"typedef struct {{ bool has_value; {inner} value; }} {type_name};")
                self.emitted_generated_types.add(type_name)

        # Emit List types (skip pre-defined and already emitted ones)
        # List uses T* (pointer), so safe in pointer phase
        if phase in ('pointer', 'all'):
            for type_name, inner in sorted(self.generated_list_types):
                if type_name not in self.RUNTIME_PREDEFINED_TYPES and type_name not in self.emitted_generated_types:
                    if not emitted_any:
                        self.emit("/* Generated generic type definitions */")
                        emitted_any = True
                    self._emit_guarded_typedef(type_name,
                        f"typedef struct {{ {inner}* data; size_t len; size_t cap; }} {type_name};")
                    self.emitted_generated_types.add(type_name)

        # Emit Map types (skip pre-defined and already emitted ones)
        # Map uses pointers in entries, so safe in pointer phase
        if phase in ('pointer', 'all'):
            for type_name, key_type, value_type in sorted(self.generated_map_types):
                if type_name not in self.RUNTIME_PREDEFINED_TYPES and type_name not in self.emitted_generated_types:
                    if not emitted_any:
                        self.emit("/* Generated generic type definitions */")
                        emitted_any = True
                    # For string-keyed maps, use the SLOP_STRING_MAP_DEFINE macro
                    if key_type == 'slop_string':
                        # Find or create the corresponding option type
                        value_id = self._type_to_identifier(value_type)
                        option_type = f"slop_option_{value_id}"
                        guard = self._type_guard_name(type_name)
                        self.emit(f"#ifndef {guard}")
                        self.emit(f"#define {guard}")
                        self.emit(f"SLOP_STRING_MAP_DEFINE({value_type}, {type_name}, {option_type})")
                        self.emit("#endif")
                    else:
                        # For non-string keys, emit custom struct with guards
                        guard = self._type_guard_name(type_name)
                        self.emit(f"#ifndef {guard}")
                        self.emit(f"#define {guard}")
                        self.emit(f"typedef struct {{ {key_type} key; {value_type} value; bool occupied; }} {type_name}_entry;")
                        self.emit(f"typedef struct {{ {type_name}_entry* entries; size_t len; size_t cap; }} {type_name};")
                        self.emit("#endif")
                    self.emitted_generated_types.add(type_name)

        # Emit Result types (skip pre-defined and already emitted ones)
        # Uses same layout as SLOP_RESULT_DEFINE: { bool is_ok; union { T ok; E err; } data; }
        # Result uses ok_type by value in union, so defer if ok_type is a record
        for type_name, ok_type, err_type in sorted(self.generated_result_types):
            if type_name not in self.RUNTIME_PREDEFINED_TYPES and type_name not in self.emitted_generated_types:
                # Result uses ok_type by value. If ok_type is a record (not pointer/void), defer to 'value' phase
                uses_record_by_value = ok_type != 'void' and '*' not in ok_type and self._is_record_type(ok_type)
                if phase == 'pointer' and uses_record_by_value:
                    continue  # Defer to value phase
                if phase == 'value' and not uses_record_by_value:
                    continue  # Already emitted in pointer phase
                if not emitted_any:
                    self.emit("/* Generated generic type definitions */")
                    emitted_any = True
                # Handle void/Unit ok type specially - can't have void in union
                if ok_type == 'void':
                    self._emit_guarded_typedef(type_name,
                        f"typedef struct {{ bool is_ok; union {{ uint8_t ok; {err_type} err; }} data; }} {type_name};")
                else:
                    self._emit_guarded_typedef(type_name,
                        f"typedef struct {{ bool is_ok; union {{ {ok_type} ok; {err_type} err; }} data; }} {type_name};")
                self.emitted_generated_types.add(type_name)

        if emitted_any:
            self.emit("")

    def _emit_map_wrappers(self):
        """Emit map operation wrappers (should go in implementation file, not header)."""
        # Emit typed map operation wrappers for each Option/List type
        # NOTE: Only emit for non-pointer types since the runtime's slop_map_get uses string keys
        # Pointer types would need a different map implementation
        emitted_wrappers = False
        for type_name, inner in sorted(self.generated_option_types):
            wrapper_key = f"map_get_{type_name}"
            if type_name not in self.RUNTIME_PREDEFINED_TYPES and wrapper_key not in self.emitted_generated_types:
                # Skip pointer types - they would need a different implementation
                if '*' in inner:
                    continue
                if not emitted_wrappers:
                    self.emit("/* Generated map operation wrappers */")
                    emitted_wrappers = True
                self.emit(f"SLOP_MAP_GET_DEFINE({inner}, {type_name})")
                self.emitted_generated_types.add(wrapper_key)

        for type_name, inner in sorted(self.generated_list_types):
            wrapper_key = f"map_values_{type_name}"
            if type_name not in self.RUNTIME_PREDEFINED_TYPES and wrapper_key not in self.emitted_generated_types:
                if '*' in inner:
                    # Pointer types break the macro's token pasting, emit inline function directly
                    if not emitted_wrappers:
                        self.emit("/* Generated map operation wrappers */")
                        emitted_wrappers = True
                    inner_id = self._type_to_identifier(inner)
                    self.emit(f"static inline {type_name} map_values_{inner_id}(void* m) {{ {type_name} r = {{0}}; size_t cap = slop_map_len(m); r.data = malloc(cap * sizeof({inner})); r.cap = cap; for (size_t i = 0; i < cap; i++) {{ void* v = slop_map_get(m, i); if (v) {{ r.data[r.len++] = *({inner}*)v; }} }} return r; }}")
                else:
                    if not emitted_wrappers:
                        self.emit("/* Generated map operation wrappers */")
                        emitted_wrappers = True
                    self.emit(f"SLOP_MAP_VALUES_DEFINE({inner}, {type_name})")
                self.emitted_generated_types.add(wrapper_key)

        if emitted_wrappers:
            self.emit("")

    def to_c_name(self, name: str) -> str:
        """Convert SLOP identifier to valid C name"""
        result = name.replace('-', '_').replace('?', '_p').replace('!', '_x').replace('$', '_')
        if result in self.C_KEYWORDS:
            return f"slop_{result}"
        return result

    # Builtin runtime functions that should NOT be prefixed with module name
    # Aligns with BUILTIN_FUNCTIONS from types.py plus Option/Result constructors
    RUNTIME_BUILTINS = {
        # I/O
        'print', 'println',
        # String operations
        'string-len', 'string-concat', 'string-eq', 'string-new', 'string-slice',
        'string-split', 'int-to-string',
        # Arena/memory operations
        'arena-new', 'arena-alloc', 'arena-free',
        # List operations
        'list-new', 'list-push', 'list-get', 'list-len',
        # Map operations
        'map-new', 'map-put', 'map-get', 'map-has',
        # Result operations
        'is-ok', 'unwrap',
        # Time
        'now-ms', 'sleep-ms',
        # Option/Result constructors (not really functions but used in expressions)
        'none', 'some', 'ok', 'error',
    }

    def to_qualified_c_name(self, name: str) -> str:
        """Convert SLOP function name to qualified C name with module prefix.

        Used for multi-module builds to avoid name collisions.
        """
        if not self.enable_prefixing:
            return self.to_c_name(name)

        # 'main' is special - C requires this exact name for entry point
        if name == 'main':
            return 'main'

        # Don't prefix runtime builtin functions
        if name in self.RUNTIME_BUILTINS:
            return self.to_c_name(name)

        # Check if it's an imported function
        if name in self.import_map:
            return self.import_map[name]

        # Prefix with current module name
        c_name = self.to_c_name(name)
        if self.current_module:
            module_prefix = self.to_c_name(self.current_module)
            return f"{module_prefix}_{c_name}"
        return c_name

    def to_qualified_type_name(self, name: str) -> str:
        """Convert SLOP type name to qualified C name with module prefix.

        Used for multi-module builds to avoid type name collisions.
        Builtins are not prefixed. Imported types use their source module prefix.
        """
        if not self.enable_prefixing:
            return self.to_c_name(name)

        # Don't prefix builtin types
        if name in self.builtin_types:
            return name

        # Check if it's an imported type
        if name in self.import_map:
            return self.import_map[name]

        # Prefix with current module name
        c_name = self.to_c_name(name)
        if self.current_module:
            module_prefix = self.to_c_name(self.current_module)
            return f"{module_prefix}_{c_name}"
        return c_name

    def setup_module_context(self, module_name: str, imports: List = None):
        """Set up module context for transpilation.

        Args:
            module_name: Name of the current module
            imports: List of (module_name, [(symbol, arity), ...]) tuples
        """
        self.current_module = module_name
        self.import_map = {}
        self.local_functions = set()
        self.enable_prefixing = True

        if imports:
            for imp_module, symbols in imports:
                module_prefix = self.to_c_name(imp_module)
                for sym_name in symbols:
                    c_name = self.to_c_name(sym_name)
                    self.import_map[sym_name] = f"{module_prefix}_{c_name}"

    def expr_to_str(self, expr: SExpr) -> str:
        """Convert expression to string for error messages"""
        return str(expr).replace('"', '\\"')


def transpile(source: str) -> str:
    """Transpile SLOP source to C"""
    ast = parse(source)
    return Transpiler().transpile(ast)


def transpile_file(input_path: str, output_path: str = None):
    """Transpile SLOP file to C file"""
    with open(input_path) as f:
        source = f.read()

    c_code = transpile(source)

    if output_path:
        with open(output_path, 'w') as f:
            f.write(c_code)
    else:
        print(c_code)


def transpile_multi(modules: dict, order: list) -> str:
    """Transpile multiple modules into a single C file.

    Args:
        modules: Dict mapping module name to ModuleInfo
        order: List of module names in topological order (dependencies first)

    Returns:
        Combined C source code
    """
    from slop.parser import is_form, get_imports, parse_import

    transpiler = Transpiler()
    transpiler.enable_prefixing = True

    # Header
    transpiler.emit("/* Generated by SLOP transpiler - Multi-module build */")
    transpiler.emit("")
    transpiler.emit('#include "slop_runtime.h"')
    transpiler.emit('#include <stdint.h>')
    transpiler.emit('#include <stdbool.h>')
    transpiler.emit("")

    # Collect all FFI includes from all modules (both ffi and ffi-struct)
    ffi_includes = set()
    for name in order:
        info = modules[name]
        for form in info.ast:
            if is_form(form, 'module'):
                for item in form.items[2:]:
                    # Check both ffi and ffi-struct
                    if (is_form(item, 'ffi') or is_form(item, 'ffi-struct')) and len(item) > 1:
                        from slop.parser import String
                        if isinstance(item[1], String):
                            ffi_includes.add(item[1].value)
            elif (is_form(form, 'ffi') or is_form(form, 'ffi-struct')) and len(form) > 1:
                from slop.parser import String
                if isinstance(form[1], String):
                    ffi_includes.add(form[1].value)

    for header in sorted(ffi_includes):
        transpiler.emit(f'#include <{header}>')
    if ffi_includes:
        transpiler.emit("")

    # Process each module in order
    for mod_name in order:
        info = modules[mod_name]

        # Set up module context with imports
        imports = []
        for imp in info.imports:
            imports.append((imp.module_name, imp.symbols))
        transpiler.setup_module_context(mod_name, imports)

        transpiler.emit(f"/* ========== Module: {mod_name} ========== */")
        transpiler.emit("")

        # Get module forms (handle both wrapped and unwrapped)
        module_forms = info.ast
        for form in info.ast:
            if is_form(form, 'module'):
                module_forms = list(form.items[2:])
                break

        # First collect FFI to register functions
        for form in module_forms:
            if is_form(form, 'ffi'):
                transpiler._register_ffi(form)
            elif is_form(form, 'ffi-struct'):
                transpiler._register_ffi_struct(form)

        # Scan FFI function return types to discover Result/Option/List types
        for func_info in transpiler.ffi_funcs.values():
            if func_info.get('return_type'):
                transpiler.to_c_type(func_info['return_type'])

        # Collect types and functions
        types = []
        functions = []
        for form in module_forms:
            if is_form(form, 'type'):
                types.append(form)
            elif is_form(form, 'fn'):
                functions.append(form)

        # Emit forward declarations for record types and register them early
        # so that forward references resolve correctly
        record_types = []
        for t in types:
            if len(t) > 2 and is_form(t[2], 'record'):
                type_name = t[1].name
                # Prefix type names too for multi-module builds
                c_type_name = f"{transpiler.to_c_name(mod_name)}_{transpiler.to_c_name(type_name)}"
                record_types.append(c_type_name)
                # Register in self.types early for forward references
                transpiler.types[type_name] = TypeInfo(name=type_name, c_type=c_type_name)
                transpiler.emit(f"typedef struct {c_type_name} {c_type_name};")
        if record_types:
            transpiler.emit("")

        # Pre-scan types to discover needed generated types (Option, List, Result, Map)
        # This must happen BEFORE emitting type bodies so the generated types are defined first
        for t in types:
            if len(t) > 2 and is_form(t[2], 'record'):
                record_def = t[2]
                for field in record_def.items[1:]:
                    if isinstance(field, SList) and len(field) >= 2:
                        transpiler.to_c_type(field[1])  # This registers generated types
            elif len(t) > 2 and is_form(t[2], 'union'):
                union_def = t[2]
                for variant in union_def.items[1:]:
                    if isinstance(variant, SList) and len(variant) >= 2:
                        transpiler.to_c_type(variant[1])

        # Pre-scan function signatures to discover Result/Option/List types in return types and params
        for fn in functions:
            transpiler._scan_function_types(fn)

        # Emit types in order to resolve dependencies:
        # 1. Enums and range types first
        # 2. Simple records (no generated type fields) - needed by generated types
        # 3. Generated types (Option, List, Result, Map)
        # 4. Complex records (with generated type fields)

        # Phase 1: Emit enums and range types
        for t in types:
            type_expr = t[2] if len(t) > 2 else None
            if type_expr and (is_form(type_expr, 'enum') or not is_form(type_expr, 'record') and not is_form(type_expr, 'union')):
                transpiler.transpile_type(t)

        # Split records into simple (no generated type fields) and complex
        simple_records = []
        complex_records = []
        for t in types:
            type_expr = t[2] if len(t) > 2 else None
            if type_expr and is_form(type_expr, 'record'):
                # Pass full type form (t), not just record expr (type_expr)
                if transpiler._record_uses_generated_types(t):
                    complex_records.append(t)
                else:
                    simple_records.append(t)
            elif type_expr and is_form(type_expr, 'union'):
                # Unions always go to complex for safety
                complex_records.append(t)

        # Phase 2: Emit simple records (generated types may wrap these)
        for t in simple_records:
            transpiler.transpile_type(t)

        # Phase 3: Emit generated types
        transpiler._emit_generated_types()

        # Phase 4: Emit complex records and unions
        for t in complex_records:
            transpiler.transpile_type(t)

        # Emit function forward declarations
        if functions:
            transpiler.emit("/* Forward declarations */")
            for fn in functions:
                transpiler.emit_forward_declaration(fn)
            transpiler.emit("")

        # Emit function bodies
        for fn in functions:
            transpiler.transpile_function(fn)

    return '\n'.join(transpiler.output)


def transpile_multi_split(modules: dict, order: list) -> dict:
    """Transpile multiple modules to separate header/implementation pairs.

    Args:
        modules: Dict mapping module name to ModuleInfo
        order: List of module names in topological order (dependencies first)

    Returns:
        Dict mapping module_name -> (header_code, impl_code)
    """
    from slop.parser import is_form, String

    results = {}

    # Accumulate enum definitions from all modules for cross-module lookup
    all_enums = {}

    for mod_name in order:
        info = modules[mod_name]

        # Create fresh transpiler for this module
        transpiler = Transpiler()
        transpiler.enable_prefixing = True

        # Copy enum definitions from previously processed modules
        transpiler.enums.update(all_enums)

        # Set up module context with imports
        imports = []
        for imp in info.imports:
            imports.append((imp.module_name, imp.symbols))
        transpiler.setup_module_context(mod_name, imports)

        # Get module forms (handle both wrapped and unwrapped)
        module_forms = info.ast
        for form in info.ast:
            if is_form(form, 'module'):
                module_forms = list(form.items[2:])
                break

        # Collect FFI includes for this module
        ffi_includes = set()
        for form in module_forms:
            if is_form(form, 'ffi') and len(form) > 1:
                if isinstance(form[1], String):
                    ffi_includes.add(form[1].value)
            elif is_form(form, 'ffi-struct') and len(form) > 1:
                if isinstance(form[1], String):
                    ffi_includes.add(form[1].value)

        # Register FFI functions and structs
        for form in module_forms:
            if is_form(form, 'ffi'):
                transpiler._register_ffi(form)
            elif is_form(form, 'ffi-struct'):
                transpiler._register_ffi_struct(form)

        # Scan FFI function return types to discover Result/Option/List types
        for func_info in transpiler.ffi_funcs.values():
            if func_info.get('return_type'):
                transpiler.to_c_type(func_info['return_type'])

        # Collect types, functions, and constants
        types = []
        functions = []
        constants = []
        for form in module_forms:
            if is_form(form, 'type'):
                types.append(form)
            elif is_form(form, 'fn'):
                functions.append(form)
            elif is_form(form, 'const'):
                constants.append(form)

        # Register record types early for forward references
        record_types = []
        for t in types:
            if len(t) > 2 and is_form(t[2], 'record'):
                type_name = t[1].name
                c_type_name = f"{transpiler.to_c_name(mod_name)}_{transpiler.to_c_name(type_name)}"
                record_types.append(c_type_name)
                transpiler.types[type_name] = TypeInfo(name=type_name, c_type=c_type_name)

        # Pre-scan types to discover needed generated types
        for t in types:
            if len(t) > 2 and is_form(t[2], 'record'):
                record_def = t[2]
                for field in record_def.items[1:]:
                    if isinstance(field, SList) and len(field) >= 2:
                        field_type_expr = field[1]
                        transpiler.to_c_type(field_type_expr)
                        # For List<T> fields, also register Option<T> since list-get returns Option
                        if is_form(field_type_expr, 'List') and len(field_type_expr) >= 2:
                            elem_type = field_type_expr[1]
                            elem_c_type = transpiler.to_c_type(elem_type)
                            elem_id = transpiler._type_to_identifier(elem_c_type)
                            option_type = f"slop_option_{elem_id}"
                            transpiler.generated_option_types.add((option_type, elem_c_type))
            elif len(t) > 2 and is_form(t[2], 'union'):
                union_def = t[2]
                for variant in union_def.items[1:]:
                    if isinstance(variant, SList) and len(variant) >= 2:
                        transpiler.to_c_type(variant[1])

        # Pre-scan function signatures
        for fn in functions:
            transpiler._scan_function_types(fn)

        # ===== BUILD HEADER FILE =====
        header_lines = []
        # Convert module name to valid C identifier for header guard
        c_mod_name = transpiler.to_c_name(mod_name)
        guard_name = f"{c_mod_name.upper()}_H"

        header_lines.append(f"#ifndef {guard_name}")
        header_lines.append(f"#define {guard_name}")
        header_lines.append("")
        header_lines.append('#include "slop_runtime.h"')
        header_lines.append('#include <stdint.h>')
        header_lines.append('#include <stdbool.h>')

        # FFI includes
        for header in sorted(ffi_includes):
            header_lines.append(f'#include <{header}>')

        # Dependency headers
        for imp in info.imports:
            header_lines.append(f'#include "{imp.module_name}.h"')

        header_lines.append("")

        # Forward declarations for record types
        if record_types:
            for c_type_name in record_types:
                header_lines.append(f"typedef struct {c_type_name} {c_type_name};")
            header_lines.append("")

        # Emit enums and range types to header
        transpiler.output = []
        for t in types:
            type_expr = t[2] if len(t) > 2 else None
            if type_expr and (is_form(type_expr, 'enum') or
                              (not is_form(type_expr, 'record') and not is_form(type_expr, 'union'))):
                transpiler.transpile_type(t)
        if transpiler.output:
            header_lines.extend(transpiler.output)
            header_lines.append("")

        # Split records into simple (no generated type fields) and complex
        simple_records = []
        complex_records = []
        for t in types:
            type_expr = t[2] if len(t) > 2 else None
            if type_expr and is_form(type_expr, 'record'):
                if transpiler._record_uses_generated_types(t):
                    complex_records.append(t)
                else:
                    simple_records.append(t)
            elif type_expr and is_form(type_expr, 'union'):
                complex_records.append(t)

        # Emit simple record bodies first
        transpiler.output = []
        for t in simple_records:
            transpiler.transpile_type(t)
        if transpiler.output:
            header_lines.extend(transpiler.output)
            header_lines.append("")

        # Emit generated types that use pointers (List, Map, Option<ptr>, Result<ptr,...>)
        transpiler.output = []
        transpiler._emit_generated_types(phase='pointer')
        if transpiler.output:
            header_lines.extend(transpiler.output)

        # Emit complex record and union bodies (use generated pointer types)
        transpiler.output = []
        for t in complex_records:
            transpiler.transpile_type(t)
        if transpiler.output:
            header_lines.extend(transpiler.output)
            header_lines.append("")

        # Emit generated types that use record values (Option<Record>, Result<Record,...>)
        transpiler.output = []
        transpiler._emit_generated_types(phase='value')
        if transpiler.output:
            header_lines.extend(transpiler.output)

        # Emit constants as #define to header
        transpiler.output = []
        for const in constants:
            transpiler.transpile_const(const)
        if transpiler.output:
            header_lines.extend(transpiler.output)

        # Function prototypes
        if functions:
            header_lines.append("")
            header_lines.append("/* Function prototypes */")
            transpiler.output = []
            for fn in functions:
                transpiler.emit_forward_declaration(fn)
            header_lines.extend(transpiler.output)

        header_lines.append("")
        header_lines.append(f"#endif /* {guard_name} */")

        # ===== BUILD IMPLEMENTATION FILE =====
        impl_lines = []
        impl_lines.append(f'#include "{mod_name}.h"')
        impl_lines.append("")

        # Map operation wrappers
        transpiler.output = []
        transpiler._emit_map_wrappers()
        if transpiler.output:
            impl_lines.extend(transpiler.output)
            impl_lines.append("")

        # Function bodies
        transpiler.output = []
        for fn in functions:
            transpiler.transpile_function(fn)
        if transpiler.output:
            impl_lines.extend(transpiler.output)

        results[mod_name] = ('\n'.join(header_lines), '\n'.join(impl_lines))

        # Accumulate enum definitions for subsequent modules
        all_enums.update(transpiler.enums)

    return results


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        transpile_file(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        # Test
        test = '''
        (type Age (Int 0 .. 150))
        (type User (record (name String) (age Age)))
        (type Status (enum active inactive))

        (fn greet ((name String))
          (@intent "Say hello")
          (@spec ((String) -> String))
          (@pre (!= name nil))
          (concat "Hello, " name))
        '''
        print(transpile(test))
