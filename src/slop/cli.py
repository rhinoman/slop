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
    MockProvider, create_default_configs, load_config, create_from_config, Tier
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
                    if info.must_use:
                        print(f"  Must use: {', '.join(info.must_use)}")
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
    """Transpile SLOP to C"""
    try:
        with open(args.input) as f:
            source = f.read()

        c_code = transpile(source)

        if args.output:
            with open(args.output, 'w') as f:
                f.write(c_code)
            print(f"Wrote {args.output}")
        else:
            print(c_code)

        return 0
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
    Returns both function names (from (fn-name arity)) and type names (bare symbols).
    """
    from slop.parser import Symbol
    imported_names = []

    for form in ast:
        if is_form(form, 'import'):
            # (import module-name (fn1 arity) (fn2 arity) Type1 Type2 ...)
            for item in form.items[2:]:  # Skip 'import' and module name
                if isinstance(item, SList) and len(item) >= 1:
                    # Function import: (fn-name arity)
                    if isinstance(item[0], Symbol):
                        imported_names.append(item[0].name)
                elif isinstance(item, Symbol):
                    # Type import: bare symbol
                    imported_names.append(item.name)
        elif is_form(form, 'module'):
            for subform in form.items:
                if is_form(subform, 'import'):
                    for item in subform.items[2:]:
                        if isinstance(item, SList) and len(item) >= 1:
                            if isinstance(item[0], Symbol):
                                imported_names.append(item[0].name)
                        elif isinstance(item, Symbol):
                            imported_names.append(item.name)

    return imported_names


def cmd_fill(args):
    """Fill holes with LLM"""
    import logging
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(name)s %(levelname)s: %(message)s'
        )
    elif not args.quiet:
        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s'
        )

    logger = logging.getLogger(__name__)
    quiet = args.quiet

    try:
        ast = parse_file(args.input)

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
                with open(args.input) as f:
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
                context['requires_fns'] = requires_fns
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names
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
                context['requires_fns'] = requires_fns
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names
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
                context['requires_fns'] = requires_fns
                context['defined_functions'] = [s['name'] for s in fn_specs] + type_names + ffi_functions + imported_names
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
            Path(args.input).write_text(output_text)
            if not quiet:
                print(f"\nWrote {args.input}")
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
        input_path = Path(args.input)
        output = args.output or input_path.stem

        print(f"Building {input_path} -> {output}")

        # Parse
        print("  Parsing...")
        ast = parse_file(args.input)

        # Check if this is a multi-module build
        search_paths = [Path(p) for p in (args.include or [])]
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

            # Transpile all modules
            print("  Transpiling to C...")
            c_code = transpile_multi(graph.modules, order)

        else:
            # Single-file build (backward compatible)
            # Check for holes
            total_holes = sum(len(find_holes(form)) for form in ast)
            if total_holes > 0:
                print(f"  Warning: {total_holes} unfilled holes")

            # Type check
            print("  Type checking...")
            diagnostics = check_file(args.input)
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

            # Transpile
            print("  Transpiling to C...")
            with open(args.input) as f:
                source = f.read()
            c_code = transpile(source)

        c_file = f"{output}.c"
        with open(c_file, 'w') as f:
            f.write(c_code)

        # Compile
        print("  Compiling...")
        import subprocess

        runtime_path = _get_runtime_path()

        library_mode = getattr(args, 'library', None)

        if library_mode == 'static':
            # Compile to object file, then create static library
            obj_file = f"{output}.o"
            lib_file = f"{output}.a"

            compile_cmd = ["cc", "-c", "-O2", "-I", str(runtime_path), "-o", obj_file, c_file]
            if args.debug:
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
                          "-o", lib_file, c_file]
            if args.debug:
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
            ]

            if args.debug:
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

    # fill
    p = subparsers.add_parser('fill', help='Fill holes with LLM')
    p.add_argument('input')
    p.add_argument('-o', '--output')
    p.add_argument('-i', '--inplace', action='store_true',
        help='Modify input file in place')
    p.add_argument('-c', '--config', help='Path to TOML config file')
    p.add_argument('-v', '--verbose', action='store_true')
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

    # build
    p = subparsers.add_parser('build', help='Full pipeline')
    p.add_argument('input')
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

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    commands = {
        'parse': cmd_parse,
        'transpile': cmd_transpile,
        'fill': cmd_fill,
        'check': cmd_check,
        'build': cmd_build,
        'derive': cmd_derive,
        'format': cmd_format,
    }

    return commands[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
