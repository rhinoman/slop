"""
SLOP Schema Converter - Deterministic type generation from schemas

Converts:
- JSON Schema → SLOP types
- SQL DDL → SLOP types
- OpenAPI → SLOP types + signatures
"""

import json
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass


@dataclass
class SlopType:
    """Represents a SLOP type definition"""
    name: str
    definition: str
    comment: Optional[str] = None

    def to_slop(self) -> str:
        lines = []
        if self.comment:
            lines.append(f";; {self.comment}")
        lines.append(f"(type {self.name} {self.definition})")
        return '\n'.join(lines)


@dataclass
class SlopFunction:
    """Represents a SLOP function definition with hole"""
    name: str
    params: List[tuple]  # [(param_name, type_str), ...]
    return_type: str
    intent: str
    hole_prompt: str
    hole_tier: str
    must_use: Optional[List[str]] = None
    preconditions: Optional[List[str]] = None   # @pre expressions
    postconditions: Optional[List[str]] = None  # @post expressions
    examples: Optional[List[tuple]] = None      # [(input, output), ...]

    def to_slop(self) -> str:
        lines = []

        # Function signature
        param_str = ' '.join(f"({p[0]} {p[1]})" for p in self.params)
        lines.append(f"(fn {self.name} ({param_str})")

        # @intent
        lines.append(f'  (@intent "{self.intent}")')

        # @spec
        param_types = ' '.join(p[1] for p in self.params)
        lines.append(f"  (@spec (({param_types}) -> {self.return_type}))")

        # @pre conditions
        if self.preconditions:
            for pre in self.preconditions:
                lines.append(f"  (@pre {pre})")

        # @post conditions
        if self.postconditions:
            for post in self.postconditions:
                lines.append(f"  (@post {post})")

        # @example annotations
        if self.examples:
            for (input_args, output) in self.examples:
                lines.append(f"  (@example {input_args} -> {output})")

        # Hole with complexity and must-use
        must_use_str = ""
        if self.must_use:
            must_use_str = f"\n    :must-use ({' '.join(self.must_use)})"

        lines.append(f'  (hole {self.return_type} "{self.hole_prompt}"')
        lines.append(f"    :complexity {self.hole_tier}{must_use_str}))")

        return '\n'.join(lines)


