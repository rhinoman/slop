#!/usr/bin/env python3
"""
SLOP CLI - Command-line interface for SLOP toolchain

Commands:
  parse      Parse and validate SLOP files
  transpile  Convert SLOP to C
  fill       Fill holes with LLM generation
  check      Validate types and contracts
  build      Full pipeline: fill → transpile → compile
"""

import argparse
import sys
import os
from pathlib import Path

from slop.parser import parse, parse_file, pretty_print, find_holes, is_form, SList
from slop.transpiler import Transpiler, transpile
from slop.hole_filler import HoleFiller, extract_hole, classify_tier, replace_holes_in_ast
from slop.providers import (
    MockProvider, create_default_configs, load_config, create_from_config
)
from slop.type_checker import TypeChecker, check_file


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


def cmd_fill(args):
    """Fill holes with LLM"""
    try:
        ast = parse_file(args.input)

        # Collect all holes with their parent forms for context
        all_holes = []
        for form in ast:
            holes = find_holes(form)
            for h in holes:
                all_holes.append((form, h))

        if not all_holes:
            print("No holes to fill")
            if args.output:
                with open(args.input) as f:
                    Path(args.output).write_text(f.read())
            return 0

        print(f"Found {len(all_holes)} holes")

        # Create filler from config or use mock
        if args.config:
            try:
                config = load_config(args.config)
                configs, provider = create_from_config(config)
                print(f"Loaded config from {args.config}")
            except Exception as e:
                print(f"Error loading config: {e}", file=sys.stderr)
                return 1
        else:
            configs = create_default_configs()
            provider = MockProvider()
            print("Note: No --config specified. Using mock provider.")
            print("      Create slop.toml from slop.toml.example for real LLM generation.")

        filler = HoleFiller(configs, provider)
        print("Filling holes...")

        # Fill holes and track replacements
        replacements = {}  # id(hole) -> filled_expr
        success_count = 0
        fail_count = 0

        for form, hole in all_holes:
            info = extract_hole(hole)
            tier = classify_tier(info)

            # Build context from parent function
            context = _extract_context(form)

            result = filler.fill(info, context)
            if result.success and result.expression:
                replacements[id(hole)] = result.expression
                success_count += 1
                status = "filled"
                model_info = f" [{result.model_used}]" if result.model_used else ""
                print(f"  + {info.prompt[:50]}... ({tier.name}){model_info}")
            else:
                fail_count += 1
                error_info = f": {result.error}" if result.error else ""
                print(f"  x {info.prompt[:50]}... ({tier.name}){error_info}")

        # Replace holes in AST
        if replacements:
            filled_ast = replace_holes_in_ast(ast, replacements)
        else:
            filled_ast = ast

        # Generate output
        output_lines = []
        for form in filled_ast:
            output_lines.append(pretty_print(form))
            output_lines.append("")

        output_text = '\n'.join(output_lines)

        if args.output:
            Path(args.output).write_text(output_text)
            print(f"\nWrote {args.output}")
        else:
            print("\n--- Filled source ---")
            print(output_text)

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


def cmd_build(args):
    """Full build pipeline"""
    try:
        input_path = Path(args.input)
        output = args.output or input_path.stem

        print(f"Building {input_path} -> {output}")

        # Parse
        print("  Parsing...")
        ast = parse_file(args.input)

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
    p.add_argument('-c', '--config', help='Path to TOML config file')
    p.add_argument('-v', '--verbose', action='store_true')

    # check
    p = subparsers.add_parser('check', help='Validate')
    p.add_argument('input')

    # build
    p = subparsers.add_parser('build', help='Full pipeline')
    p.add_argument('input')
    p.add_argument('-o', '--output')
    p.add_argument('-c', '--config', help='Path to TOML config file')
    p.add_argument('--debug', action='store_true')

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
    }

    return commands[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
