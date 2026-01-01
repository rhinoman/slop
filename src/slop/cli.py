#!/usr/bin/env python3
"""
SLOP CLI - Command-line interface for SLOP toolchain

Commands:
  parse      Parse and validate SLOP files
  transpile  Convert SLOP to C
  fill       Fill holes with LLM generation
  check      Validate types and contracts
  build      Full pipeline: fill → transpile → compile
  derive     Generate SLOP types from schemas (JSON Schema, OpenAPI, SQL)
"""

import argparse
import sys
import os
from pathlib import Path

from slop.parser import parse, parse_file, pretty_print, find_holes, is_form, SList, get_imports
from slop.transpiler import Transpiler, transpile, transpile_multi
from slop.hole_filler import HoleFiller, extract_hole, classify_tier, replace_holes_in_ast
from slop.formatter import format_source
from slop.providers import (
    MockProvider, create_default_configs, load_config, create_from_config, Tier,
    load_project_config, ProjectConfig, BuildConfig
)
from slop.type_checker import TypeChecker, check_file, check_modules
from slop.resolver import ModuleResolver, ResolverError


def extract_requires_blocks(ast):
    """Extract @requires annotations from AST.

    Returns list of dicts with:
      - category: the requires category (e.g., 'storage')
      - has_prompt: whether it has an interactive :prompt
      - prompt: the prompt text if present
      - functions: list of required function signatures
    """
    requires = []
    for form in ast:
        if is_form(form, '@requires') and len(form) > 1:
            from slop.parser import Symbol, String
            category = form[1].name if isinstance(form[1], Symbol) else str(form[1])

            has_prompt = False
            prompt_text = None
            functions = []

            i = 2
            while i < len(form):
                item = form[i]
                if isinstance(item, Symbol) and item.name == ':prompt' and i + 1 < len(form):
                    has_prompt = True
                    prompt_text = form[i + 1].value if isinstance(form[i + 1], String) else str(form[i + 1])
                    i += 2
                elif isinstance(item, Symbol) and item.name == ':options':
                    i += 2  # Skip :options and its value
                elif isinstance(item, SList):
                    # This is a function signature
                    functions.append(pretty_print(item))
                    i += 1
                elif isinstance(item, Symbol) and str(item).startswith(';;'):
                    i += 1  # Skip comments
                else:
                    i += 1

            requires.append({
                'category': category,
                'has_prompt': has_prompt,
                'prompt': prompt_text,
                'functions': functions
            })
    return requires


def cmd_parse(args):
    """Parse and display SLOP file"""
    try:
        ast = parse_file(args.input)

        if args.holes:
            total = 0
            for form in ast:
                holes = find_holes(form)
                for h in holes:
                    info = extract_hole(h)
                    tier = classify_tier(info)
                    total += 1
                    print(f"Hole: {info.prompt}")
                    print(f"  Type: {info.type_expr}")
                    print(f"  Tier: {tier.name}")
                    if info.context:
                        print(f"  Context: {', '.join(info.context)}")
                    if info.required:
                        print(f"  Required: {', '.join(info.required)}")
                    print()
            print(f"Found {total} holes", file=sys.stderr)
        else:
            for form in ast:
                print(pretty_print(form))
                print()

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_transpile(args):
    """Transpile SLOP to C (single or multi-module)"""
    import os
    from slop.transpiler import transpile_multi_split

    try:
        input_path = Path(args.input)

        # Parse entry file to check for imports
        with open(input_path) as f:
            source = f.read()
        ast = parse(source)

        if _has_imports(ast):
            # Multi-module path
            search_paths = [Path(p) for p in args.include]
            resolver = ModuleResolver(search_paths)

            graph = resolver.build_dependency_graph(input_path)
            errors = resolver.validate_imports(graph)
            if errors:
                for e in errors:
                    print(f"Import error: {e}", file=sys.stderr)
                return 1

            order = resolver.topological_sort(graph)

            # Check if output is a directory (ends with /)
            if args.output and args.output.endswith('/'):
                # Multi-file output: separate .h/.c per module
                os.makedirs(args.output, exist_ok=True)
                results = transpile_multi_split(graph.modules, order)
                for mod_name, (header, impl) in results.items():
                    header_path = os.path.join(args.output, f"{mod_name}.h")
                    impl_path = os.path.join(args.output, f"{mod_name}.c")
                    with open(header_path, 'w') as f:
                        f.write(header)
                    with open(impl_path, 'w') as f:
                        f.write(impl)
                    print(f"Wrote {header_path}")
                    print(f"Wrote {impl_path}")
                return 0
            else:
                # Single combined file output
                c_code = transpile_multi(graph.modules, order)
        else:
            # Single-file path (backward compatible)
            c_code = transpile(source)

        if args.output:
            with open(args.output, 'w') as f:
                f.write(c_code)
            print(f"Wrote {args.output}")
        else:
            print(c_code)

        return 0
    except ResolverError as e:
        print(f"Module resolution error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _extract_fn_spec(form: SList) -> dict:
    """Extract function name, params, and return type from fn form."""
    from slop.parser import Symbol
    if len(form) < 3:
        return None
    name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
    params = str(form[2])  # ((x Type) (y Type))

    # Find @spec annotation for return type
    return_type = None
    for item in form.items[3:]:
        if is_form(item, '@spec') and len(item) > 1:
            spec = item[1]
            if hasattr(spec, 'items') and len(spec.items) >= 3:
                return_type = str(spec.items[-1])  # Last element is return type
            break

    return {'name': name, 'params': params, 'return_type': return_type}


def _extract_ffi_functions(ast) -> list:
    """Extract function names from FFI declarations.

    Handles both top-level (ffi ...) and (module ... (ffi ...)) forms.
    """
    from slop.parser import Symbol
    ffi_functions = []

    for form in ast:
        if is_form(form, 'ffi'):
            # (ffi "header.h" (func1 ...) (func2 ...) ...)
            for item in form.items[2:]:  # Skip 'ffi' and header
                if isinstance(item, SList) and len(item) >= 1:
                    if isinstance(item[0], Symbol):
                        ffi_functions.append(item[0].name)
        elif is_form(form, 'module'):
            # Look inside module for ffi declarations
            for subform in form.items:
                if is_form(subform, 'ffi'):
                    for item in subform.items[2:]:
                        if isinstance(item, SList) and len(item) >= 1:
                            if isinstance(item[0], Symbol):
                                ffi_functions.append(item[0].name)

    return ffi_functions


def _extract_const_names(ast) -> list:
    """Extract constant names from const declarations.

    Handles both top-level (const ...) and (module ... (const ...)) forms.
    Format: (const NAME Type value)
    """
    from slop.parser import Symbol
    const_names = []

    for form in ast:
        if is_form(form, 'const') and len(form) >= 2:
            if isinstance(form[1], Symbol):
                const_names.append(form[1].name)
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'const') and len(subform) >= 2:
                    if isinstance(subform[1], Symbol):
                        const_names.append(subform[1].name)

    return const_names


def _extract_const_specs(ast) -> list:
    """Extract constant specs (name + type) from const declarations.

    Format: (const NAME Type value)
    Returns list of dicts with 'name' and 'type_expr' (as string)
    """
    from slop.parser import Symbol
    const_specs = []

    def extract_const(form):
        if is_form(form, 'const') and len(form) >= 3:
            if isinstance(form[1], Symbol):
                name = form[1].name
                type_expr = pretty_print(form[2])
                const_specs.append({'name': name, 'type_expr': type_expr})

    for form in ast:
        extract_const(form)
        if is_form(form, 'module'):
            for subform in form.items:
                extract_const(subform)

    return const_specs