class JsonSchemaConverter:
    """Convert JSON Schema to SLOP types"""

    def __init__(self):
        self.types: List[SlopType] = []
        self.type_names: Dict[str, str] = {}

    def convert(self, schema: dict, root_name: str = "Root") -> str:
        """Convert JSON Schema to SLOP type definitions"""
        self.types = []
        self.type_names = {}

        # Handle definitions/components first
        if "definitions" in schema:
            for name, defn in schema["definitions"].items():
                self._convert_schema(defn, self._to_type_name(name))

        if "$defs" in schema:
            for name, defn in schema["$defs"].items():
                self._convert_schema(defn, self._to_type_name(name))

        # Convert root schema
        self._convert_schema(schema, root_name)

        # Generate output
        lines = [";; Generated from JSON Schema", ""]
        for t in self.types:
            lines.append(t.to_slop())
            lines.append("")

        return '\n'.join(lines)

    def _convert_schema(self, schema: dict, name: str) -> str:
        """Convert a schema node, returning type expression"""
        if "$ref" in schema:
            ref = schema["$ref"].split("/")[-1]
            return self._to_type_name(ref)

        schema_type = schema.get("type", "any")

        if schema_type == "object":
            return self._convert_object(schema, name)
        elif schema_type == "array":
            return self._convert_array(schema, name)
        elif schema_type == "string":
            return self._convert_string(schema)
        elif schema_type == "integer":
            return self._convert_integer(schema)
        elif schema_type == "number":
            return self._convert_number(schema)
        elif schema_type == "boolean":
            return "Bool"
        elif "enum" in schema:
            return self._convert_enum(schema, name)
        elif "oneOf" in schema or "anyOf" in schema:
            return self._convert_union(schema, name)
        else:
            return "Any"

    def _convert_object(self, schema: dict, name: str) -> str:
        """Convert object schema to record type"""
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))

        if not properties:
            return "(Map String Any)"

        fields = []
        for prop_name, prop_schema in properties.items():
            field_type = self._convert_schema(prop_schema, f"{name}{self._to_type_name(prop_name)}")

            if prop_name not in required:
                field_type = f"(Option {field_type})"

            field_name = self._to_field_name(prop_name)
            fields.append(f"({field_name} {field_type})")

        definition = "(record\n    " + "\n    ".join(fields) + ")"

        self.types.append(SlopType(name, definition))
        self.type_names[name] = name
        return name

    def _convert_array(self, schema: dict, name: str) -> str:
        """Convert array schema to List type"""
        items = schema.get("items", {})
        item_type = self._convert_schema(items, f"{name}Item")

        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")

        if min_items is not None and max_items is not None:
            return f"(List {item_type} {min_items} .. {max_items})"
        elif min_items is not None:
            return f"(List {item_type} {min_items} ..)"
        elif max_items is not None:
            return f"(List {item_type} .. {max_items})"
        else:
            return f"(List {item_type})"

    def _convert_string(self, schema: dict) -> str:
        """Convert string schema with constraints"""
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        pattern = schema.get("pattern")
        format_ = schema.get("format")

        # Handle common formats
        if format_ == "email":
            return "(String 5 .. 255)"  # Email type
        elif format_ == "date-time":
            return "DateTime"
        elif format_ == "date":
            return "Date"
        elif format_ == "uri":
            return "(String 1 .. 2048)"
        elif format_ == "uuid":
            return "(String 36 .. 36)"

        # Handle length constraints
        if min_len is not None and max_len is not None:
            return f"(String {min_len} .. {max_len})"
        elif min_len is not None:
            return f"(String {min_len} ..)"
        elif max_len is not None:
            return f"(String .. {max_len})"
        else:
            return "String"

    def _convert_integer(self, schema: dict) -> str:
        """Convert integer schema with constraints"""
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        exclusive_min = schema.get("exclusiveMinimum")
        exclusive_max = schema.get("exclusiveMaximum")

        # Handle exclusive bounds
        if exclusive_min is not None:
            minimum = exclusive_min + 1
        if exclusive_max is not None:
            maximum = exclusive_max - 1

        if minimum is not None and maximum is not None:
            return f"(Int {minimum} .. {maximum})"
        elif minimum is not None:
            return f"(Int {minimum} ..)"
        elif maximum is not None:
            return f"(Int .. {maximum})"
        else:
            return "Int"

    def _convert_number(self, schema: dict) -> str:
        """Convert number schema"""
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")

        if minimum is not None and maximum is not None:
            return f"(Float {minimum} .. {maximum})"
        elif minimum is not None:
            return f"(Float {minimum} ..)"
        elif maximum is not None:
            return f"(Float .. {maximum})"
        else:
            return "Float"

    def _convert_enum(self, schema: dict, name: str) -> str:
        """Convert enum schema"""
        values = schema.get("enum", [])

        if all(isinstance(v, str) for v in values):
            enum_vals = ' '.join(self._to_field_name(v) for v in values)
            definition = f"(enum {enum_vals})"
            self.types.append(SlopType(name, definition))
            return name
        else:
            # Mixed types - use union
            return "Any"

    def _convert_union(self, schema: dict, name: str) -> str:
        """Convert oneOf/anyOf to union"""
        variants = schema.get("oneOf") or schema.get("anyOf", [])

        if len(variants) == 2:
            # Check for nullable pattern
            types = [v.get("type") for v in variants]
            if "null" in types:
                non_null = [v for v in variants if v.get("type") != "null"][0]
                inner = self._convert_schema(non_null, f"{name}Value")
                return f"(Option {inner})"

        # General union
        union_variants = []
        for i, variant in enumerate(variants):
            variant_name = f"{name}V{i}"
            variant_type = self._convert_schema(variant, variant_name)
            tag = f"v{i}"
            union_variants.append(f"({tag} {variant_type})")

        definition = "(union\n    " + "\n    ".join(union_variants) + ")"
        self.types.append(SlopType(name, definition))
        return name

    def _to_type_name(self, s: str) -> str:
        """Convert string to PascalCase type name"""
        # Split on separators AND case boundaries to preserve PascalCase
        s = re.sub(r'([a-z])([A-Z])', r'\1_\2', s)
        words = re.split(r'[-_\s]+', s)
        return ''.join(w.capitalize() for w in words)

    def _to_field_name(self, s: str) -> str:
        """Convert string to kebab-case field name"""
        s = re.sub(r'([a-z])([A-Z])', r'\1-\2', s)
        return s.lower().replace('_', '-').replace(' ', '-')


