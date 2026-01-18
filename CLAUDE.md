# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is SLOP?

SLOP (Symbolic LLM-Optimized Programming) is a language designed for hybrid human-machine code generation. Humans specify intent via contracts and types; machines generate implementation and transpile to C.

Key features:
- S-expression syntax (Lisp-like)
- Mandatory contracts (`@intent`, `@spec`, `@pre`, `@post`)
- Range types: `(Int 0 .. 100)` catches bounds errors at compile time
- Typed holes for LLM generation with complexity tiers
- Transpiles to C for performance

## Commands

```bash
# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/test_transpiler.py

# Run a single test
uv run pytest tests/test_transpiler.py::TestTypeDefinitions::test_range_type -v

# Parse a SLOP file
uv run slop parse examples/rate-limiter.slop

# Type check
uv run slop check examples/rate-limiter.slop

# Transpile to C
uv run slop transpile examples/rate-limiter.slop -o output.c

# Build to native binary (requires cc)
uv run slop build examples/rate-limiter.slop -o binary

# Build the native toolchain using the python toolchain
./build_native_py.sh
```

## Architecture

### Pipeline Flow
```
SLOP source → parser.py → AST → type_checker.py → transpiler.py → C code
                                      ↓
                              hole_filler.py (LLM fills holes)
```

### Core Modules (src/slop/)

- **parser.py**: S-expression parser producing AST (`SList`, `Symbol`, `String`, `Number`). Key functions: `parse()`, `is_form()`, `find_holes()`, `pretty_print()`

- **type_checker.py**: Type inference with range propagation and path-sensitive analysis. Tracks pointer types, validates contracts, checks exhaustiveness. Raises `TypeError` with line numbers.

- **transpiler.py**: AST to C code generation. Handles type mapping, field access (auto `.` vs `->`), FFI, modules. Raises `UnfilledHoleError` if holes remain.

- **hole_filler.py**: Routes holes to LLM providers based on complexity tiers (tier-1 through tier-4). Validates generated code against type constraints.

- **providers.py**: LLM provider implementations (Ollama, OpenAI-compatible, Interactive, Multi-provider routing)

- **cli.py**: Command-line interface (`slop` command)

- **resolver.py**: Module import/export resolution

### Language Spec

The authoritative language specification is in `spec/LANGUAGE.md`. When modifying the language:
1. Update `spec/LANGUAGE.md`
2. Update `.claude/skills/slop/SKILL.md` (Claude's quick reference)
3. Update `src/slop/reference.py` (CLI reference for `slop ref` command)
4. Update parser/transpiler/type_checker as needed
5. Update `tree-sitter-slop/` grammar and highlights if syntax changes

### Tree-sitter Grammar

Located in `tree-sitter-slop/`:
- `grammar.js`: Grammar definition
- `queries/highlights.scm`: Syntax highlighting
- `slop-ts-mode.el`: Emacs major mode

## SLOP Syntax Quick Reference

```lisp
;; Module with exports
(module name (export fn-name) forms...)

;; Types
(type Name (record (field Type)...))
(type Name (enum val1 val2))
(type Name (Int min .. max))

;; Constants (integers → #define, others → static const)
(const NAME Type value)

;; Functions with contracts
(fn name ((param Type)...)
  (@intent "description")
  (@spec ((ParamTypes) -> ReturnType))
  (@pre condition)
  (@post condition)
  body)

;; Holes for LLM generation
(hole Type "prompt" :complexity tier-2 :required (var1 var2))

;; FFI
(ffi "header.h" (func-name ((param Type)) ReturnType))
(ffi-struct "header.h" name (field Type)...)
```

## Key Patterns

**Pointer tracking**: Transpiler auto-detects pointers from `(Ptr T)` params and `arena-alloc` results. Uses `->` for pointer field access, `.` for values.

**Range types**: `(Int 0 .. 255)` maps to smallest C type (uint8_t) with runtime bounds checks via `SLOP_PRE`.

**Arena allocation**: Primary memory model. `(with-arena size body)` creates scoped arena, freed automatically.

**Result types**: `(Result T E)` is a tagged union. Use `(ok val)` / `(error e)` constructors, `match` to destructure.

## Self Hosting
The goal is a self-hosting slop parser, type checker, and transpiler.  When working on the slop native tooling
- It is always better to generate a transpiler error than to emit ambiguous C code and / or some hard-coded default when running into some unimplemented SLOP feature.
- Errors should be caught as early as possible.  First by the parser, then the type checker, then the transpiler.  It is a FAILURE if an error is caught by the C compiler.
- The goal is not simply to get something that builds.  We do not workaround code generation issues with hardcoding, default types, rewriting using a different SLOP construct, etc.  
- The SLOP native tools are the priority for new features, implement there first, the python tools second.


## General guidance
- Paren balance is critical for a language based on S-expressions.  Verify balance BEFORE making multiple writes to a file to avoid large compounding imbalances that require extensive troubleshooting.