def _extract_ffi_specs(ast) -> list:
    """Extract full FFI function specs with return types.

    Handles both top-level (ffi ...) and (module ... (ffi ...)) forms.
    Returns list of dicts with 'name', 'params', 'return_type'.

    FFI form: (ffi "header.h" (func-name ((param Type)...) ReturnType) ...)
    """
    from slop.parser import Symbol, pretty_print
    ffi_specs = []

    def extract_from_ffi_form(ffi_form):
        for item in ffi_form.items[2:]:  # Skip 'ffi' and header
            if isinstance(item, SList) and len(item) >= 3:
                # (func-name ((param Type)...) ReturnType)
                fn_name = item[0].name if isinstance(item[0], Symbol) else str(item[0])
                params_list = item[1] if isinstance(item[1], SList) else SList([])
                return_type_expr = item[2]

                # Pretty-print params and return type for the validator
                params_str = pretty_print(params_list)
                return_type_str = pretty_print(return_type_expr)

                ffi_specs.append({
                    'name': fn_name,
                    'params': params_str,
                    'return_type': return_type_str
                })

    for form in ast:
        if is_form(form, 'ffi'):
            extract_from_ffi_form(form)
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'ffi'):
                    extract_from_ffi_form(subform)

    return ffi_specs


def _extract_imported_functions(ast) -> list:
    """Extract function and type names from import declarations.

    Handles both top-level (import ...) and (module ... (import ...)) forms.
    Supports two import formats:
      - (import mod func1 func2 Type1 Type2)  -- direct list
      - (import mod (func1 func2 Type1 Type2))  -- grouped in SList
    """
    from slop.parser import Symbol
    imported_names = []

    def extract_from_import(import_form):
        """Extract names from an import form."""
        # Skip 'import' keyword and module name
        for item in import_form.items[2:]:
            if isinstance(item, SList):
                # Grouped import list: (func1 func2 Type1 Type2)
                # Extract ALL names from the list
                for sub_item in item.items:
                    if isinstance(sub_item, Symbol):
                        imported_names.append(sub_item.name)
            elif isinstance(item, Symbol):
                # Direct import: bare symbol
                imported_names.append(item.name)

    for form in ast:
        if is_form(form, 'import'):
            extract_from_import(form)
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'import'):
                    extract_from_import(subform)

    return imported_names


def _parse_import_form(import_form) -> tuple:
    """Parse import form, return (module_name, [imported_names])."""
    from slop.parser import Symbol

    if len(import_form) < 2:
        return (None, [])

    module_name = import_form[1].name if isinstance(import_form[1], Symbol) else str(import_form[1])
    names = []

    for item in import_form.items[2:]:
        if isinstance(item, Symbol):
            names.append(item.name)
        elif isinstance(item, SList):
            for sub in item.items:
                if isinstance(sub, Symbol):
                    names.append(sub.name)

    return (module_name, names)


def _extract_imported_specs(ast, search_paths=None, from_path=None) -> list:
    """Extract function signatures from imported modules.

    Args:
        ast: Parsed AST of the importing file
        search_paths: List of paths to search for modules
        from_path: Path of the importing file (for relative resolution)

    Returns list of dicts: {'name': str, 'params': str, 'return_type': str}
    """
    from slop.resolver import ModuleResolver

    resolver = ModuleResolver([Path(p) for p in (search_paths or [])])
    imported_specs = []

    # Collect all import info: [(module_name, [imported_names]), ...]
    imports = []
    for form in ast:
        if is_form(form, 'import'):
            imports.append(_parse_import_form(form))
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'import'):
                    imports.append(_parse_import_form(subform))

    # For each imported module, load and extract specs
    for module_name, imported_names in imports:
        if not module_name:
            continue
        imported_set = set(imported_names)
        try:
            module_path = resolver.resolve_module(module_name, from_path)
            module_ast = parse_file(str(module_path))

            # Extract all fn specs from module
            for form in module_ast:
                if is_form(form, 'fn'):
                    spec = _extract_fn_spec(form)
                    if spec and spec['name'] in imported_set:
                        imported_specs.append(spec)
                elif is_form(form, 'module'):
                    for item in form.items:
                        if is_form(item, 'fn'):
                            spec = _extract_fn_spec(item)
                            if spec and spec['name'] in imported_set:
                                imported_specs.append(spec)
        except Exception:
            # Module not found - skip, will error during type checking
            pass

    return imported_specs


def _extract_imported_types(ast, search_paths=None, from_path=None) -> list:
    """Extract type definitions from imported modules.

    Args:
        ast: Parsed AST of the importing file
        search_paths: List of paths to search for modules
        from_path: Path of the importing file (for relative resolution)

    Returns list of dicts: {'name': str, 'type_def': str}
    where type_def is the pretty-printed (type Name ...) form.
    """
    from slop.resolver import ModuleResolver
    from slop.parser import Symbol

    resolver = ModuleResolver([Path(p) for p in (search_paths or [])])
    imported_types = []

    # Collect all import info: [(module_name, [imported_names]), ...]
    imports = []
    for form in ast:
        if is_form(form, 'import'):
            imports.append(_parse_import_form(form))
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'import'):
                    imports.append(_parse_import_form(subform))

    # For each imported module, load and extract type definitions
    for module_name, imported_names in imports:
        if not module_name:
            continue
        imported_set = set(imported_names)
        try:
            module_path = resolver.resolve_module(module_name, from_path)
            module_ast = parse_file(str(module_path))

            # Extract type definitions from module
            for form in module_ast:
                if is_form(form, 'type') and len(form) > 1:
                    name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
                    if name in imported_set:
                        imported_types.append({
                            'name': name,
                            'type_def': pretty_print(form)
                        })
                elif is_form(form, 'module'):
                    for item in form.items:
                        if is_form(item, 'type') and len(item) > 1:
                            name = item[1].name if isinstance(item[1], Symbol) else str(item[1])
                            if name in imported_set:
                                imported_types.append({
                                    'name': name,
                                    'type_def': pretty_print(item)
                                })
        except Exception:
            # Module not found - skip, will error during type checking
            pass

    return imported_types


def extract_file_context(filepath: str, fn_name: str = None) -> dict:
    """Extract type checking context from a .slop file.

    Args:
        filepath: Path to the .slop file
        fn_name: If provided, also extract params from this function

    Returns:
        Context dictionary with type_defs, fn_specs, ffi_specs, const_specs,
        imported_specs, imported_types, and params (if fn_name provided).
    """
    from slop.parser import Symbol

    ast = parse_file(filepath)

    # Extract type definitions
    type_defs = []
    for form in ast:
        if is_form(form, 'type') and len(form) > 1:
            type_defs.append(pretty_print(form))
        elif is_form(form, 'record') and len(form) > 1:
            type_defs.append(pretty_print(form))
        elif is_form(form, 'enum') and len(form) > 1:
            type_defs.append(pretty_print(form))
        elif is_form(form, 'module'):
            for item in form.items:
                if is_form(item, 'type') and len(item) > 1:
                    type_defs.append(pretty_print(item))
                elif is_form(item, 'record') and len(item) > 1:
                    type_defs.append(pretty_print(item))
                elif is_form(item, 'enum') and len(item) > 1:
                    type_defs.append(pretty_print(item))

    # Extract function specs
    fn_specs = []
    for form in ast:
        if is_form(form, 'fn'):
            spec = _extract_fn_spec(form)
            if spec:
                fn_specs.append(spec)
        elif is_form(form, 'module'):
            for item in form.items:
                if is_form(item, 'fn'):
                    spec = _extract_fn_spec(item)
                    if spec:
                        fn_specs.append(spec)

    # Extract FFI specs
    ffi_specs = _extract_ffi_specs(ast)

    # Extract const specs
    const_specs = _extract_const_specs(ast)

    # Extract imported specs and types
    from_path = Path(filepath)
    imported_specs = _extract_imported_specs(ast, from_path=from_path)
    imported_types = _extract_imported_types(ast, from_path=from_path)

    context = {
        'type_defs': type_defs,
        'fn_specs': fn_specs,
        'ffi_specs': ffi_specs,
        'const_specs': const_specs,
        'imported_specs': imported_specs,
        'imported_types': imported_types,
        'params': '',
    }

    # If fn_name provided, find that function and extract its params
    if fn_name:
        for form in ast:
            if is_form(form, 'fn') and len(form) > 2:
                name = form[1].name if isinstance(form[1], Symbol) else str(form[1])
                if name == fn_name:
                    context['params'] = str(form[2])
                    break
            elif is_form(form, 'module'):
                for item in form.items:
                    if is_form(item, 'fn') and len(item) > 2:
                        name = item[1].name if isinstance(item[1], Symbol) else str(item[1])
                        if name == fn_name:
                            context['params'] = str(item[2])
                            break

    return context


