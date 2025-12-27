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
from slop.parser import SExpr, SList, Symbol, String, Number, is_form, parse


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
        self.record_fields: Dict[str, Dict[str, bool]] = {}  # type_name -> {field_name -> is_pointer}
        self.func_returns_pointer: Dict[str, bool] = {}  # func_name -> returns_pointer
        self.current_return_type: Optional[SExpr] = None  # Current function's return type for context
        self.generated_option_types: Set[str] = set()  # Track generated Option<T> types
        self.generated_result_types: Set[str] = set()  # Track generated Result<T, E> types
        self.generated_list_types: Set[str] = set()  # Track generated List<T> types

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
                # Field access: (. obj field) - check if field is a pointer type
                if op == '.' and len(expr) >= 3:
                    obj_expr = expr[1]
                    field = expr[2]
                    if isinstance(field, Symbol):
                        field_name = field.name
                        # Look up field type in known record types
                        for type_name, fields in self.record_fields.items():
                            if field_name in fields and fields[field_name]:
                                return True
                    return False
                # Function call: check if function returns a pointer
                if op in self.func_returns_pointer:
                    return self.func_returns_pointer[op]
        return False

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
                # (Option T), (Result T E), (List T) -> track the wrapper type name
                if op in ('Option', 'Result', 'List'):
                    return op
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
                return f"slop_result_{ok_type}_{err_type}"

        return "slop_result"

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
                if isinstance(inner, Symbol):
                    return inner.name
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
                if isinstance(inner, Symbol):
                    return inner.name
                return self.to_c_type(inner)
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

        # Check if it's a simple enum match
        is_simple_enum = False
        for clause in clauses:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                if isinstance(pattern, Symbol) and pattern.name in self.enums:
                    is_simple_enum = True
                    break

        # Build switch as statement expression
        # Use concrete type from context since __auto_type requires initializer
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
                    elif pattern.name in self.enums:
                        parts.append(f"case {self.enums[pattern.name]}: {{ ")
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
                    tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None

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

        Uses tag values: some=0, none=1, ok=0, error=1
        """
        # Map tag names to tag values
        tag_values = {'some': 0, 'none': 1, 'ok': 0, 'error': 1}

        # Determine result type from context for proper declaration
        result_c_type = self._get_result_c_type()
        parts = ["({ __auto_type _match_val = ", scrutinee, f"; {result_c_type} _match_result = {{0}}; switch (_match_val.tag) {{ "]

        for clause in clauses:
            if not isinstance(clause, SList) or len(clause) < 2:
                continue

            pattern = clause[0]
            body = clause.items[1:]

            if isinstance(pattern, SList) and len(pattern) >= 1:
                tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None

                if tag in tag_values:
                    tag_val = tag_values[tag]
                    # Map 'error' to 'err' for data field access
                    data_field = 'err' if tag == 'error' else tag
                    parts.append(f"case {tag_val}: {{ ")
                    if var_name:
                        parts.append(f"__auto_type {var_name} = _match_val.data.{data_field}; ")

                    for j, item in enumerate(body):
                        if j == len(body) - 1:
                            parts.append(f"_match_result = {self.transpile_expr(item)}; ")
                        else:
                            parts.append(f"{self.transpile_expr(item)}; ")
                    parts.append("break; } ")

            elif isinstance(pattern, Symbol):
                # Bare pattern like 'none'
                tag = pattern.name
                if tag in tag_values:
                    parts.append(f"case {tag_values[tag]}: {{ ")
                    for j, item in enumerate(body):
                        if j == len(body) - 1:
                            parts.append(f"_match_result = {self.transpile_expr(item)}; ")
                        else:
                            parts.append(f"{self.transpile_expr(item)}; ")
                    parts.append("break; } ")

        parts.append("} _match_result; })")
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

                # Record construction: (record-new Type ...) -> Type
                if op == 'record-new' and len(expr) >= 2:
                    if isinstance(expr[1], Symbol):
                        return expr[1].name

                # Field access: (. obj field) -> field's type (harder to determine)
                if op == '.' and len(expr) >= 3:
                    # For now, return None (requires full type inference)
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
        """Transpile SLOP AST to C code"""
        self.output = []
        self.ffi_funcs = {}
        self.ffi_structs = {}
        self.ffi_struct_fields = {}
        self.ffi_includes = []

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

        # Collect types and functions separately
        types = []
        functions = []
        modules = []

        for form in ast:
            if is_form(form, 'module'):
                modules.append(form)
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

        # Process all type definitions first
        for form in types:
            if is_form(form, 'type'):
                self.transpile_type(form)
            elif is_form(form, 'record'):
                name = form[1].name if len(form) > 1 and isinstance(form[1], Symbol) else "unnamed"
                self.transpile_record(name, form)
            elif is_form(form, 'enum'):
                name = form[1].name if len(form) > 1 and isinstance(form[1], Symbol) else "unnamed"
                self.transpile_enum(name, form)

        # Pre-scan functions to discover needed generic types
        for form in functions:
            self._scan_function_types(form)

        # Emit generated Option/List/Result types
        self._emit_generated_types()

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
        """Register FFI function declarations: (ffi "header.h" (func ...) ...)"""
        if len(form) < 2:
            return

        header = form[1].value if isinstance(form[1], String) else None
        if header and header not in self.ffi_includes:
            self.ffi_includes.append(header)

        # Register each declared function
        for decl in form.items[2:]:
            if isinstance(decl, SList) and len(decl) >= 2:
                func_name = decl[0].name
                params = decl[1] if len(decl) > 1 else SList([])
                return_type = decl[2] if len(decl) > 2 else Symbol('Void')
                self.ffi_funcs[func_name] = {
                    'c_name': func_name.replace('-', '_'),
                    'params': params,
                    'return_type': return_type
                }

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

            self.ffi_structs[name] = {
                'c_name': f"struct {name}",
                'fields': fields
            }
            # Register as known type
            self.types[name] = TypeInfo(name, f"struct {name}")

    def transpile_module(self, form: SList):
        """Transpile module definition"""
        module_name = form[1].name if len(form) > 1 else "main"
        self.emit(f"/* Module: {module_name} */")
        self.emit("")

        # Collect types and functions
        types = []
        functions = []
        for item in form.items[2:]:
            if is_form(item, 'export'):
                continue  # Skip exports, handled at link time
            elif is_form(item, 'ffi') or is_form(item, 'ffi-struct'):
                continue  # Already processed in first pass
            elif is_form(item, 'type'):
                types.append(item)
            elif is_form(item, 'fn'):
                functions.append(item)

        # Emit forward declarations for record types (needed for mutual references)
        record_types = []
        for t in types:
            if len(t) > 2 and is_form(t[2], 'record'):
                type_name = t[1].name
                record_types.append(type_name)
                self.emit(f"typedef struct {type_name} {type_name};")
        if record_types:
            self.emit("")

        # First pass: emit types
        for t in types:
            self.transpile_type(t)

        # Pre-scan functions to discover needed generic types
        for fn in functions:
            self._scan_function_types(fn)

        # Emit generated Option/List/Result types
        self._emit_generated_types()

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
        name = self.to_c_name(form[1].name)
        params = form[2] if len(form) > 2 else SList([])
        return_type = self._get_return_type(form)

        # C requires main to return int
        if name == 'main':
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

    def transpile_type(self, form: SList):
        """Transpile type definition"""
        name = form[1].name
        type_expr = form[2]

        if is_form(type_expr, 'record'):
            self.transpile_record(name, type_expr)
        elif is_form(type_expr, 'enum'):
            self.transpile_enum(name, type_expr)
        elif is_form(type_expr, 'union'):
            self.transpile_union(name, type_expr)
        else:
            # Range type or alias
            self.transpile_range_type(name, type_expr)

    def transpile_record(self, name: str, form: SList):
        """Transpile record to C struct

        Handles both syntaxes:
        - Wrapped: (record (field Type) ...) - called from (type Name (record ...))
        - Bare: (record Name (field Type) ...) - top-level form
        """
        # Use struct tag name so it works with forward declarations
        self.emit(f"struct {name} {{")
        self.indent += 1

        # Track field types for pointer detection
        self.record_fields[name] = {}

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
                self.record_fields[name][raw_field_name] = self._is_pointer_type(field_type_expr)

        self.indent -= 1
        self.emit(f"}};")
        # Add typedef so we can use just 'Name' instead of 'struct Name'
        self.emit(f"typedef struct {name} {name};")
        self.emit("")

        self.types[name] = TypeInfo(name, name)

    def transpile_enum(self, name: str, form: SList):
        """Transpile enum

        Handles both syntaxes:
        - Wrapped: (enum val1 val2 ...) - called from (type Name (enum ...))
        - Bare: (enum Name val1 val2 ...) - top-level form
        """
        self.emit(f"typedef enum {{")
        self.indent += 1

        # Determine start index: skip name if bare form
        start_idx = 1
        if len(form) > 1 and isinstance(form[1], Symbol) and form[1].name == name:
            # First element is the name in bare form, skip it
            start_idx = 2

        values = form.items[start_idx:]
        for i, val in enumerate(values):
            if not isinstance(val, Symbol):
                continue
            val_c_name = self.to_c_name(val.name)
            qualified_name = f"{name}_{val_c_name}"
            comma = "," if i < len(values) - 1 else ""
            self.emit(f"{qualified_name}{comma}")
            # Store enum value for lookup
            self.enums[val.name] = qualified_name
            self.enums[val_c_name] = qualified_name

        self.indent -= 1
        self.emit(f"}} {name};")
        self.emit("")

        self.types[name] = TypeInfo(name, name)
        self.simple_enums.add(name)

    def transpile_union(self, name: str, form: SList):
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
        self.emit(f"}} {name};")
        self.emit("")

        # Generate tag constants
        for i, variant in enumerate(form.items[1:]):
            if isinstance(variant, SList):
                tag = variant[0].name
                self.emit(f"#define {name}_{tag}_TAG {i}")
        self.emit("")

        self.types[name] = TypeInfo(name, name)

    def transpile_range_type(self, name: str, type_expr: SExpr):
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
                self.emit(f"typedef {inner_type} {name}[{size}];")
                self.emit("")
                self.types[name] = TypeInfo(name, f"{inner_type}*", is_array=True)
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
        self.emit(f"typedef {base_type} {name};")
        self.emit("")

        # Emit constructor with range check
        self.emit(f"static inline {name} {name}_new(int64_t v) {{")
        self.indent += 1

        if min_val is not None and max_val is not None:
            self.emit(f'SLOP_PRE(v >= {min_val} && v <= {max_val}, "{name} in range {min_val}..{max_val}");')
        elif min_val is not None:
            self.emit(f'SLOP_PRE(v >= {min_val}, "{name} >= {min_val}");')
        elif max_val is not None:
            self.emit(f'SLOP_PRE(v <= {max_val}, "{name} <= {max_val}");')

        self.emit(f"return ({name})v;")
        self.indent -= 1
        self.emit("}")
        self.emit("")

        self.types[name] = TypeInfo(name, name, True, min_val, max_val)

    def transpile_function(self, form: SList):
        """Transpile function definition"""
        raw_name = form[1].name
        name = self.to_c_name(raw_name)
        params = form[2] if len(form) > 2 else SList([])

        # Clear pointer and type tracking for this function scope
        self.pointer_vars = set()
        self.var_types = {}

        # Register parameter types and track pointers
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                pname = p[0].name
                ptype = p[1]
                # Track type for type flow analysis
                type_name = self._get_type_name(ptype)
                if type_name:
                    self.var_types[pname] = type_name
                # Track pointers
                if self._is_pointer_type(ptype):
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

        # Emit function signature and track pointer parameters
        param_strs = []
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                raw_pname = p[0].name
                pname = self.to_c_name(raw_pname)
                ptype = self.to_c_type(p[1])
                param_strs.append(f"{ptype} {pname}")
                # Track pointer parameters
                if self._is_pointer_type(p[1]):
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
            code = self.transpile_expr(expr)
            if is_return:
                self.emit(f"return {code};")
            else:
                self.emit(f"{code};")

    def transpile_let(self, expr: SList, is_return: bool):
        """Transpile let binding"""
        bindings = expr[1]
        body = expr.items[2:]

        # Emit bindings
        for binding in bindings:
            raw_name = binding[0].name
            var_name = self.to_c_name(raw_name)
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
            # Type inference would go here; for now use auto
            self.emit(f"__auto_type {var_name} = {var_expr};")

        # Emit body
        for i, item in enumerate(body):
            is_last = (i == len(body) - 1)
            self.transpile_statement(item, is_return and is_last)

    def transpile_let_with_capture(self, expr: SList, capture_var: str):
        """Transpile let binding, capturing the final value in a variable"""
        bindings = expr[1]
        body = expr.items[2:]

        # Emit bindings
        for binding in bindings:
            raw_name = binding[0].name
            var_name = self.to_c_name(raw_name)
            init_expr = binding[1]

            # Register pointer variables
            if self._is_pointer_expr(init_expr):
                self.pointer_vars.add(raw_name)

            var_expr = self.transpile_expr(init_expr)
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
            # Use ternary for simple returns
            then_expr = self.transpile_expr(then_branch)
            else_expr = self.transpile_expr(else_branch) if else_branch else "0"
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
            value = self.transpile_expr(expr[3])
            target_code = self.transpile_expr(target)
            self.emit(f"{target_code}->{self.to_c_name(field)} = {value};")
        elif len(expr) == 3:
            # (set! var value) - simple assignment
            target = expr[1]
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

        # Determine if this is a simple enum match
        # Check if any pattern is a bare symbol or SList head that's in self.enums
        is_simple_enum = False
        for clause in expr.items[2:]:
            if isinstance(clause, SList) and len(clause) >= 2:
                pattern = clause[0]
                if isinstance(pattern, Symbol) and pattern.name in self.enums:
                    is_simple_enum = True
                    break
                if isinstance(pattern, SList) and len(pattern) >= 1:
                    tag = pattern[0].name if isinstance(pattern[0], Symbol) else None
                    if tag and tag in self.enums:
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
                    # Bare symbol pattern (simple enum or wildcard)
                    if pattern.name == '_' or pattern.name == 'else':
                        # Wildcard/default case
                        self.emit("default: {")
                    elif pattern.name in self.enums:
                        # Simple enum variant
                        enum_const = self.enums[pattern.name]
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
                    # Pattern with binding like (Number n)
                    tag = pattern[0].name
                    var_name = self.to_c_name(pattern[1].name) if len(pattern) > 1 else None

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

    def transpile_expr(self, expr: SExpr) -> str:
        """Transpile expression to C"""
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
                    then_expr = self.transpile_expr(expr[2])
                    else_expr = self.transpile_expr(expr[3]) if len(expr) > 3 else "0"
                    return f"(({cond}) ? ({then_expr}) : ({else_expr}))"

                # Let as expression (GCC statement expression)
                if op == 'let' or op == 'let*':
                    bindings = expr[1]
                    body = expr[2] if len(expr) > 2 else Number(0)
                    parts = ["({"]
                    for binding in bindings.items:
                        if isinstance(binding, SList) and len(binding) >= 2:
                            var_name = self.to_c_name(binding[0].name)
                            val = self.transpile_expr(binding[1])
                            parts.append(f" __auto_type {var_name} = {val};")
                    body_code = self.transpile_expr(body)
                    parts.append(f" {body_code}; }})")
                    return ''.join(parts)

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
                    else:
                        size = self.transpile_expr(size_expr)
                        return f"slop_arena_alloc({arena}, {size})"

                if op == 'sizeof':
                    type_expr = expr[1]
                    # Handle both simple and complex type expressions
                    c_type = self.to_c_type(type_expr)
                    return f"sizeof({c_type})"

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
                    # Use typed version: map_get_ValueType
                    value_type = self._get_map_value_type_from_context()
                    if value_type:
                        return f"map_get_{value_type}({m}, {key})"
                    return f"map_get({m}, {key})"

                if op == 'map-put':
                    m = self.transpile_expr(expr[1])
                    key = self.transpile_expr(expr[2])
                    val = self.transpile_expr(expr[3])
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
                    return f"(({result_type}){{ .tag = 0, .data.ok = {val} }})"

                if op == 'error':
                    val = self.transpile_expr(expr[1])
                    result_type = self._get_result_c_type()
                    return f"(({result_type}){{ .tag = 1, .data.err = {val} }})"

                if op == '?':
                    # Early return on error
                    val = self.transpile_expr(expr[1])
                    return f"({{ __auto_type _tmp = {val}; if (_tmp.tag != 0) return _tmp; _tmp.data.ok; }})"

                if op == 'is-ok':
                    val = self.transpile_expr(expr[1])
                    return f"({val}.tag == 0)"

                if op == 'unwrap':
                    val = self.transpile_expr(expr[1])
                    return f"({val}.data.ok)"

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
                    elements = [self.transpile_expr(e) for e in expr.items[1:]]
                    n = len(elements)
                    if n == 0:
                        return "(slop_list){ .data = NULL, .len = 0, .cap = 0 }"
                    elems_str = ", ".join(elements)
                    return f"(slop_list){{ .data = (void*[]){{{elems_str}}}, .len = {n}, .cap = {n} }}"

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
                    type_name = expr[1].name
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

                # Union construction: (union-new Type Tag value?)
                if op == 'union-new':
                    type_name = expr[1].name
                    tag = self.to_c_name(expr[2].name)
                    tag_const = f"{type_name}_{tag}_TAG"
                    if len(expr) > 3:
                        value = self.transpile_expr(expr[3])
                        return f"(({type_name}){{ .tag = {tag_const}, .data.{tag} = {value} }})"
                    return f"(({type_name}){{ .tag = {tag_const} }})"

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
                    value = self.transpile_expr(expr[2])
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

                # Function call
                fn_name = self.to_c_name(op)
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

            if head == 'Option':
                inner = self.to_c_type(type_expr[1])
                type_name = f"slop_option_{inner}"
                self.generated_option_types.add((type_name, inner))
                return type_name

            if head == 'Result':
                ok_type = self.to_c_type(type_expr[1])
                err_type = self.to_c_type(type_expr[2]) if len(type_expr) > 2 else 'slop_error'
                type_name = f"slop_result_{ok_type}_{err_type}"
                self.generated_result_types.add((type_name, ok_type, err_type))
                return type_name

            if head == 'List':
                inner = self.to_c_type(type_expr[1])
                type_name = f"slop_list_{inner}"
                self.generated_list_types.add((type_name, inner))
                return type_name

            if head == 'Array':
                # (Array T size) -> T* (pointer to first element)
                inner = self.to_c_type(type_expr[1])
                return f"{inner}*"

            if head in self.builtin_types:
                return self.builtin_types[head]

            # Range type reference
            if head in self.types:
                return self.types[head].c_type

        return "void*"

    def _scan_function_types(self, form: SList):
        """Pre-scan function to discover needed generic types.

        This is called before function transpilation to ensure generated
        Option/List/Result types are emitted before they're used.
        """
        # Scan parameter types
        params = form[2] if len(form) > 2 else SList([])
        for p in params:
            if isinstance(p, SList) and len(p) >= 2:
                self.to_c_type(p[1])  # This populates generated_*_types sets

        # Scan return type from @spec
        for item in form.items[3:]:
            if is_form(item, '@spec'):
                spec = item[1] if len(item) > 1 else None
                if spec and isinstance(spec, SList) and len(spec) >= 3:
                    self.to_c_type(spec[-1])  # Return type

    def _emit_generated_types(self):
        """Emit type definitions for generated Option/List/Result types."""
        if not (self.generated_option_types or self.generated_list_types or self.generated_result_types):
            return

        self.emit("/* Generated generic type definitions */")

        # Emit Option types
        for type_name, inner in sorted(self.generated_option_types):
            self.emit(f"typedef struct {{ uint8_t tag; union {{ {inner} some; }} data; }} {type_name};")

        # Emit List types
        for type_name, inner in sorted(self.generated_list_types):
            self.emit(f"typedef struct {{ {inner}* data; size_t len; size_t cap; }} {type_name};")

        # Emit Result types
        for type_name, ok_type, err_type in sorted(self.generated_result_types):
            # Handle void/Unit ok type specially - can't have void in union
            if ok_type == 'void':
                self.emit(f"typedef struct {{ uint8_t tag; union {{ uint8_t ok; {err_type} err; }} data; }} {type_name};")
            else:
                self.emit(f"typedef struct {{ uint8_t tag; union {{ {ok_type} ok; {err_type} err; }} data; }} {type_name};")

        self.emit("")

        # Emit typed map operation wrappers for each Option/List type
        self.emit("/* Generated map operation wrappers */")
        for type_name, inner in sorted(self.generated_option_types):
            self.emit(f"SLOP_MAP_GET_DEFINE({inner}, {type_name})")
        for type_name, inner in sorted(self.generated_list_types):
            self.emit(f"SLOP_MAP_VALUES_DEFINE({inner}, {type_name})")
        self.emit("")

    def to_c_name(self, name: str) -> str:
        """Convert SLOP identifier to valid C name"""
        result = name.replace('-', '_').replace('?', '_p').replace('!', '_x').replace('$', '_')
        if result in self.C_KEYWORDS:
            return f"slop_{result}"
        return result

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