class SqlSchemaConverter:
    """Convert SQL DDL to SLOP types"""

    def convert(self, sql: str) -> str:
        """Convert SQL CREATE TABLE statements to SLOP types"""
        types = []

        # Find CREATE TABLE statements
        pattern = r'CREATE\s+TABLE\s+(\w+)\s*\((.*?)\);'
        matches = re.findall(pattern, sql, re.IGNORECASE | re.DOTALL)

        for table_name, columns_str in matches:
            type_name = self._to_type_name(table_name)
            fields = self._parse_columns(columns_str)

            field_strs = [f"({f['name']} {f['type']})" for f in fields]
            definition = "(record\n    " + "\n    ".join(field_strs) + ")"

            types.append(SlopType(type_name, definition, f"From table: {table_name}"))

        lines = [";; Generated from SQL DDL", ""]
        for t in types:
            lines.append(t.to_slop())
            lines.append("")

        return '\n'.join(lines)

    def _parse_columns(self, columns_str: str) -> List[dict]:
        """Parse column definitions"""
        fields = []

        # Split by comma, but not within parentheses
        depth = 0
        current = ""
        parts = []

        for char in columns_str:
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            elif char == ',' and depth == 0:
                parts.append(current.strip())
                current = ""
                continue
            current += char
        if current.strip():
            parts.append(current.strip())

        for part in parts:
            # Skip constraints
            if any(kw in part.upper() for kw in ['PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE', 'CHECK', 'INDEX']):
                continue

            tokens = part.split()
            if len(tokens) < 2:
                continue

            col_name = tokens[0].strip('`"[]')
            col_type = tokens[1].upper()

            is_nullable = 'NOT NULL' not in part.upper()

            slop_type = self._sql_type_to_slop(col_type, part)

            if is_nullable:
                slop_type = f"(Option {slop_type})"

            fields.append({
                'name': self._to_field_name(col_name),
                'type': slop_type
            })

        return fields

    def _sql_type_to_slop(self, sql_type: str, full_def: str) -> str:
        """Convert SQL type to SLOP type"""
        sql_type = sql_type.upper()

        # Extract length/precision
        length_match = re.search(r'\((\d+)(?:,\s*(\d+))?\)', full_def)
        length = int(length_match.group(1)) if length_match else None

        if sql_type.startswith('VARCHAR') or sql_type.startswith('CHAR'):
            if length:
                return f"(String .. {length})"
            return "String"
        elif sql_type in ('TEXT', 'CLOB'):
            return "String"
        elif sql_type in ('INT', 'INTEGER'):
            return "Int"
        elif sql_type == 'BIGINT':
            return "I64"
        elif sql_type == 'SMALLINT':
            return "I16"
        elif sql_type == 'TINYINT':
            return "I8"
        elif sql_type in ('FLOAT', 'REAL'):
            return "F32"
        elif sql_type in ('DOUBLE', 'DECIMAL', 'NUMERIC'):
            return "Float"
        elif sql_type in ('BOOLEAN', 'BOOL'):
            return "Bool"
        elif sql_type in ('BLOB', 'BYTEA', 'BINARY', 'VARBINARY'):
            if length:
                return f"(Bytes .. {length})"
            return "Bytes"
        elif sql_type in ('DATE',):
            return "Date"
        elif sql_type in ('DATETIME', 'TIMESTAMP'):
            return "DateTime"
        elif sql_type == 'UUID':
            return "(String 36 .. 36)"
        else:
            return "Any"

    def _to_type_name(self, s: str) -> str:
        """Convert to PascalCase"""
        # Split on separators AND case boundaries to preserve PascalCase
        s = re.sub(r'([a-z])([A-Z])', r'\1_\2', s)
        words = re.split(r'[-_\s]+', s)
        return ''.join(w.capitalize() for w in words)

    def _to_field_name(self, s: str) -> str:
        """Convert to kebab-case"""
        s = re.sub(r'([a-z])([A-Z])', r'\1-\2', s)
        return s.lower().replace('_', '-')