def extract_documentation(ast) -> dict:
    """Extract structured documentation from SLOP AST.

    Returns dict with module info, types, functions, and constants.
    """
    from slop.parser import Symbol, String

    doc = {
        'module': None,
        'exports': [],
        'types': [],
        'functions': [],
        'constants': [],
    }

    # Find module name and exports
    for form in ast:
        if is_form(form, 'module') and len(form) >= 2:
            if isinstance(form[1], Symbol):
                doc['module'] = form[1].name
            # Check for export list
            for item in form.items[2:]:
                if is_form(item, 'export'):
                    for exp in item.items[1:]:
                        if isinstance(exp, Symbol):
                            doc['exports'].append(exp.name)
                        elif isinstance(exp, SList) and len(exp) >= 1:
                            # (fn-name arity) form
                            if isinstance(exp[0], Symbol):
                                doc['exports'].append(exp[0].name)
            break

    export_set = set(doc['exports'])

    def extract_type_info(type_form) -> dict:
        """Extract type definition info."""
        if len(type_form) < 2:
            return None

        name = type_form[1].name if isinstance(type_form[1], Symbol) else str(type_form[1])
        type_info = {'name': name, 'definition': pretty_print(type_form)}

        if len(type_form) > 2:
            body = type_form[2]
            if is_form(body, 'record'):
                type_info['kind'] = 'record'
                type_info['fields'] = []
                for field in body.items[1:]:
                    if isinstance(field, SList) and len(field) >= 2:
                        fname = field[0].name if isinstance(field[0], Symbol) else str(field[0])
                        ftype = pretty_print(field[1]) if len(field) > 1 else 'Unknown'
                        type_info['fields'].append({'name': fname, 'type': ftype})
            elif is_form(body, 'enum'):
                type_info['kind'] = 'enum'
                type_info['variants'] = []
                for v in body.items[1:]:
                    if isinstance(v, Symbol):
                        type_info['variants'].append(v.name)
            elif isinstance(body, SList) and len(body) >= 3:
                # Range type: (Int min .. max)
                type_info['kind'] = 'range'
            else:
                type_info['kind'] = 'alias'
        else:
            type_info['kind'] = 'alias'

        return type_info

    def extract_fn_info(fn_form) -> dict:
        """Extract function documentation info."""
        if len(fn_form) < 3:
            return None

        name = fn_form[1].name if isinstance(fn_form[1], Symbol) else str(fn_form[1])

        fn_info = {
            'name': name,
            'exported': name in export_set or not doc['exports'],
            'params': [],
            'intent': None,
            'spec': None,
            'pre': [],
            'post': [],
            'examples': [],
            'pure': False,
            'alloc': None,
        }

        # Extract parameters
        params_list = fn_form[2]
        if isinstance(params_list, SList):
            for p in params_list.items:
                if isinstance(p, SList) and len(p) >= 2:
                    # Handle different param forms: (name Type), (in name Type), (out name Type)
                    if isinstance(p[0], Symbol) and p[0].name in ('in', 'out', 'inout'):
                        # Directional param: (in name Type)
                        pname = p[1].name if isinstance(p[1], Symbol) else str(p[1])
                        ptype = pretty_print(p[2]) if len(p) > 2 else 'Unknown'
                        fn_info['params'].append({'name': pname, 'type': ptype, 'direction': p[0].name})
                    else:
                        # Regular param: (name Type)
                        pname = p[0].name if isinstance(p[0], Symbol) else str(p[0])
                        ptype = pretty_print(p[1]) if len(p) > 1 else 'Unknown'
                        fn_info['params'].append({'name': pname, 'type': ptype})

        def compact(s: str) -> str:
            """Normalize whitespace to single spaces."""
            import re
            return re.sub(r'\s+', ' ', s).strip()

        # Extract annotations
        for item in fn_form.items[3:]:
            if is_form(item, '@intent') and len(item) > 1:
                fn_info['intent'] = item[1].value if isinstance(item[1], String) else str(item[1])
            elif is_form(item, '@spec') and len(item) > 1:
                fn_info['spec'] = compact(pretty_print(item[1]))
            elif is_form(item, '@pre') and len(item) > 1:
                fn_info['pre'].append(compact(pretty_print(item[1])))
            elif is_form(item, '@post') and len(item) > 1:
                fn_info['post'].append(compact(pretty_print(item[1])))
            elif is_form(item, '@example') and len(item) > 1:
                # Format: (@example (args...) -> result)
                example_str = compact(pretty_print(SList(item.items[1:])))
                fn_info['examples'].append(example_str)
            elif is_form(item, '@pure'):
                fn_info['pure'] = True
            elif is_form(item, '@alloc') and len(item) > 1:
                fn_info['alloc'] = item[1].name if isinstance(item[1], Symbol) else str(item[1])

        return fn_info

    def extract_const_info(const_form) -> dict:
        """Extract constant documentation info."""
        if len(const_form) < 4:
            return None

        name = const_form[1].name if isinstance(const_form[1], Symbol) else str(const_form[1])
        type_expr = pretty_print(const_form[2])
        value = pretty_print(const_form[3])

        return {'name': name, 'type': type_expr, 'value': value}

    # Process top-level forms
    for form in ast:
        if is_form(form, 'type'):
            info = extract_type_info(form)
            if info:
                doc['types'].append(info)
        elif is_form(form, 'fn') or is_form(form, 'impl'):
            info = extract_fn_info(form)
            if info:
                doc['functions'].append(info)
        elif is_form(form, 'const'):
            info = extract_const_info(form)
            if info:
                doc['constants'].append(info)
        elif is_form(form, 'module'):
            # Process forms inside module
            for item in form.items:
                if is_form(item, 'type'):
                    info = extract_type_info(item)
                    if info:
                        doc['types'].append(info)
                elif is_form(item, 'fn') or is_form(item, 'impl'):
                    info = extract_fn_info(item)
                    if info:
                        doc['functions'].append(info)
                elif is_form(item, 'const'):
                    info = extract_const_info(item)
                    if info:
                        doc['constants'].append(info)

    return doc


def render_markdown(doc: dict) -> str:
    """Render documentation dict as Markdown."""
    lines = []

    # Module header
    if doc['module']:
        lines.append(f"# {doc['module']}")
    else:
        lines.append("# Module Documentation")
    lines.append("")

    # Types section
    if doc['types']:
        lines.append("## Types")
        lines.append("")

        for t in doc['types']:
            lines.append(f"### {t['name']}")
            lines.append("")
            lines.append("```lisp")
            lines.append(t['definition'])
            lines.append("```")

            if t['kind'] == 'record' and t.get('fields'):
                lines.append("")
                lines.append("**Fields:**")
                for f in t['fields']:
                    lines.append(f"- `{f['name']}` — {f['type']}")
            elif t['kind'] == 'enum' and t.get('variants'):
                lines.append("")
                lines.append("**Variants:** " + ", ".join(f"`{v}`" for v in t['variants']))

            lines.append("")
            lines.append("---")
            lines.append("")

    # Constants section
    if doc['constants']:
        lines.append("## Constants")
        lines.append("")

        for c in doc['constants']:
            lines.append(f"### {c['name']}")
            lines.append("")
            lines.append(f"**Type:** `{c['type']}`")
            lines.append("")
            lines.append(f"**Value:** `{c['value']}`")
            lines.append("")
            lines.append("---")
            lines.append("")

    # Functions section
    if doc['functions']:
        lines.append("## Functions")
        lines.append("")

        for fn in doc['functions']:
            # Function name with internal marker if not exported
            if fn['exported']:
                lines.append(f"### {fn['name']}")
            else:
                lines.append(f"### {fn['name']} *(internal)*")
            lines.append("")

            # Intent
            if fn['intent']:
                lines.append(f"> {fn['intent']}")
                lines.append("")

            # Signature
            if fn['spec']:
                lines.append(f"**Signature:** `{fn['spec']}`")
                lines.append("")

            # Parameters
            if fn['params']:
                lines.append("**Parameters:**")
                for p in fn['params']:
                    direction = f" *({p['direction']})* " if p.get('direction') else " "
                    lines.append(f"- `{p['name']}`{direction}— {p['type']}")
                lines.append("")

            # Preconditions
            if fn['pre']:
                lines.append("**Preconditions:**")
                for pre in fn['pre']:
                    lines.append(f"- `{pre}`")
                lines.append("")

            # Postconditions
            if fn['post']:
                lines.append("**Postconditions:**")
                for post in fn['post']:
                    lines.append(f"- `{post}`")
                lines.append("")

            # Examples
            if fn['examples']:
                lines.append("**Examples:**")
                lines.append("```lisp")
                for ex in fn['examples']:
                    lines.append(f"({fn['name']} {ex})")
                lines.append("```")
                lines.append("")

            # Notes (pure, alloc)
            notes = []
            if fn['pure']:
                notes.append("Pure function")
            if fn['alloc']:
                notes.append(f"Allocates in `{fn['alloc']}`")
            if notes:
                lines.append("*" + " | ".join(notes) + "*")
                lines.append("")

            lines.append("---")
            lines.append("")

    return "\n".join(lines)


