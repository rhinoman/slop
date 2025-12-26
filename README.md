# SLOP - Symbolic LLM-Optimized Programming

A programming language designed for minimal human involvement in coding.

```
Humans specify WHAT and WHY → Machines handle HOW
```

## Philosophy

SLOP inverts the traditional programming model:

| Traditional | SLOP |
|-------------|------|
| Human writes code | Human writes specification |
| Compiler checks syntax | Machine generates implementation |
| Tests verify behavior | Contracts define correctness |
| Libraries provide code | Schemas generate types |

## Design Choices

- **S-expression syntax**: Zero parsing ambiguity, trivial for LLMs (I like Lisp)
- **Range types**: `(Int 0 .. 100)` catches bounds errors at compile time (I also like Ada)
- **Mandatory contracts**: `@intent`, `@spec`, `@pre`, `@post` define correctness 
- **Typed holes**: Explicit markers for LLM generation with complexity tiers
- **Transpiles to C**: Maximum performance, universal FFI, minimal runtime 

## Quick Example

```lisp
(module rate-limiter
  (export (acquire 1))
  
  (type Tokens (Int 0 .. 10000))
  
  (fn acquire ((limiter (Ptr Limiter)))
    (@intent "Try to acquire one token")
    (@spec (((Ptr Limiter)) -> AcquireResult))
    (@pre (!= limiter nil))
    
    (if (> (. limiter tokens) 0)
      (do
        (set! limiter tokens (- (. limiter tokens) 1))
        'acquired)
      'rate-limited)))
```

Transpiles to:

```c
AcquireResult acquire(Limiter* limiter) {
    SLOP_PRE(limiter != NULL, "limiter != nil");
    
    if (limiter->tokens.value > 0) {
        limiter->tokens = Tokens_new(limiter->tokens.value - 1);
        return AcquireResult_acquired;
    } else {
        return AcquireResult_rate_limited;
    }
}
```

## Project Structure

```
slop/
├── spec/                    Language specifications
│   ├── LANGUAGE.md          Grammar, types, semantics
│   └── HYBRID_PIPELINE.md   Generation architecture
├── src/slop/
│   ├── runtime/
│   │   └── slop_runtime.h   Minimal C runtime (~400 lines)
│   ├── parser.py            S-expression parser
│   ├── transpiler.py        SLOP → C transpiler (with type flow analysis)
│   ├── type_checker.py      Type inference with range propagation
│   ├── hole_filler.py       LLM integration with tiered routing
│   ├── providers.py         LLM providers (Ollama, OpenAI, etc.)
│   ├── schema_converter.py  JSON Schema → SLOP types
│   └── cli.py               Command-line interface
├── examples/
│   ├── rate-limiter.slop    Complete example
│   └── hello.slop           Minimal example
├── skill/                   Claude skill for SLOP generation
│   └── references/          Built-ins, patterns, types docs
└── tests/                   Test suite
```

## Usage

```bash
# Install
pip install -e .

# Parse and inspect
slop parse examples/rate-limiter.slop

# Show holes
slop parse examples/rate-limiter.slop --holes

# Transpile to C
slop transpile examples/rate-limiter.slop -o rate_limiter.c

# Type check
slop check examples/rate-limiter.slop

# Full build (requires cc)
slop build examples/rate-limiter.slop -o rate_limiter
```

## Hybrid Generation Pipeline

```
┌─────────────────┐
│  JSON Schema    │  ← External specs
│  SQL DDL        │
│  OpenAPI        │
└────────┬────────┘
         │ Deterministic
         ▼
┌─────────────────┐
│  SLOP Types     │  ← Generated types + signatures
│  + Signatures   │
└────────┬────────┘
         │ LLM (tiered)
         ▼
┌─────────────────┐
│  SLOP + Impl    │  ← Holes filled by appropriate model
└────────┬────────┘
         │ Deterministic
         ▼
┌─────────────────┐
│  Verification   │  ← Type check, contract check
└────────┬────────┘
         │ Deterministic
         ▼
┌─────────────────┐
│  C Source       │  ← Transpiled output
└────────┬────────┘
         │ cc -O3
         ▼
┌─────────────────┐
│  Native Binary  │  ← Optimized executable
└─────────────────┘
```

## Model Tiering

Holes are routed to appropriately-sized models:

| Tier | Model Size | Use Case |
|------|------------|----------|
| tier-1 | 1-3B | Boolean expressions, simple arithmetic |
| tier-2 | 7-8B | Single conditional, Result construction |
| tier-3 | 13-34B | Loops, multiple conditions |
| tier-4 | 70B+ | Algorithms, complex logic |

```lisp
(hole Bool "Check if user is adult"
  :complexity tier-1)  ; Small model handles this

(hole (Result User Error) "Validate and update user"
  :complexity tier-3   ; Needs larger model
  :must-use (input db-update validate-email))
```

## Why C?

C's problems are **human** problems:
- Manual memory management → Machines don't forget
- No namespaces → Machines use prefixes consistently
- Buffer overflows → Transpiler generates safe patterns

C's benefits remain:
- 10-100x faster than interpreted languages
- Universal FFI to any library
- 50 years of optimizer engineering
- Runs everywhere

## Other Targets

Aside from C, an obvious choice for another target would be typescript.
WASM would also be easy to do since we're already transpiling to C.

## Memory Model

Arena allocation handles 90% of cases:

```lisp
(fn handle-request ((arena Arena) (req Request))
  (@alloc arena)
  (let ((user (parse-user arena req))
        (resp (process arena user)))
    (send resp)))
; Arena freed by caller
```

## Status

**Implemented:**
- ✓ Language specification
- ✓ S-expression parser with pretty-printing
- ✓ SLOP → C transpiler with type flow analysis
- ✓ Type checker with range inference and path-sensitive analysis
- ✓ Hole extraction, classification, and tiered model routing
- ✓ LLM providers (Ollama, OpenAI-compatible, Interactive, Multi-provider)
- ✓ Hole filler with quality scoring and pattern library
- ✓ CLI tooling (`slop` command)
- ✓ Runtime header with arena allocation
- ✓ Test suite (74 tests)

**Not Yet Implemented:**
- Contract verification (SMT solver integration)
- Property-based testing generation

## License

Apache 2.0