class OpenApiConverter:
    """Convert OpenAPI 3.x specs to SLOP types and function signatures

    Storage modes:
    - 'stub': Generate (@requires storage ...) with interactive prompt
    - 'map': Generate Map-based state + CRUD helpers (no holes for storage)
    - 'none': Just types + holes (current behavior, no storage context)
    """

    def __init__(self, storage_mode: str = 'stub'):
        self.storage_mode = storage_mode
        self.json_converter = JsonSchemaConverter()
        self.types: List[SlopType] = []
        self.functions: List[SlopFunction] = []
        self.error_codes: set = set()
        self.resource_types: List[str] = []  # Track resource types for state generation
        self.schema_fields: Dict[str, List[str]] = {}  # Track fields per schema type

    def convert(self, spec: dict) -> str:
        """Convert OpenAPI spec to SLOP module with types and function stubs"""
        self.types = []
        self.functions = []
        self.error_codes = set()
        self.resource_types = []
        self.schema_fields = {}
        self.json_converter = JsonSchemaConverter()

        # Extract API metadata
        info = spec.get('info', {})
        title = info.get('title', 'Api')

        # 1. Convert component schemas first (reuse JsonSchemaConverter)
        self._convert_components(spec.get('components', {}))

        # 2. Collect error codes from all operations
        self._collect_error_codes(spec.get('paths', {}))

        # 3. Generate unified ApiError type
        self._generate_error_type()

        # 4. Identify resource types from paths (for state generation)
        self._identify_resources(spec.get('paths', {}))

        # 5. Convert each path/operation to function
        self._convert_paths(spec.get('paths', {}))

        # 6. Generate output based on storage mode
        return self._generate_output(title)

    def _extract_result_inner_type(self, result_type: str) -> str:
        """Extract T from (Result T E) -> returns T

        Examples:
            "(Result Pet ApiError)" -> "Pet"
            "(Result (List Pet) ApiError)" -> "(List Pet)"
        """
        if not result_type.startswith("(Result "):
            return result_type

        # Remove "(Result " prefix and find the inner type
        inner = result_type[8:]  # Skip "(Result "

        # Handle nested parens - find where T ends
        if inner.startswith("("):
            # Nested type like (List Pet) - find matching paren
            depth = 1
            i = 1
            while i < len(inner) and depth > 0:
                if inner[i] == "(":
                    depth += 1
                elif inner[i] == ")":
                    depth -= 1
                i += 1
            return inner[:i]
        else:
            # Simple type like Pet - ends at space
            end = inner.find(" ")
            return inner[:end] if end > 0 else inner.rstrip(")")

    def _build_storage_type_map(self) -> Dict[str, str]:
        """Build mapping from storage function name to expected return type.

        Derives types from the API functions that reference each storage fn.

        Returns:
            Dict mapping storage fn name to return type, e.g.:
            {
                "state-get-pet": "(Option Pet)",
                "state-list-pets": "(List Pet)",
                "state-insert-pet": "Pet",
                "state-delete-pet": "Bool"
            }
        """
        storage_types: Dict[str, str] = {}

        for fn in self.functions:
            if not fn.must_use:
                continue

            inner_type = self._extract_result_inner_type(fn.return_type)

            for storage_fn in fn.must_use:
                if not storage_fn.startswith("state-"):
                    continue

                if storage_fn.startswith("state-get-"):
                    # GET single item: (Result T E) -> storage returns (Option T)
                    storage_types[storage_fn] = f"(Option {inner_type})"
                elif storage_fn.startswith("state-list-"):
                    # GET list: (Result (List T) E) -> storage returns (List T)
                    storage_types[storage_fn] = inner_type
                elif storage_fn.startswith("state-insert-"):
                    # POST: (Result T E) -> storage returns T
                    storage_types[storage_fn] = inner_type
                elif storage_fn.startswith("state-delete-"):
                    # DELETE: (Result Unit E) -> storage returns Bool
                    storage_types[storage_fn] = "Bool"

        return storage_types

    def _convert_components(self, components: dict):
        """Convert OpenAPI components/schemas using JsonSchemaConverter"""
        schemas = components.get('schemas', {})
        for name, schema in schemas.items():
            type_name = self._to_type_name(name)
            self.json_converter._convert_schema(schema, type_name)
            # Track field names for each schema (for generating make-*-from-new)
            if schema.get('type') == 'object' and 'properties' in schema:
                fields = [self._to_kebab(f) for f in schema['properties'].keys()]
                self.schema_fields[type_name] = fields

        # Copy generated types
        self.types.extend(self.json_converter.types)

    def _collect_error_codes(self, paths: dict):
        """Collect all HTTP error codes used in spec"""
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method not in ('get', 'post', 'put', 'patch', 'delete', 'head', 'options'):
                    continue
                if not isinstance(operation, dict):
                    continue
                for code_str in operation.get('responses', {}).keys():
                    try:
                        code = int(code_str)
                        if 400 <= code < 600:
                            self.error_codes.add(code)
                    except ValueError:
                        pass  # 'default' or other non-numeric

    def _generate_error_type(self):
        """Generate unified ApiError enum from collected error codes"""
        error_map = {
            400: 'bad-request',
            401: 'unauthorized',
            403: 'forbidden',
            404: 'not-found',
            405: 'method-not-allowed',
            409: 'conflict',
            422: 'validation-error',
            429: 'too-many-requests',
            500: 'internal-error',
            502: 'bad-gateway',
            503: 'service-unavailable',
            504: 'gateway-timeout',
        }

        variants = []
        for code in sorted(self.error_codes):
            variant = error_map.get(code, f'error-{code}')
            variants.append(variant)

        if not variants:
            variants = ['unknown-error']
        else:
            variants.append('unknown-error')

        self.types.append(SlopType(
            'ApiError',
            f"(enum {' '.join(variants)})",
            "HTTP API error codes"
        ))

    def _identify_resources(self, paths: dict):
        """Identify resource types from API paths for state generation"""
        # Look for patterns like /pets, /users/{id} and extract the resource name
        seen = set()
        for path in paths.keys():
            segments = [s for s in path.split('/') if s and not s.startswith('{')]
            if segments:
                resource = segments[0]  # First segment is typically the resource
                type_name = self._to_type_name(resource)
                # Singularize if plural
                if type_name.endswith('s') and len(type_name) > 1:
                    type_name = type_name[:-1]
                if type_name not in seen:
                    seen.add(type_name)
                    self.resource_types.append(type_name)

    def _convert_paths(self, paths: dict):
        """Convert each path/operation to SLOP function"""
        for path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method not in ('get', 'post', 'put', 'patch', 'delete'):
                    continue
                if not isinstance(operation, dict):
                    continue
                self._convert_operation(method, path, operation)

    def _convert_operation(self, method: str, path: str, operation: dict):
        """Convert single API operation to SLOP function"""
        fn_name = self._path_to_function_name(method, path)

        # Extract parameters
        params = []
        must_use = []
        preconditions = []
        param_examples = {}

        # For stub/map modes, add state as first parameter
        if self.storage_mode in ('stub', 'map') and self.resource_types:
            # POST operations need arena for state-insert-*
            if method.lower() == 'post':
                params.append(('arena', 'Arena'))
                must_use.append('arena')
            params.append(('state', '(Ptr State)'))
            must_use.append('state')
            preconditions.append("(!= state nil)")

        # Path parameters
        for param in operation.get('parameters', []):
            if param.get('in') == 'path':
                pname = self._to_kebab(param['name'])
                ptype = self._schema_to_type(param.get('schema', {}))
                params.append((pname, ptype))
                must_use.append(pname)

                # Extract preconditions from schema constraints
                schema = param.get('schema', {})
                if 'minimum' in schema:
                    preconditions.append(f"(>= {pname} {schema['minimum']})")
                if 'maximum' in schema:
                    preconditions.append(f"(<= {pname} {schema['maximum']})")

                # Store example for later
                if 'example' in param:
                    param_examples[pname] = param['example']

        # Query parameters (as optional unless required)
        query_params = [p for p in operation.get('parameters', []) if p.get('in') == 'query']
        for param in query_params:
            pname = self._to_kebab(param['name'])
            ptype = self._schema_to_type(param.get('schema', {}))
            if not param.get('required', False):
                ptype = f"(Option {ptype})"
            params.append((pname, ptype))

            # Store example
            if 'example' in param:
                param_examples[pname] = param['example']

        # Request body
        if 'requestBody' in operation:
            body_schema = self._extract_body_schema(operation['requestBody'])
            if body_schema:
                body_type = self._schema_to_type(body_schema)
                params.append(('body', f"(Ptr {body_type})"))
                must_use.append('body')
                preconditions.append("(!= body nil)")

        # Extract return type from success response
        return_type = self._extract_return_type(operation)

        # For stub/map modes, add appropriate storage function to must_use
        storage_fn = None
        if self.storage_mode in ('stub', 'map') and self.resource_types:
            # Determine resource from path (first non-param segment)
            segments = [s for s in path.split('/') if s and not s.startswith('{')]
            if segments:
                resource = self._to_type_name(segments[0])
                if resource.endswith('s') and len(resource) > 1:
                    resource = resource[:-1]
                resource_lower = self._to_kebab(resource)

                # Map HTTP method to storage function
                method_lower = method.lower()
                if method_lower == 'get':
                    # Check if it's a list or single item
                    has_path_param = any(
                        p.get('in') == 'path' for p in operation.get('parameters', [])
                    )
                    if has_path_param:
                        storage_fn = f"state-get-{resource_lower}"
                    else:
                        storage_fn = f"state-list-{resource_lower}s"
                elif method_lower == 'post':
                    storage_fn = f"state-insert-{resource_lower}"
                elif method_lower == 'delete':
                    storage_fn = f"state-delete-{resource_lower}"
                elif method_lower in ('put', 'patch'):
                    # Update typically needs get + set
                    storage_fn = f"state-get-{resource_lower}"

                if storage_fn:
                    must_use.append(storage_fn)

        # Generate intent from summary/description
        summary = operation.get('summary', '')
        description = operation.get('description', '')
        intent = summary or description or f"{method.upper()} {path}"
        # Truncate and clean
        intent = intent.replace('"', '\\"')[:100]

        # Generate postconditions
        postconditions = self._extract_postconditions(operation, return_type)

        # Generate examples
        examples = self._extract_examples(operation, param_examples)

        # Generate hole prompt
        hole_prompt = self._generate_hole_prompt(method, path, operation, storage_fn)

        # Determine complexity tier
        tier = self._classify_operation_tier(method, operation)

        self.functions.append(SlopFunction(
            name=fn_name,
            params=params,
            return_type=f"(Result {return_type} ApiError)",
            intent=intent,
            hole_prompt=hole_prompt,
            hole_tier=tier,
            must_use=must_use if must_use else None,
            preconditions=preconditions if preconditions else None,
            postconditions=postconditions if postconditions else None,
            examples=examples if examples else None
        ))

    def _extract_body_schema(self, request_body: dict) -> Optional[dict]:
        """Extract schema from request body"""
        content = request_body.get('content', {})
        json_content = content.get('application/json', {})
        return json_content.get('schema')

    def _extract_return_type(self, operation: dict) -> str:
        """Extract success response type"""
        responses = operation.get('responses', {})

        for code in ('200', '201', '204'):
            if code in responses:
                response = responses[code]
                content = response.get('content', {})
                json_content = content.get('application/json', {})
                schema = json_content.get('schema')
                if schema:
                    return self._schema_to_type(schema)

        return 'Unit'  # No response body

    def _schema_to_type(self, schema: dict) -> str:
        """Convert OpenAPI schema to SLOP type reference"""
        if '$ref' in schema:
            ref = schema['$ref'].split('/')[-1]
            return self._to_type_name(ref)

        # Handle array types
        if schema.get('type') == 'array':
            items = schema.get('items', {})
            item_type = self._schema_to_type(items)
            return f"(List {item_type})"

        # Handle primitive types with constraints
        schema_type = schema.get('type', 'any')

        if schema_type == 'integer':
            minimum = schema.get('minimum')
            maximum = schema.get('maximum')
            if minimum is not None and maximum is not None:
                return f"(Int {minimum} .. {maximum})"
            elif minimum is not None:
                return f"(Int {minimum} ..)"
            elif maximum is not None:
                return f"(Int .. {maximum})"
            return "Int"
        elif schema_type == 'number':
            return "Float"
        elif schema_type == 'string':
            fmt = schema.get('format')
            if fmt == 'email':
                return "(String 5 .. 255)"
            elif fmt == 'uuid':
                return "(String 36 .. 36)"
            elif fmt == 'date':
                return "Date"
            elif fmt in ('date-time', 'datetime'):
                return "DateTime"
            min_len = schema.get('minLength')
            max_len = schema.get('maxLength')
            if min_len is not None and max_len is not None:
                return f"(String {min_len} .. {max_len})"
            elif max_len is not None:
                return f"(String .. {max_len})"
            return "String"
        elif schema_type == 'boolean':
            return "Bool"
        elif schema_type == 'object':
            return "Any"  # Inline object, not worth creating a type
        else:
            return "Any"

    def _extract_postconditions(self, operation: dict, return_type: str) -> Optional[List[str]]:
        """Generate @post conditions from response schema"""
        posts = []

        # Basic postcondition: success result is non-nil for object returns
        if return_type not in ('Unit', 'Bool', 'Int', 'Float', 'String'):
            if return_type.startswith('(List'):
                posts.append(f"(match $result ((ok list) (>= (len list) 0)) ((error _) true))")
            else:
                posts.append(f"(match $result ((ok val) (!= val nil)) ((error _) true))")

        return posts if posts else None

    def _extract_examples(self, operation: dict, param_examples: dict) -> Optional[List[tuple]]:
        """Extract @example annotations from OpenAPI examples"""
        examples = []

        # Get response example
        responses = operation.get('responses', {})
        response_example = None

        for code in ('200', '201'):
            if code in responses:
                response = responses[code]
                content = response.get('content', {})
                json_content = content.get('application/json', {})
                if 'example' in json_content:
                    response_example = json_content['example']
                    break
                elif 'examples' in json_content:
                    # Take first example
                    ex_dict = json_content['examples']
                    if ex_dict:
                        first_key = list(ex_dict.keys())[0]
                        response_example = ex_dict[first_key].get('value')
                        break

        # If we have both input and output examples, create an @example
        if param_examples and response_example is not None:
            input_args = self._format_example_args(param_examples)
            output = self._format_example_value(response_example)
            examples.append((input_args, f"(ok {output})"))

        return examples if examples else None

    def _format_example_args(self, param_examples: dict) -> str:
        """Format parameter examples as SLOP tuple"""
        if len(param_examples) == 1:
            val = list(param_examples.values())[0]
            return self._format_example_value(val)
        else:
            vals = [self._format_example_value(v) for v in param_examples.values()]
            return f"({' '.join(vals)})"

    def _format_example_value(self, value: Any) -> str:
        """Format a single value for SLOP example"""
        if isinstance(value, str):
            return f'"{value}"'
        elif isinstance(value, bool):
            return 'true' if value else 'false'
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, dict):
            # Format as SLOP record
            fields = ' '.join(f"({self._to_kebab(k)} {self._format_example_value(v)})"
                              for k, v in value.items())
            return f"({fields})"
        elif isinstance(value, list):
            items = ' '.join(self._format_example_value(v) for v in value)
            return f"(list {items})"
        elif value is None:
            return 'nil'
        else:
            return str(value)

    def _generate_hole_prompt(self, method: str, path: str, operation: dict,
                               storage_fn: Optional[str] = None) -> str:
        """Generate descriptive hole prompt"""
        prompts = {
            'get': "Fetch data from storage",
            'post': "Create new resource and persist to storage",
            'put': "Update existing resource in storage",
            'patch': "Partially update resource fields",
            'delete': "Remove resource from storage"
        }
        base = prompts.get(method.lower(), f"Handle {method.upper()} request")

        # Add parameter context
        params = operation.get('parameters', [])
        path_params = [p['name'] for p in params if p.get('in') == 'path']
        if path_params:
            base += f" using {', '.join(path_params)}"

        # Add storage function hint for stub/map modes
        if storage_fn:
            base += f". Use {storage_fn}"

        return base

    def _classify_operation_tier(self, method: str, operation: dict) -> str:
        """Determine hole complexity tier for an operation"""
        # Check for complexity indicators
        params = operation.get('parameters', [])
        has_pagination = any(
            p.get('name') in ('page', 'limit', 'offset', 'cursor', 'skip', 'take')
            for p in params
        )
        has_request_body = 'requestBody' in operation

        method_lower = method.lower()

        if method_lower == 'get':
            # GET with path param is simple lookup
            path_params = [p for p in params if p.get('in') == 'path']
            if path_params and not has_pagination:
                return 'tier-1'
            return 'tier-2'
        elif method_lower in ('post', 'delete'):
            return 'tier-2'
        elif method_lower in ('put', 'patch'):
            return 'tier-3'
        else:
            return 'tier-2'

    def _generate_output(self, title: str) -> str:
        """Generate complete SLOP module based on storage mode"""
        lines = [
            ";; Generated from OpenAPI specification",
            '(@derived-from "openapi")',
            "(@generation-mode deterministic)",
            "",
        ]

        # Types
        if self.types:
            lines.append(";; Types")
            for t in self.types:
                lines.append(t.to_slop())
                lines.append("")

        # Storage mode-specific output
        if self.storage_mode == 'stub' and self.resource_types:
            # Generate ID types for stub mode (same as map mode)
            for resource in self.resource_types:
                lines.append(f"(type {resource}Id (Int 1 ..))")
            lines.append("")
            lines.append(self._generate_requires_block())
            lines.append("")
        elif self.storage_mode == 'map' and self.resource_types:
            lines.append(self._generate_state_types())
            lines.append("")
            lines.append(self._generate_state_functions())
            lines.append("")

        # Functions
        if self.functions:
            lines.append(";; API Endpoints")
            for fn in self.functions:
                lines.append(fn.to_slop())
                lines.append("")

        return '\n'.join(lines)

    def _generate_requires_block(self) -> str:
        """Generate (@requires storage ...) block for stub mode"""
        # Build type map from API function specs
        storage_types = self._build_storage_type_map()

        lines = [";; Storage requirements (interactive - resolve before filling holes)"]
        lines.append("(@requires storage")
        lines.append('  :prompt "Which storage approach for this API?"')
        lines.append("  :options (")
        lines.append('    ("In-memory Map - simple, good for prototypes" map)')
        lines.append('    ("Database stubs - I\'ll provide db-* implementations" db)')
        lines.append('    ("Custom - I\'ll implement storage myself" custom))')

        # Generate function signatures for each resource type
        for resource in self.resource_types:
            resource_lower = self._to_kebab(resource)
            lines.append(f"  ;; {resource} storage functions")

            # Look up derived types, with fallbacks
            get_fn = f"state-get-{resource_lower}"
            list_fn = f"state-list-{resource_lower}s"
            insert_fn = f"state-insert-{resource_lower}"
            delete_fn = f"state-delete-{resource_lower}"

            get_ret = storage_types.get(get_fn, f"(Option {resource})")
            list_ret = storage_types.get(list_fn, f"(List {resource})")
            insert_ret = storage_types.get(insert_fn, resource)
            delete_ret = storage_types.get(delete_fn, "Bool")

            lines.append(f"  ({get_fn} ((state (Ptr State)) (id {resource}Id)) -> {get_ret})")
            lines.append(f"  ({list_fn} ((state (Ptr State)) (limit (Option Int))) -> {list_ret})")
            lines.append(f"  ({insert_fn} ((arena Arena) (state (Ptr State)) (item (Ptr New{resource}))) -> {insert_ret})")
            lines.append(f"  ({delete_fn} ((state (Ptr State)) (id {resource}Id)) -> {delete_ret})")

        lines.append(")")
        return '\n'.join(lines)

    def _generate_state_types(self) -> str:
        """Generate State type for map mode"""
        lines = [";; State (generated for Map-based storage)"]

        # Generate ID types
        for resource in self.resource_types:
            lines.append(f"(type {resource}Id (Int 1 ..))")

        # Generate State record with maps for each resource
        state_fields = []
        for resource in self.resource_types:
            resource_lower = self._to_kebab(resource)
            state_fields.append(f"  ({resource_lower}s (Map {resource}Id {resource}))")
            state_fields.append(f"  (next-{resource_lower}-id {resource}Id)")
        lines.append("(type State (record")
        lines.extend(state_fields)
        lines.append("))")

        return '\n'.join(lines)

    def _generate_state_functions(self) -> str:
        """Generate CRUD helper functions for map mode"""
        # Build type map from API function specs
        storage_types = self._build_storage_type_map()

        lines = [";; State helper functions (deterministic - no holes)"]

        # State constructor
        lines.append("""(fn state-new ((arena Arena))
  (@intent "Create new empty state")
  (@spec ((Arena) -> (Ptr State)))
  (let ((s (cast (Ptr State) (arena-alloc arena (sizeof State)))))""")

        for resource in self.resource_types:
            resource_lower = self._to_kebab(resource)
            lines.append(f"    (set! s {resource_lower}s (map-empty))")
            lines.append(f"    (set! s next-{resource_lower}-id 1)")
        lines.append("    s))")
        lines.append("")

        # Generate make-{resource}-from-new for each resource type
        for resource in self.resource_types:
            resource_lower = self._to_kebab(resource)
            new_type = f"New{resource}"

            # Get fields from NewX (these are the fields to copy)
            new_fields = self.schema_fields.get(new_type, [])

            # Generate field copy statements
            field_copies = [f"    (set! p {f} (. item {f}))" for f in new_fields]
            field_copies_str = '\n'.join(field_copies) if field_copies else "    ;; no fields to copy"

            lines.append(f"""(fn make-{resource_lower}-from-new ((arena Arena) (id {resource}Id) (item (Ptr {new_type})))
  (@intent "Create {resource} from {new_type} with assigned ID")
  (@spec ((Arena {resource}Id (Ptr {new_type})) -> {resource}))
  (let ((p (cast (Ptr {resource}) (arena-alloc arena (sizeof {resource})))))
    (set! p id id)
{field_copies_str}
    (deref p)))
""")

        # Generate CRUD for each resource
        for resource in self.resource_types:
            resource_lower = self._to_kebab(resource)

            # Look up derived types, with fallbacks
            get_fn = f"state-get-{resource_lower}"
            list_fn = f"state-list-{resource_lower}s"
            insert_fn = f"state-insert-{resource_lower}"
            delete_fn = f"state-delete-{resource_lower}"

            get_ret = storage_types.get(get_fn, f"(Option {resource})")
            list_ret = storage_types.get(list_fn, f"(List {resource})")
            insert_ret = storage_types.get(insert_fn, resource)
            delete_ret = storage_types.get(delete_fn, "Bool")

            lines.append(f"""(fn {get_fn} ((state (Ptr State)) (id {resource}Id))
  (@intent "Get {resource_lower} by ID from state")
  (@spec (((Ptr State) {resource}Id) -> {get_ret}))
  (@pure)
  (map-get (. state {resource_lower}s) id))
""")
            lines.append(f"""(fn {list_fn} ((state (Ptr State)) (limit (Option Int)))
  (@intent "List all {resource_lower}s from state")
  (@spec (((Ptr State) (Option Int)) -> {list_ret}))
  (@pure)
  (map-values (. state {resource_lower}s)))
""")
            lines.append(f"""(fn {insert_fn} ((arena Arena) (state (Ptr State)) (item (Ptr New{resource})))
  (@intent "Insert new {resource_lower} into state")
  (@spec ((Arena (Ptr State) (Ptr New{resource})) -> {insert_ret}))
  (let ((id (. state next-{resource_lower}-id)))
    (set! state next-{resource_lower}-id (+ id 1))
    (let ((new-item (make-{resource_lower}-from-new arena id item)))
      (map-put (. state {resource_lower}s) id new-item)
      new-item)))
""")
            lines.append(f"""(fn {delete_fn} ((state (Ptr State)) (id {resource}Id))
  (@intent "Delete {resource_lower} from state by ID")
  (@spec (((Ptr State) {resource}Id) -> {delete_ret}))
  (if (map-has (. state {resource_lower}s) id)
    (do (map-remove (. state {resource_lower}s) id) true)
    false))
""")

        return '\n'.join(lines)

    def _path_to_function_name(self, method: str, path: str) -> str:
        """Convert HTTP method + path to function name"""
        # Replace path params with 'by-{param}'
        parts = []
        segments = [s for s in path.split('/') if s]

        for seg in segments:
            if seg.startswith('{') and seg.endswith('}'):
                param = seg[1:-1]
                parts.append(f"by-{self._to_kebab(param)}")
            else:
                parts.append(self._to_kebab(seg))

        # Map HTTP verbs to action prefixes
        verb_map = {
            'get': 'get',
            'post': 'create',
            'put': 'update',
            'patch': 'patch',
            'delete': 'delete'
        }
        prefix = verb_map.get(method.lower(), method.lower())

        return f"{prefix}-{'-'.join(parts)}"

    def _to_type_name(self, s: str) -> str:
        """Convert string to PascalCase type name"""
        # Split on separators AND case boundaries to preserve PascalCase
        s = re.sub(r'([a-z])([A-Z])', r'\1_\2', s)
        words = re.split(r'[-_\s]+', s)
        return ''.join(w.capitalize() for w in words)

    def _to_kebab(self, s: str) -> str:
        """Convert string to kebab-case"""
        s = re.sub(r'([a-z])([A-Z])', r'\1-\2', s)
        return s.lower().replace('_', '-').replace(' ', '-')