def cmd_doc(args):
    """Generate documentation from SLOP source."""
    import json

    try:
        ast = parse_file(args.input)
        doc = extract_documentation(ast)

        if args.format == 'json':
            output = json.dumps(doc, indent=2)
        else:
            output = render_markdown(doc)

        if args.output:
            Path(args.output).write_text(output)
            print(f"Wrote {args.output}")
        else:
            print(output)

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_fill(args):
    """Fill holes with LLM"""
    import logging
    if args.verbose >= 2:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(name)s %(levelname)s: %(message)s'
        )
    elif args.verbose >= 1 or not args.quiet:
        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s'
        )

    logger = logging.getLogger(__name__)
    quiet = args.quiet

    try:
        # Load project config for entry point
        config_path = getattr(args, 'config', None)
        project_cfg, _ = load_project_config(config_path)

        # Determine input file
        if hasattr(args, 'input') and args.input:
            input_file = args.input
        elif project_cfg and project_cfg.entry:
            input_file = project_cfg.entry
            if not quiet:
                print(f"Using entry from slop.toml: {input_file}")
        else:
            print("Error: No input file specified and no [project].entry in slop.toml", file=sys.stderr)
            return 1

        ast = parse_file(input_file)

        # Pre-check scaffold for type errors before filling
        if not quiet:
            print("  Pre-checking scaffold...")

        diagnostics = check_file(input_file)
        type_errors = [d for d in diagnostics if d.severity == 'error']
        type_warnings = [d for d in diagnostics if d.severity == 'warning']

        # Show warnings but don't block
        if type_warnings and not quiet:
            for w in type_warnings:
                print(f"  warning: {w}")

        if type_errors:
            print(f"Scaffold has {len(type_errors)} type error(s) that must be fixed before filling:", file=sys.stderr)
            for e in type_errors:
                print(f"  {e}", file=sys.stderr)
            print("\nFix these errors in the scaffold file first.", file=sys.stderr)
            return 1

        # Check for @requires blocks
        requires_blocks = extract_requires_blocks(ast)
        requires_fns = []  # Function signatures from @requires for context

        for req in requires_blocks:
            if req['has_prompt']:
                # Interactive @requires - warn user
                print(f"Warning: File contains (@requires {req['category']} ...) with interactive :prompt", file=sys.stderr)
                print(f"  Prompt: {req['prompt']}", file=sys.stderr)
                print("  This scaffold needs resolution before holes can be filled reliably.", file=sys.stderr)
                print("  Use Claude Code or another LLM to resolve this @requires first.", file=sys.stderr)
                print("", file=sys.stderr)
            else:
                # Non-interactive @requires - add function signatures to context
                if not quiet:
                    print(f"Found (@requires {req['category']}) - adding {len(req['functions'])} function signatures to context")

            # Add function signatures for LLM context
            requires_fns.extend(req['functions'])

        # Collect type definitions and names from module for context
        type_defs = []
        type_names = []
        error_variants = {}  # Maps enum type name -> list of variant names

        def _extract_enum_variants(type_form):
            """Extract enum variants from a type definition.

            Handles both (type Name (enum v1 v2 ...)) and (enum v1 v2 ...) forms.
            """
            from slop.parser import Symbol
            name = None
            enum_form = None

            if is_form(type_form, 'type') and len(type_form) > 2:
                # (type Name (enum v1 v2 ...))
                if isinstance(type_form[1], Symbol):
                    name = type_form[1].name
                enum_expr = type_form[2]
                if is_form(enum_expr, 'enum'):
                    enum_form = enum_expr
            elif is_form(type_form, 'enum') and len(type_form) > 1:
                # (enum v1 v2 ...) - no name
                name = None
                enum_form = type_form

            if enum_form is not None:
                variants = []
                for v in enum_form.items[1:]:
                    if isinstance(v, Symbol):
                        variants.append(v.name)
                if name and variants:
                    error_variants[name] = variants
                return variants
            return []

        for form in ast:
            if is_form(form, 'type') and len(form) > 1:
                type_defs.append(pretty_print(form))
                from slop.parser import Symbol
                if isinstance(form[1], Symbol):
                    type_names.append(form[1].name)
                # Check if this type is an enum
                if len(form) > 2 and is_form(form[2], 'enum'):
                    variants = _extract_enum_variants(form)
                    for v in variants:
                        type_names.append(v)
            elif is_form(form, 'record') and len(form) > 1:
                type_defs.append(pretty_print(form))
                from slop.parser import Symbol
                if isinstance(form[1], Symbol):
                    type_names.append(form[1].name)
            elif is_form(form, 'enum') and len(form) > 1:
                type_defs.append(pretty_print(form))
                from slop.parser import Symbol
                if isinstance(form[1], Symbol):
                    type_names.append(form[1].name)
                # Also add enum variant names
                for item in form.items[2:]:
                    if isinstance(item, Symbol):
                        type_names.append(item.name)
            elif is_form(form, 'module'):
                for item in form.items:
                    if is_form(item, 'type') and len(item) > 1:
                        type_defs.append(pretty_print(item))
                        from slop.parser import Symbol
                        if isinstance(item[1], Symbol):
                            type_names.append(item[1].name)
                        # Check if this type is an enum
                        if len(item) > 2 and is_form(item[2], 'enum'):
                            variants = _extract_enum_variants(item)
                            for v in variants:
                                type_names.append(v)
                    elif is_form(item, 'record') and len(item) > 1:
                        type_defs.append(pretty_print(item))
                        from slop.parser import Symbol
                        if isinstance(item[1], Symbol):
                            type_names.append(item[1].name)
                    elif is_form(item, 'enum') and len(item) > 1:
                        type_defs.append(pretty_print(item))
                        from slop.parser import Symbol
                        if isinstance(item[1], Symbol):
                            type_names.append(item[1].name)
                        # Also add enum variant names
                        for variant in item.items[2:]:
                            if isinstance(variant, Symbol):
                                type_names.append(variant.name)

        # Collect function specs for context (name + params + return type)
        fn_specs = []
        for form in ast:
            if is_form(form, 'fn'):
                spec = _extract_fn_spec(form)
                if spec:
                    fn_specs.append(spec)
            elif is_form(form, 'module'):
                for item in form.items:
                    if is_form(item, 'fn'):
                        spec = _extract_fn_spec(item)
                        if spec:
                            fn_specs.append(spec)

        # Collect FFI function names/specs and imported names for validation
        ffi_functions = _extract_ffi_functions(ast)
        ffi_specs = _extract_ffi_specs(ast)
        imported_names = _extract_imported_functions(ast)
        const_names = _extract_const_names(ast)
        const_specs = _extract_const_specs(ast)
        # Extract full signatures from imported modules
        imported_specs = _extract_imported_specs(ast, from_path=Path(input_file))
        imported_types = _extract_imported_types(ast, from_path=Path(input_file))
        if not quiet:
            if imported_specs:
                print(f"  Extracted {len(imported_specs)} imported function specs")
                if args.verbose >= 2:  # -vv shows details
                    for spec in imported_specs:
                        print(f"    {spec['name']}: {spec['params']} -> {spec.get('return_type', '?')}")
            else:
                print("  Warning: No imported function specs extracted")
            if imported_types and args.verbose >= 2:
                print(f"  Extracted {len(imported_types)} imported type definitions")
                for t in imported_types:
                    print(f"    {t['name']}")

        # Collect all holes with their parent function forms for context
        all_holes = []
        for form in ast:
            if is_form(form, 'module'):
                # Look inside module for functions containing holes
                for item in form.items:
                    if is_form(item, 'fn') or is_form(item, 'impl'):
                        holes = find_holes(item)
                        for h in holes:
                            all_holes.append((item, h))  # item is the fn form
            else:
                # Top-level function
                holes = find_holes(form)
                for h in holes:
                    all_holes.append((form, h))

        if not all_holes:
            if not quiet:
                print("No holes to fill")
            if args.output:
                with open(input_file) as f:
                    Path(args.output).write_text(f.read())
            return 0

        if not quiet:
            print(f"Found {len(all_holes)} holes")

        # Create filler from config or use mock
        if args.config:
            try:
                config = load_config(args.config)
                configs, provider = create_from_config(config)
                if not quiet:
                    print(f"Loaded config from {args.config}")
            except Exception as e:
                print(f"Error loading config: {e}", file=sys.stderr)
                return 1
        else:
            configs = create_default_configs()
            provider = MockProvider()
            if not quiet:
                print("Note: No --config specified. Using mock provider.")
                print("      Create slop.toml from slop.toml.example for real LLM generation.")

        filler = HoleFiller(configs, provider)
        if not quiet:
            print("Filling holes...")

        # Fill holes and track replacements
        replacements = {}  # id(hole) -> filled_expr
        success_count = 0
        fail_count = 0

        # Check if batch mode should be used
        batch_mode = getattr(args, 'batch_interactive', False)

        if batch_mode and hasattr(provider, 'is_interactive'):
            # Group holes by tier for batch processing
            tier_groups = {}  # tier -> [(form, hole, info, context), ...]
            for form, hole in all_holes:
                info = extract_hole(hole)
                tier = classify_tier(info)
                context = _extract_context(form)
                context['type_defs'] = type_defs
                context['fn_specs'] = fn_specs
                context['ffi_specs'] = ffi_specs
                context['const_specs'] = const_specs
                context['requires_fns'] = requires_fns
                context['imported_specs'] = imported_specs
                context['imported_types'] = imported_types
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names + const_names
                context['error_variants'] = error_variants
                if tier not in tier_groups:
                    tier_groups[tier] = []
                tier_groups[tier].append((form, hole, info, context))

            # Process each tier
            for tier in sorted(tier_groups.keys(), key=lambda t: t.value):
                group = tier_groups[tier]
                tier_config = configs.get(tier)

                # Check if this tier uses an interactive provider
                is_interactive = (
                    tier_config and
                    hasattr(provider, 'is_interactive') and
                    provider.is_interactive(tier_config.provider)
                )

                if is_interactive and len(group) > 1:
                    # Batch fill for interactive providers
                    if not quiet:
                        print(f"\n{tier.name} ({len(group)} holes): Batching for interactive provider...")
                    batch_data = [(info, context) for (form, hole, info, context) in group]
                    results = filler.fill_batch(batch_data, tier)

                    for i, (form, hole, info, context) in enumerate(group):
                        result = results[i] if i < len(results) else None
                        if result and result.success and result.expression:
                            replacements[id(hole)] = result.expression
                            logger.debug(f"Replacement added: id={id(hole)}")
                            success_count += 1
                            if not quiet:
                                print(f"  + {info.prompt[:50]}...")
                        else:
                            fail_count += 1
                            error_info = f": {result.error}" if result and result.error else ""
                            if not quiet:
                                print(f"  x {info.prompt[:50]}...{error_info}")
                else:
                    # Individual fill for non-interactive or single holes
                    if not quiet:
                        print(f"\n{tier.name} ({len(group)} holes):")
                    for form, hole, info, context in group:
                        result = filler.fill(info, context)
                        if result.success and result.expression:
                            replacements[id(hole)] = result.expression
                            logger.debug(f"Replacement added: id={id(hole)}")
                            success_count += 1
                            if not quiet:
                                model_info = f" [{result.model_used}]" if result.model_used else ""
                                print(f"  + {info.prompt[:50]}...{model_info}")
                        else:
                            fail_count += 1
                            if not quiet:
                                error_info = f": {result.error}" if result.error else ""
                                print(f"  x {info.prompt[:50]}...{error_info}")
        elif getattr(args, 'parallel', False):
            # Parallel mode with per-tier rate limiting
            if not quiet:
                print("Filling holes in parallel...")

            # Build all contexts upfront
            hole_data = []  # List of (form, hole, info, context) tuples
            for form, hole in all_holes:
                info = extract_hole(hole)
                context = _extract_context(form)
                context['type_defs'] = type_defs
                context['fn_specs'] = fn_specs
                context['ffi_specs'] = ffi_specs
                context['const_specs'] = const_specs
                context['requires_fns'] = requires_fns
                context['imported_specs'] = imported_specs
                context['imported_types'] = imported_types
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names + const_names
                context['error_variants'] = error_variants
                hole_data.append((form, hole, info, context))

            # Prepare for parallel fill
            fill_inputs = [(info, context) for (form, hole, info, context) in hole_data]

            # Progress callback for real-time output
            import threading
            print_lock = threading.Lock()

            def progress_callback(completed: int, total: int, result):
                nonlocal success_count, fail_count
                with print_lock:
                    if result.success:
                        success_count += 1
                        if not quiet:
                            model_info = f" [{result.model_used}]" if result.model_used else ""
                            print(f"  [{completed}/{total}] + ...{model_info}")
                    else:
                        fail_count += 1
                        if not quiet:
                            error_info = f": {result.error}" if result.error else ""
                            print(f"  [{completed}/{total}] x ...{error_info}")

            # Run parallel fill
            max_workers = getattr(args, 'max_workers', None)
            results = filler.fill_parallel(fill_inputs, max_workers=max_workers, progress_callback=progress_callback)

            # Process results
            for i, (form, hole, info, context) in enumerate(hole_data):
                result = results[i]
                if result and result.success and result.expression:
                    replacements[id(hole)] = result.expression
                    logger.debug(f"Replacement added: id={id(hole)}")

        else:
            # Original sequential mode
            for form, hole in all_holes:
                info = extract_hole(hole)
                tier = classify_tier(info)

                # Build context from parent function
                context = _extract_context(form)
                context['type_defs'] = type_defs
                context['fn_specs'] = fn_specs
                context['ffi_specs'] = ffi_specs
                context['const_specs'] = const_specs
                context['requires_fns'] = requires_fns
                context['imported_specs'] = imported_specs
                context['imported_types'] = imported_types
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names + const_names
                context['error_variants'] = error_variants

                result = filler.fill(info, context)
                if result.success and result.expression:
                    replacements[id(hole)] = result.expression
                    logger.debug(f"Replacement added: id={id(hole)}")
                    success_count += 1
                    if not quiet:
                        model_info = f" [{result.model_used}]" if result.model_used else ""
                        print(f"  + {info.prompt[:50]}... ({tier.name}){model_info}")
                else:
                    fail_count += 1
                    if not quiet:
                        error_info = f": {result.error}" if result.error else ""
                        print(f"  x {info.prompt[:50]}... ({tier.name}){error_info}")

        # Replace holes in AST
        logger.debug(f"Replacements: {len(replacements)} entries, ids={list(replacements.keys())}")
        if replacements:
            filled_ast = replace_holes_in_ast(ast, replacements)
        else:
            filled_ast = ast

        # Generate output and format it
        output_lines = []
        for form in filled_ast:
            output_lines.append(pretty_print(form))
            output_lines.append("")

        output_text = '\n'.join(output_lines)
        output_text = format_source(output_text)

        if args.inplace:
            Path(input_file).write_text(output_text)
            if not quiet:
                print(f"\nWrote {input_file}")
        elif args.output:
            Path(args.output).write_text(output_text)
            if not quiet:
                print(f"\nWrote {args.output}")
        else:
            if not quiet:
                print("\n--- Filled source ---")
            print(output_text)

        if not quiet:
            print(f"\n{success_count} filled, {fail_count} failed")
        return 0 if fail_count == 0 else 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _extract_context(form: SList) -> dict:
    """Extract context from a function form for hole filling"""
    context = {}

    if is_form(form, 'fn') or is_form(form, 'impl'):
        if len(form) > 1:
            context['fn_name'] = str(form[1])
        if len(form) > 2:
            context['params'] = str(form[2])

        for item in form.items[3:]:
            if is_form(item, '@intent') and len(item) > 1:
                context['intent'] = item[1].value if hasattr(item[1], 'value') else str(item[1])
            elif is_form(item, '@spec') and len(item) > 1:
                spec = item[1]
                if hasattr(spec, 'items') and len(spec.items) >= 3:
                    context['return_type'] = str(spec.items[-1])

    return context


