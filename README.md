```
 ███████╗██╗      ██████╗ ██████╗
 ██╔════╝██║     ██╔═══██╗██╔══██╗
 ███████╗██║     ██║   ██║██████╔╝
 ╚════██║██║     ██║   ██║██╔═══╝
 ███████║███████╗╚██████╔╝██║
 ╚══════╝╚══════╝ ╚═════╝ ╚═╝
```

# Symbolic LLM-Optimized Programming

A programming language designed for minimal human involvement in coding.

```
Humans specify WHAT and WHY → Machines handle HOW
```

## Why SLOP?

This started as a thought experiment.  It's still very much an experiment :)

LLMs generate code fast—but without constraints they hallucinate APIs, ignore edge cases, and produce code that "looks right" but fails in production.

SLOP makes the spec the source of truth:

```lisp
(fn transfer ((from Account) (to Account) (amount (Int 1 ..)))
  (@intent "Transfer funds between accounts")
  (@spec ((Account Account (Int 1 ..)) -> (Result Receipt Error)))
  (@pre {from != to})
  (@pre {(. from balance) >= amount})
  (@post {(. from balance) + (. to balance) == (old (. from balance)) + (old (. to balance))})
  ...)
```

- **Contracts are mandatory.** No `@intent` or `@spec`, no compilation.
- **Range types catch bugs at compile time.** `(Int 1 .. 100)`
- **Typed holes constrain generation.** LLMs fill gaps bounded by types, examples, and required variables.

## Status

Currently working on SLOP-native versions of the build chain in lib/compiler.  The Python 'bootstrapped' versions of these are in src/slop and still the 'primary' build tools.  Using the --native flag with slop build will using the slop native tools.  Currently, the parser and transpiler are self-hosting.  The type checker is still a work in progress.

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
- **Minimal spec**: ~50 built-ins, entire language fits in a prompt (~4K tokens)
- **Range types**: `(Int 0 .. 100)` catches bounds errors at compile time (I also like Ada)
- **Mandatory contracts**: `@intent`, `@spec`, `@pre`, `@post` define correctness
- **Infix in contracts**: `{x > 0 and x < 100}` — readable math notation in `@pre`/`@post`
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
    (@pre {limiter != nil})

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
│   ├── HYBRID_PIPELINE.md   Generation architecture
│   └── REFERENCE.md         Quick reference
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

# Verify contracts with Z3 (requires: pip install z3-solver)
slop verify examples/rate-limiter.slop

# Full build (requires cc)
slop build examples/rate-limiter.slop -o rate_limiter

# Language reference (for AI coding assistants)
slop ref                      # Full reference
slop ref types                # Just type system
slop ref --list               # List available topics

# Generate documentation from source
slop doc examples/fibonacci.slop           # Markdown to stdout
slop doc examples/fibonacci.slop -o doc.md # Write to file
slop doc examples/fibonacci.slop -f json   # JSON output for tooling

# Validate a hole implementation against expected type
slop check-hole '(+ x 1)' -t Int -p '((x Int))'

# With context from a file
slop check-hole '(helper 42)' -t Int -c myfile.slop

# From stdin
echo '(ok value)' | slop check-hole -t '(Result T E)'
```

### Native Components

SLOP includes native (self-hosted) implementations of core compiler components written in SLOP itself. Use the `--native` flag to use these instead of the Python implementations:

```bash
# Use native parser
slop parse examples/rate-limiter.slop --native

# Use native type checker
slop check examples/rate-limiter.slop --native

# Use native components for full build (parser, transpiler)
slop build examples/rate-limiter.slop --native
```

Native components are located in `lib/compiler/`:
- `lib/compiler/parser/` - Native S-expression parser
- `lib/compiler/checker/` - Native type checker
- `lib/compiler/transpiler/` - Native SLOP-to-C transpiler

When `--native` is specified, the CLI looks for pre-built binaries (`slop-parser`, `slop-checker`, `slop-transpiler`). If a native component isn't found, it falls back to the Python implementation.

## Project Configuration

Create a `slop.toml` file to configure your project:

```toml
[project]
name = "my-project"
version = "0.1.0"
entry = "src/main.slop"        # Main module

[build]
output = "build/myapp"         # Output path (directory created if needed)
include = ["src", "lib"]       # Module search paths
type = "executable"            # "executable", "static", or "shared"
debug = false

[build.link]
libraries = ["pthread"]        # -l flags
library_paths = []             # -L flags
```

With a `slop.toml`, commands use project settings automatically:

```bash
slop build                     # Uses [project].entry, outputs to [build].output
slop build --debug             # CLI flags override config
slop fill                      # Uses entry from config
slop fill -c slop.toml         # Explicit config path
```

### Hole Filler Configuration

Configure LLM providers and tier routing for `slop fill`:

```toml
[providers.ollama]
type = "ollama"
base_url = "http://localhost:11434"

[providers.openai]
type = "openai-compatible"
base_url = "https://api.openai.com/v1"
api_key = "${OPENAI_API_KEY}"

[tiers.tier-1]
provider = "ollama"
model = "phi3:mini"

[tiers.tier-2]
provider = "ollama"
model = "llama3:8b"

[tiers.tier-3]
provider = "ollama"
model = "llama3:70b-q4"

[tiers.tier-4]
provider = "openai"
model = "gpt-4o"
```

See `slop.toml.example` for complete configuration options.

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

## Typed Holes

Holes are placeholders where LLMs generate code, constrained by types and contracts:

```lisp
(fn validate-age ((age Int))
  (@intent "Check if age is valid for registration")
  (@spec ((Int) -> (Result (Int 18 .. 120) String)))

  (hole (Result (Int 18 .. 120) String)
    "validate age is between 18 and 120, return error message if invalid"
    :complexity tier-2))
```

The hole specifies:
- **Return type**: `(Result (Int 18 .. 120) String)` — must return this exact type
- **Prompt**: Natural language description of what to generate
- **Complexity**: `tier-2` — routes to an appropriately-sized model

Running `slop fill` replaces the hole with a valid implementation:

```lisp
(if (and (>= age 18) (<= age 120))
    (union-new Result ok age)
    (union-new Result error "Age must be between 18 and 120"))
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
  :required (input db-update validate-email))
```

## Why C?

Because that's what I want.  And also:

C's problems are **human** problems:
- Manual memory management? Machines don't forget
- No namespaces? Machines use prefixes consistently
- Buffer overflows? Transpiler generates safe patterns

C's benefits remain:
- 10-100x faster than interpreted languages
- Universal FFI to any library
- 50 years of optimizer engineering
- Runs everywhere

## Other Targets

Aside from C, an obvious choice for a future target would be typescript.
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
- ✓ Contract verification via Z3 (`slop verify`)
- ✓ Test suite

**Not Yet Implemented:**
- Concurrency primitives (Channel based)
- Property-based testing generation
- Contract verification with body analysis (verifies contract consistency, not implementation)

## License

Apache 2.0