def detect_schema_format(data: dict) -> str:
    """Detect schema format from parsed content"""
    if "openapi" in data:
        return "openapi"
    elif "swagger" in data:
        return "swagger"  # OpenAPI 2.x
    elif "paths" in data:
        return "openapi"
    else:
        return "jsonschema"


def convert_json_schema(schema_path: str) -> str:
    """Convert JSON Schema file to SLOP"""
    with open(schema_path) as f:
        schema = json.load(f)

    name = schema.get("title", "Root")
    return JsonSchemaConverter().convert(schema, name)


def convert_sql(sql_path: str) -> str:
    """Convert SQL DDL file to SLOP"""
    with open(sql_path) as f:
        sql = f.read()

    return SqlSchemaConverter().convert(sql)


def convert_openapi(spec_path: str) -> str:
    """Convert OpenAPI spec file to SLOP"""
    spec = _load_spec(spec_path)
    return OpenApiConverter().convert(spec)


def _load_spec(path: str) -> dict:
    """Load spec from JSON or YAML file"""
    if path.endswith(('.yaml', '.yml')):
        try:
            import yaml
            with open(path) as f:
                return yaml.safe_load(f)
        except ImportError:
            raise ImportError(
                "PyYAML required for YAML files. Install with: pip install pyyaml"
            )
    else:
        with open(path) as f:
            return json.load(f)