def cmd_check(args):
    """Validate SLOP file with type checking"""
    try:
        ast = parse_file(args.input)

        errors = []
        warnings = []

        # Check for unfilled holes and missing annotations
        for form in ast:
            holes = find_holes(form)
            for h in holes:
                info = extract_hole(h)
                errors.append(f"Unfilled hole: {info.prompt}")
                if not info.context:
                    warnings.append(f"Hole missing :context - add for better LLM guidance: {info.prompt[:50]}...")

            if is_form(form, 'fn') or is_form(form, 'impl'):
                has_intent = any(is_form(item, '@intent') for item in form.items)
                has_spec = any(is_form(item, '@spec') for item in form.items)

                fn_name = form[1].name if len(form) > 1 else "unknown"

                if not has_intent:
                    warnings.append(f"Function '{fn_name}' missing @intent")
                if not has_spec:
                    warnings.append(f"Function '{fn_name}' missing @spec")

        # Run type checker
        print("  Type checking...")
        diagnostics = check_file(args.input)

        type_errors = [d for d in diagnostics if d.severity == 'error']
        type_warnings = [d for d in diagnostics if d.severity == 'warning']

        # Print all diagnostics
        for w in warnings:
            print(f"warning: {w}")
        for w in type_warnings:
            print(str(w))
        for e in errors:
            print(f"error: {e}")
        for e in type_errors:
            print(str(e))

        total_errors = len(errors) + len(type_errors)
        total_warnings = len(warnings) + len(type_warnings)

        if total_errors > 0:
            print(f"\n{total_errors} error(s), {total_warnings} warning(s)")
            return 1

        if total_warnings > 0:
            print(f"✓ OK with {total_warnings} warning(s)")
        else:
            print("✓ All checks passed")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_check_hole(args):
    """Validate expression against expected type."""
    from slop.hole_filler import check_hole_impl

    # Get expression from arg or stdin
    if args.expr:
        expr_str = args.expr
    else:
        if sys.stdin.isatty():
            print("Error: No expression provided. Use positional arg or pipe to stdin.",
                  file=sys.stderr)
            return 1
        expr_str = sys.stdin.read().strip()

    if not expr_str:
        print("Error: Empty expression", file=sys.stderr)
        return 1

    # Validate --fn requires --context
    if args.fn and not args.context:
        print("Error: --fn requires --context", file=sys.stderr)
        return 1

    # Call the API
    result = check_hole_impl(
        expr_str=expr_str,
        expected_type=args.expected_type,
        context_file=args.context,
        fn_name=args.fn,
        params=args.params,
    )

    if result.valid:
        if args.verbose:
            print(f"OK: {result.inferred_type} matches {result.expected_type}")
        else:
            print("OK")
        return 0
    else:
        for err in result.errors:
            print(f"Error: {err}", file=sys.stderr)
        if args.verbose and result.inferred_type:
            print(f"Inferred: {result.inferred_type}", file=sys.stderr)
            print(f"Expected: {result.expected_type}", file=sys.stderr)
        return 1