if __name__ == '__main__':
    # Test JSON Schema conversion
    test_schema = {
        "title": "User",
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "email": {"type": "string", "format": "email"},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
            "status": {"type": "string", "enum": ["active", "inactive", "deleted"]}
        },
        "required": ["id", "name", "email"]
    }

    print("=== JSON Schema → SLOP ===")
    print(JsonSchemaConverter().convert(test_schema, "User"))

    # Test SQL conversion
    test_sql = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY NOT NULL,
        name VARCHAR(100) NOT NULL,
        email VARCHAR(255) NOT NULL,
        age INTEGER,
        created_at TIMESTAMP NOT NULL
    );
    """

    print("\n=== SQL DDL → SLOP ===")
    print(SqlSchemaConverter().convert(test_sql))

    # Test OpenAPI conversion
    test_openapi = {
        "openapi": "3.0.0",
        "info": {"title": "User API", "version": "1.0.0"},
        "paths": {
            "/users/{id}": {
                "get": {
                    "summary": "Get user by ID",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1},
                            "example": 42
                        }
                    ],
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"},
                                    "example": {"id": 42, "name": "Alice", "email": "alice@test.com"}
                                }
                            }
                        },
                        "404": {"description": "User not found"}
                    }
                }
            },
            "/users": {
                "post": {
                    "summary": "Create a new user",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/CreateUser"}
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/User"}
                                }
                            }
                        },
                        "400": {"description": "Bad request"}
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "minimum": 1},
                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                        "email": {"type": "string", "format": "email"}
                    },
                    "required": ["id", "name", "email"]
                },
                "CreateUser": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                        "email": {"type": "string", "format": "email"}
                    },
                    "required": ["name", "email"]
                }
            }
        }
    }

    print("\n=== OpenAPI → SLOP ===")
    print(OpenApiConverter().convert(test_openapi))