def _get_runtime_path() -> Path:
    """Get path to bundled runtime header"""
    try:
        from importlib.resources import files
        return files('slop.runtime')
    except (ImportError, TypeError):
        # Fallback for older Python or development
        return Path(__file__).parent / "runtime"


def _has_imports(ast) -> bool:
    """Check if AST contains import declarations."""
    for form in ast:
        if is_form(form, 'module'):
            for item in form.items[2:]:
                if is_form(item, 'import'):
                    return True
        elif is_form(form, 'import'):
            return True
    return False


def cmd_build(args):
    """Full build pipeline"""
    try:
        # Load project config (auto-detect slop.toml or use explicit -c)
        config_path = getattr(args, 'config', None)
        project_cfg, build_cfg = load_project_config(config_path)

        # Determine input file
        if hasattr(args, 'input') and args.input:
            input_path = Path(args.input)
        elif project_cfg and project_cfg.entry:
            input_path = Path(project_cfg.entry)
            print(f"Using entry from slop.toml: {input_path}")
        else:
            print("Error: No input file specified and no [project].entry in slop.toml", file=sys.stderr)
            return 1

        # Merge config with CLI args (CLI wins)
        if build_cfg:
            output = args.output or build_cfg.output or input_path.stem
            include_paths = (args.include or []) + build_cfg.include
            debug = args.debug or build_cfg.debug
            # Map build_type to library flag format
            if build_cfg.build_type == "static":
                library_mode = getattr(args, 'library', None) or "static"
            elif build_cfg.build_type == "shared":
                library_mode = getattr(args, 'library', None) or "shared"
            else:
                library_mode = getattr(args, 'library', None)
            link_libraries = build_cfg.libraries
            link_paths = build_cfg.library_paths
        else:
            output = args.output or input_path.stem
            include_paths = args.include or []
            debug = args.debug
            library_mode = getattr(args, 'library', None)
            link_libraries = []
            link_paths = []

        print(f"Building {input_path} -> {output}")

        # Create output directory if needed
        output_dir = Path(output).parent
        if output_dir and str(output_dir) != '.':
            output_dir.mkdir(parents=True, exist_ok=True)

        # Parse
        print("  Parsing...")
        ast = parse_file(str(input_path))

        # Check if this is a multi-module build
        search_paths = [Path(p) for p in include_paths]
        is_multi_module = _has_imports(ast)

        if is_multi_module:
            # Multi-module build
            print("  Resolving modules...")
            resolver = ModuleResolver(search_paths)
            try:
                graph = resolver.build_dependency_graph(input_path)
                order = resolver.topological_sort(graph)
                print(f"    Build order: {', '.join(order)}")
            except ResolverError as e:
                print(f"  Module resolution failed: {e}")
                return 1

            # Validate imports
            errors = resolver.validate_imports(graph)
            if errors:
                for e in errors:
                    print(f"    {e}")
                print(f"  Import validation failed: {len(errors)} error(s)")
                return 1

            # Check for holes across all modules
            total_holes = 0
            for mod_name in order:
                info = graph.modules[mod_name]
                for form in info.ast:
                    total_holes += len(find_holes(form))
            if total_holes > 0:
                print(f"  Warning: {total_holes} unfilled holes")

            # Type check all modules
            print("  Type checking...")
            all_diagnostics = check_modules(graph.modules, order)
            total_errors = 0
            total_warnings = 0
            for mod_name, diagnostics in all_diagnostics.items():
                type_errors = [d for d in diagnostics if d.severity == 'error']
                type_warnings = [d for d in diagnostics if d.severity == 'warning']
                for w in type_warnings:
                    print(f"    [{mod_name}] {w}")
                for e in type_errors:
                    print(f"    [{mod_name}] {e}")
                total_errors += len(type_errors)
                total_warnings += len(type_warnings)

            if total_errors > 0:
                print(f"  Type check failed: {total_errors} error(s)")
                return 1

            if total_warnings > 0:
                print(f"  Type check passed with {total_warnings} warning(s)")
            else:
                print("  Type check passed")

            # Transpile all modules to separate files
            print("  Transpiling to C...")
            from slop.transpiler import transpile_multi_split
            import tempfile
            import subprocess

            results = transpile_multi_split(graph.modules, order)

            # Write to temp directory and compile
            runtime_path = _get_runtime_path()

            with tempfile.TemporaryDirectory() as tmpdir:
                c_files = []
                for mod_name, (header, impl) in results.items():
                    header_path = os.path.join(tmpdir, f"{mod_name}.h")
                    impl_path = os.path.join(tmpdir, f"{mod_name}.c")
                    with open(header_path, 'w') as f:
                        f.write(header)
                    with open(impl_path, 'w') as f:
                        f.write(impl)
                    c_files.append(impl_path)

                print("  Compiling...")

                # Build link flags from config
                link_flags = []
                for lpath in link_paths:
                    link_flags.extend(["-L", lpath])
                for lib in link_libraries:
                    link_flags.extend(["-l", lib])

                if library_mode == 'static':
                    # Compile to object files, then create static library
                    obj_files = []
                    for c_file in c_files:
                        obj_file = c_file.replace('.c', '.o')
                        compile_cmd = ["cc", "-c", "-O2", "-I", str(runtime_path), "-I", tmpdir, "-o", obj_file, c_file]
                        if debug:
                            compile_cmd.insert(1, "-g")
                            compile_cmd.insert(2, "-DSLOP_DEBUG")
                        result = subprocess.run(compile_cmd, capture_output=True, text=True)
                        if result.returncode != 0:
                            print(f"Compilation failed:\n{result.stderr}")
                            return 1
                        obj_files.append(obj_file)

                    lib_file = f"{output}.a"
                    ar_cmd = ["ar", "rcs", lib_file] + obj_files
                    result = subprocess.run(ar_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"Archive failed:\n{result.stderr}")
                        return 1
                    print(f"✓ Built {lib_file}")

                elif library_mode == 'shared':
                    ext = ".dylib" if sys.platform == "darwin" else ".so"
                    lib_file = f"{output}{ext}"
                    compile_cmd = ["cc", "-shared", "-fPIC", "-O2", "-I", str(runtime_path), "-I", tmpdir,
                                  "-o", lib_file] + c_files + link_flags
                    if debug:
                        compile_cmd.insert(1, "-g")
                        compile_cmd.insert(2, "-DSLOP_DEBUG")
                    result = subprocess.run(compile_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"Compilation failed:\n{result.stderr}")
                        return 1
                    print(f"✓ Built {lib_file}")

                else:
                    # Default: build executable
                    compile_cmd = ["cc", "-O2", "-I", str(runtime_path), "-I", tmpdir, "-o", output] + c_files + link_flags
                    if debug:
                        compile_cmd.insert(1, "-g")
                        compile_cmd.insert(2, "-DSLOP_DEBUG")
                    result = subprocess.run(compile_cmd, capture_output=True, text=True)
                    if result.returncode != 0:
                        print(f"Compilation failed:\n{result.stderr}")
                        return 1
                    print(f"✓ Built {output}")

            return 0

        else:
            # Single-file build (backward compatible)
            # Check for holes
            total_holes = sum(len(find_holes(form)) for form in ast)
            if total_holes > 0:
                print(f"  Warning: {total_holes} unfilled holes")

            # Type check using the already-parsed AST
            # This annotates AST nodes with resolved_type for the transpiler to use
            print("  Type checking...")
            from slop.type_checker import check_source_ast
            diagnostics = check_source_ast(ast, str(input_path))
            type_errors = [d for d in diagnostics if d.severity == 'error']
            type_warnings = [d for d in diagnostics if d.severity == 'warning']

            for w in type_warnings:
                print(f"    {w}")

            if type_errors:
                for e in type_errors:
                    print(f"    {e}")
                print(f"  Type check failed: {len(type_errors)} error(s)")
                return 1

            if type_warnings:
                print(f"  Type check passed with {len(type_warnings)} warning(s)")
            else:
                print("  Type check passed")

            # Transpile using the same AST (now has resolved_type annotations)
            print("  Transpiling to C...")
            from slop.transpiler import Transpiler
            c_code = Transpiler().transpile(ast)

        c_file = f"{output}.c"
        with open(c_file, 'w') as f:
            f.write(c_code)

        # Compile
        print("  Compiling...")
        import subprocess

        runtime_path = _get_runtime_path()

        # Build link flags from config
        link_flags = []
        for lpath in link_paths:
            link_flags.extend(["-L", lpath])
        for lib in link_libraries:
            link_flags.extend(["-l", lib])

        if library_mode == 'static':
            # Compile to object file, then create static library
            obj_file = f"{output}.o"
            lib_file = f"{output}.a"

            compile_cmd = ["cc", "-c", "-O2", "-I", str(runtime_path), "-o", obj_file, c_file]
            if debug:
                compile_cmd.insert(1, "-g")
                compile_cmd.insert(2, "-DSLOP_DEBUG")

            result = subprocess.run(compile_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Compilation failed:\n{result.stderr}")
                return 1

            ar_cmd = ["ar", "rcs", lib_file, obj_file]
            result = subprocess.run(ar_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Archive failed:\n{result.stderr}")
                return 1

            # Clean up object file
            Path(obj_file).unlink(missing_ok=True)
            print(f"✓ Built {lib_file}")

        elif library_mode == 'shared':
            # Compile to shared library
            ext = ".dylib" if sys.platform == "darwin" else ".so"
            lib_file = f"{output}{ext}"

            compile_cmd = ["cc", "-shared", "-fPIC", "-O2", "-I", str(runtime_path),
                          "-o", lib_file, c_file] + link_flags
            if debug:
                compile_cmd.insert(1, "-g")
                compile_cmd.insert(2, "-DSLOP_DEBUG")

            result = subprocess.run(compile_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Compilation failed:\n{result.stderr}")
                return 1

            print(f"✓ Built {lib_file}")

        else:
            # Default: build executable
            compile_cmd = [
                "cc",
                "-O2",
                "-I", str(runtime_path),
                "-o", output,
                c_file,
            ] + link_flags

            if debug:
                compile_cmd.insert(1, "-g")
                compile_cmd.insert(2, "-DSLOP_DEBUG")

            result = subprocess.run(compile_cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Compilation failed:\n{result.stderr}")
                return 1

            print(f"✓ Built {output}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_derive(args):
    """Derive SLOP types from external schemas"""
    import json
    from slop.schema_converter import (
        convert_json_schema, convert_sql, OpenApiConverter, detect_schema_format
    )

    input_path = Path(args.input)

    # Auto-detect format
    if args.format:
        fmt = args.format
    else:
        fmt = _detect_format(input_path)

    # Get storage mode (only applies to OpenAPI)
    storage_mode = getattr(args, 'storage', 'stub')

    try:
        if fmt == 'sql':
            output = convert_sql(str(input_path))
        elif fmt == 'openapi':
            spec = _load_spec(str(input_path))
            output = OpenApiConverter(storage_mode=storage_mode).convert(spec)
        else:  # jsonschema
            output = convert_json_schema(str(input_path))

        if args.output:
            Path(args.output).write_text(output)
            print(f"Wrote {args.output}")
        else:
            print(output)

        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _detect_format(path: Path) -> str:
    """Auto-detect schema format from file"""
    suffix = path.suffix.lower()

    if suffix == '.sql':
        return 'sql'
    elif suffix in ('.yaml', '.yml', '.json'):
        # Check content for OpenAPI vs JSON Schema
        try:
            data = _load_spec(str(path))
            if 'openapi' in data or 'swagger' in data or 'paths' in data:
                return 'openapi'
            else:
                return 'jsonschema'
        except Exception:
            return 'jsonschema'
    else:
        return 'jsonschema'


def _load_spec(path: str) -> dict:
    """Load spec from JSON or YAML file"""
    import json

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


def cmd_format(args):
    """Format SLOP source code."""
    from slop.formatter import format_source

    exit_code = 0
    for filepath in args.input:
        try:
            source = Path(filepath).read_text()
            formatted = format_source(source)

            if args.check:
                # Check mode - just report if file needs formatting
                if source != formatted:
                    print(f"{filepath}: needs formatting")
                    exit_code = 1
            elif args.stdout:
                # Print to stdout
                print(formatted, end='')
            else:
                # Default - format in place
                if source != formatted:
                    Path(filepath).write_text(formatted)
                    print(f"Formatted {filepath}")
                else:
                    print(f"{filepath} unchanged")

        except Exception as e:
            print(f"Error formatting {filepath}: {e}", file=sys.stderr)
            exit_code = 1

    return exit_code


def cmd_ref(args):
    """Display language reference for AI assistants"""
    from slop.reference import get_reference, list_topics, TOPICS

    if args.list:
        for topic in list_topics():
            print(topic)
        return 0

    topic = args.topic or 'all'
    if topic != 'all' and topic not in TOPICS:
        print(f"Unknown topic: {topic}", file=sys.stderr)
        print(f"Available: {', '.join(list_topics())}", file=sys.stderr)
        return 1

    print(get_reference(topic))
    return 0


def cmd_verify(args):
    """Verify contracts and range safety with Z3"""
    try:
        from slop.verifier import verify_file, Z3_AVAILABLE
    except ImportError:
        print("Error: verifier module not found", file=sys.stderr)
        return 1

    if not Z3_AVAILABLE:
        print("Error: Z3 solver not available.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Install with one of:", file=sys.stderr)
        print("  uv sync --extra verify     # if using uv (recommended)", file=sys.stderr)
        print("  uv pip install z3-solver   # alternative for uv", file=sys.stderr)
        print("  pip install z3-solver      # if using pip", file=sys.stderr)
        return 1

    # Determine failure mode
    mode = args.mode if args.mode else "error"

    # Run verification
    results = verify_file(args.input, mode=mode, timeout_ms=args.timeout)

    # Categorize results
    verified = [r for r in results if r.status == 'verified']
    failed = [r for r in results if r.status == 'failed']
    unknown = [r for r in results if r.status == 'unknown']
    warnings = [r for r in results if r.status == 'warning']
    errors = [r for r in results if r.status == 'error']
    skipped = [r for r in results if r.status == 'skipped']

    # Print results
    for r in verified:
        print(f"  verified: {r.name}")

    for r in skipped:
        if args.verbose:
            print(f"  skipped: {r.name} - {r.message}")

    for r in warnings:
        print(f"  warning: {r.name} - {r.message}")

    for r in unknown:
        print(f"  unknown: {r.name} - {r.message}")

    for r in failed:
        print(f"  failed: {r.name} - {r.message}")
        if args.verbose and r.counterexample:
            ce_str = ", ".join(f"{k}={v}" for k, v in r.counterexample.items())
            print(f"    counterexample: {ce_str}")

    for r in errors:
        print(f"  error: {r.name} - {r.message}")

    # Summary
    total = len(verified) + len(failed) + len(unknown) + len(warnings)
    if total == 0:
        print("No contracts to verify")
        return 0

    if failed or errors:
        summary = f"\n{len(verified)} verified, {len(failed)} failed"
        if warnings:
            summary += f", {len(warnings)} warning(s)"
        if unknown:
            summary += f", {len(unknown)} unknown"
        print(summary)
        if mode == "error":
            return 1
        return 0

    if unknown or warnings:
        summary = f"\n{len(verified)} verified"
        if warnings:
            summary += f", {len(warnings)} warning(s)"
        if unknown:
            summary += f", {len(unknown)} unknown (timeout)"
        print(summary)
        return 0

    print(f"\nAll {len(verified)} contract(s) verified")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='SLOP - Symbolic LLM-Optimized Programming',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-v', '--verbose', action='store_true')

    subparsers = parser.add_subparsers(dest='command')

    # parse
    p = subparsers.add_parser('parse', help='Parse SLOP file')
    p.add_argument('input')
    p.add_argument('--holes', action='store_true', help='Show only holes')

    # transpile
    p = subparsers.add_parser('transpile', help='Convert to C')
    p.add_argument('input')
    p.add_argument('-o', '--output')
    p.add_argument('-I', '--include', action='append', default=[],
                   help='Add search path for module imports')

    # fill
    p = subparsers.add_parser('fill', help='Fill holes with LLM')
    p.add_argument('input', nargs='?', default=None,
                   help='Input file (optional if slop.toml has [project].entry)')
    p.add_argument('-o', '--output')
    p.add_argument('-i', '--inplace', action='store_true',
        help='Modify input file in place')
    p.add_argument('-c', '--config', help='Path to TOML config file')
    p.add_argument('-v', '--verbose', action='count', default=0,
        help='Increase verbosity (-v for info, -vv for debug with imported specs)')
    p.add_argument('-q', '--quiet', action='store_true',
        help='Output only the filled source, no status messages')
    p.add_argument('--batch-interactive', action='store_true',
        help='Batch interactive provider holes into single sessions')
    p.add_argument('-p', '--parallel', action='store_true',
        help='Fill holes in parallel (respects per-tier rate limits)')
    p.add_argument('--max-workers', type=int, default=None,
        help='Max parallel workers (default: auto based on tier limits)')

    # check
    p = subparsers.add_parser('check', help='Validate')
    p.add_argument('input')

    # check-hole
    p = subparsers.add_parser('check-hole', help='Validate expression against expected type')
    p.add_argument('expr', nargs='?', default=None,
                   help='Expression to check (reads from stdin if not provided)')
    p.add_argument('-t', '--type', required=True, dest='expected_type',
                   help='Expected type (e.g., "Int", "(Ptr User)", "(Result Int Error)")')
    p.add_argument('-c', '--context',
                   help='Context .slop file for types and functions')
    p.add_argument('-f', '--fn',
                   help='Function name to extract params from (requires --context)')
    p.add_argument('-p', '--params',
                   help='Parameter list like "((x Int) (y String))" (overrides --fn)')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Show inferred and expected types')

    # build
    p = subparsers.add_parser('build', help='Full pipeline')
    p.add_argument('input', nargs='?', default=None,
                   help='Input file (optional if slop.toml has [project].entry)')
    p.add_argument('-o', '--output')
    p.add_argument('-c', '--config', help='Path to TOML config file')
    p.add_argument('-I', '--include', action='append',
                   help='Add search path for imports (can be repeated)')
    p.add_argument('--debug', action='store_true')
    p.add_argument('--library', choices=['static', 'shared'],
                   help='Build as library instead of executable')

    # derive
    p = subparsers.add_parser('derive', help='Generate SLOP from schemas')
    p.add_argument('input', help='Schema file (JSON, YAML, or SQL)')
    p.add_argument('-o', '--output', help='Output file')
    p.add_argument('-f', '--format',
        choices=['jsonschema', 'openapi', 'sql'],
        help='Force schema format (auto-detected by default)')
    p.add_argument('-s', '--storage',
        choices=['stub', 'map', 'none'],
        default='stub',
        help='Storage mode for OpenAPI: stub (default, with @requires), map (in-memory), none (holes only)')

    # format
    p = subparsers.add_parser('format', help='Format SLOP source code')
    p.add_argument('input', nargs='+', help='Input file(s)')
    p.add_argument('--stdout', action='store_true',
        help='Print to stdout instead of modifying files')
    p.add_argument('--check', action='store_true',
        help='Check if files are formatted (exit 1 if not)')

    # verify
    p = subparsers.add_parser('verify', help='Verify contracts with Z3')
    p.add_argument('input', help='Input SLOP file')
    p.add_argument('--mode', choices=['error', 'warn'],
        help='Failure mode: error (block, default) or warn')
    p.add_argument('--timeout', type=int, default=5000,
        help='Z3 solver timeout in milliseconds (default: 5000)')
    p.add_argument('-v', '--verbose', action='store_true',
        help='Show counterexamples and skipped contracts')

    # ref
    p = subparsers.add_parser('ref', help='Language reference for AI assistants')
    p.add_argument('topic', nargs='?', default='all',
        help='Topic: types, functions, contracts, holes, memory, ffi, stdlib, expressions, patterns')
    p.add_argument('--list', action='store_true',
        help='List available topics')

    # doc
    p = subparsers.add_parser('doc', help='Generate documentation from SLOP source')
    p.add_argument('input', help='Input SLOP file')
    p.add_argument('-o', '--output', help='Output file (default: stdout)')
    p.add_argument('-f', '--format', choices=['markdown', 'json'], default='markdown',
        help='Output format (default: markdown)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        'parse': cmd_parse,
        'transpile': cmd_transpile,
        'fill': cmd_fill,
        'check': cmd_check,
        'check-hole': cmd_check_hole,
        'build': cmd_build,
        'derive': cmd_derive,
        'format': cmd_format,
        'verify': cmd_verify,
        'ref': cmd_ref,
        'doc': cmd_doc,
    }

    return commands[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
