"""
SLOP Language Reference - Optimized for AI coding assistants

When spec/LANGUAGE.md is updated, this file must be updated to match.
"""

TOPICS = {
    'types': """## Types

### Primitives
(Int)                   ; int64_t, any value
(I8) (I16) (I32) (I64)  ; Signed integers
(U8) (U16) (U32) (U64)  ; Unsigned integers
(Float) (F32)           ; double / float
(Bool)                  ; Boolean
(String)                ; slop_string
(Bytes)                 ; Byte buffer
(Unit)                  ; void / no value

### Range Types
(Int min ..)            ; >= min
(Int .. max)            ; <= max
(Int min .. max)        ; Bounded range
(String min .. max)     ; Length-bounded string
(Float min .. max)      ; Bounded float

; Examples
(type UserId (Int 1 ..))
(type Age (Int 0 .. 150))
(type Port (Int 1 .. 65535))

; C mapping: (Int 0 .. 255) -> uint8_t with runtime check

### Collections
(List T)                ; Dynamic array
(List T n)              ; Exactly n elements
(List T min ..)         ; At least min
(Array T n)             ; Fixed-size, stack-allocated
(Map K V)               ; Hash map
(Set T)                 ; Hash set

; Literals
(list Int 1 2 3)                    ; Explicit type
(list 1 2 3)                        ; Inferred type
(map String Int ("a" 1) ("b" 2))    ; Explicit types
(map ("a" 1) ("b" 2))               ; Inferred types

### Algebraic Types
(type Status (enum pending active done))
(type User (record (id Int) (name String)))
(type Shape (union (circle Float) (rect Float Float) (point)))

### Pointers
(Ptr T)                 ; Borrowed pointer (T*)
(ScopedPtr T)           ; Scoped, auto-freed on scope exit
(OptPtr T)              ; Nullable pointer

### Utility Types
(Option T)              ; T or none
(Result T E)            ; Success or error
(Fn (A B) -> R)         ; Function pointer

### Concurrency Types (thread library)
(Chan T)                ; Typed channel
(Thread T)              ; Thread handle returning T

### Type Aliases
(type UserId (Int 1 ..))
(alias Handler (Fn (Request) -> Response))
""",

    'functions': """## Functions

### Basic Structure
(fn name ((param1 Type1) (param2 Type2))
  (@intent "Human-readable purpose")      ; REQUIRED
  (@spec ((Type1 Type2) -> ReturnType))   ; REQUIRED
  body)

### Parameter Modes
(fn example ((in x Type)         ; Read-only (default), pass by value
             (out result Type)   ; Write-only, pointer to uninitialized
             (mut state Type))   ; Read-write, pointer to initialized
  ...)

### With Arena (for allocating functions)
(fn create-user ((arena Arena) (name String))
  (@intent "Create a new user in arena")
  (@spec ((Arena String) -> (Ptr User)))
  (@alloc arena)
  (let ((user (arena-alloc arena (sizeof User))))
    (set! user.name name)
    user))

### Impl (implementation without annotations)
(impl helper ((x Int))
  (+ x 1))

### C Name Override (for external interop)
(fn slop-parse-int ((s (Ptr Char)))
  (@intent "Parse integer from string")
  (@spec (((Ptr Char)) -> Int))
  (strtol s nil 10)
  :c-name "parse_int")    ; Emits as parse_int() in C

The :c-name attribute specifies a clean C name for external code.
Transpiler emits both the clean name and a #define alias.
""",

    'contracts': """## Contracts

### Required Annotations
(@intent "Human-readable purpose")         ; What the function does
(@spec ((ParamTypes) -> ReturnType))       ; Type signature

### Preconditions and Postconditions
(@pre condition)           ; Input validation, checked on entry
(@post condition)          ; Output guarantee, $result = return value

; Contracts support optional infix notation with curly braces:
(@pre {x > 0})             ; Equivalent to (@pre (> x 0))
(@pre {x >= 0 and x <= 100})  ; Equivalent to (@pre (and (>= x 0) (<= x 100)))
(@post {$result == a + b}) ; Equivalent to (@post (== $result (+ a b)))

; Infix precedence: *, /, % > +, - > comparisons > and > or
; Use () for grouping: {(a + b) * c}
; Use prefix notation for function calls within infix: {(len arr) > 0}

; Example (both styles work)
(fn divide ((a Int) (b Int))
  (@intent "Divide a by b")
  (@spec ((Int Int) -> Int))
  (@pre {b != 0})                  ; Infix style
  (@post (== (* $result b) a))     ; Prefix style
  (/ a b))

### Assumptions (for FFI and trusted behavior)
(@assume condition)        ; Trusted axiom for verification

; Use @assume for FFI wrappers where behavior can't be verified
; The verifier trusts @assume as an axiom, runtime still checks it
(fn abs-float ((x Float))
  (@intent "Return absolute value of float")
  (@spec ((Float) -> Float))
  (@assume (>= $result 0.0))   ; Tell verifier to trust this
  (fabs x))                    ; FFI call - verifier can't analyze

### Function Properties
(@pure)                    ; No side effects, deterministic
(@alloc arena)             ; Allocates in specified arena
(@alloc none)              ; No allocation

### Examples (executable test cases)
(@example (arg1 arg2) -> expected)

; Multiple examples recommended
(fn factorial ((n (Int 0 ..)))
  (@intent "Calculate n!")
  (@spec (((Int 0 ..)) -> (Int 1 ..)))
  (@example (0) -> 1)
  (@example (1) -> 1)
  (@example (5) -> 120)
  ...)

### Deprecation
(@deprecated "message")                 ; Mark function as deprecated

; Example
(fn old-api ((x Int))
  (@intent "Old API function")
  (@spec ((Int) -> Int))
  (@deprecated "use new-api instead")
  x)

; Calling deprecated functions emits a warning during type checking

### Advanced Annotations
(@property (forall (x T) expr))        ; Property assertion
(@generation-mode mode)                 ; deterministic|template|llm
(@requires category :prompt "..." ...)  ; Dependency requirements
""",

    'holes': """## Holes (LLM Generation Points)

Holes support two modes: generation (new code) and refactoring (improve existing code).

### Generation Mode (no existing code)
(hole Type "prompt")

(hole Type "prompt"
  :complexity tier-2          ; tier-1 to tier-4
  :context (var1 fn1)         ; Whitelist of available identifiers
  :required (var1)            ; Identifiers that MUST appear in output
  :examples ((in) -> out))    ; Example behavior

### Refactoring Mode (existing code provided)
(hole Type "prompt"
  existing-code               ; Code to refactor
  :complexity tier-2)

### Complexity Tiers
tier-1: 1-3B models   ; Trivial expressions, simple arithmetic
tier-2: 7-8B models   ; Simple conditionals, basic logic
tier-3: 13-34B models ; Loops, moderate conditionals
tier-4: 70B+ models   ; Complex algorithms, multi-step logic

### Examples

; Generation: Simple hole
(hole Int "calculate the sum of x and y"
  :context (x y))

; Generation: Complex hole with constraints
(hole (List Int) "sort the input list"
  :complexity tier-3
  :context (input compare)
  :required (input)
  :examples (((list 3 1 2)) -> (list 1 2 3)))

; Refactoring: Simplify nested conditionals
(hole Bool "simplify this logic"
  (if (> x 0)
    (if (> y 0) true false)
    false)
  :complexity tier-2)
; Result: (and (> x 0) (> y 0))

### Best Practices
; Use :context to whitelist what the LLM can use
; Use :required for identifiers that MUST appear
; Match tier to actual complexity needed
; For refactoring, existing code must type-check
""",

    'memory': """## Memory Model

### Arena Allocation (Primary Pattern)
(arena-new size)                 ; Create arena with capacity
(arena-alloc arena size)         ; Allocate from arena
(arena-free arena)               ; Free entire arena

; With arena parameter
(fn process ((arena Arena) (data Input))
  (@alloc arena)
  (let ((result (arena-alloc arena (sizeof Output))))
    ...))

### Scoped Arena
(with-arena 4096
  (let ((x (arena-alloc arena size)))
    ...))  ; Arena auto-freed at end

### Pointer Types
(Ptr T)                          ; Borrowed, non-owning
(ScopedPtr T)                    ; Auto-freed on scope exit
(OptPtr T)                       ; Nullable

### Pointer Operations
(deref ptr)                      ; Dereference: (Ptr T) -> T
(addr expr)                      ; Address-of: T -> (Ptr T)
(. ptr field)                    ; Field access (auto -> vs .)

### Slices (Borrowed Views)
(Slice T)                        ; Non-owning view into array/list
(string-slice s start end)       ; Create slice from string
""",

    'ffi': """## FFI (Foreign Function Interface)

### Function Declaration
(ffi "header.h"
  (func-name ((param Type)...) ReturnType)
  (CONSTANT_NAME Type))          ; Constants: just (name Type)

; Example
(ffi "unistd.h"
  (read ((fd Int) (buf (Ptr U8)) (n U64)) I64)
  (write ((fd Int) (buf (Ptr U8)) (n U64)) I64)
  (close ((fd Int)) Int))

### Struct Declaration
(ffi-struct "header.h" struct_name
  (field1 Type1)
  (field2 Type2))

; With C name override
(ffi-struct "sys/stat.h" stat_buf :c-name "stat"
  (st_size I64)
  (st_mode U32))

; Example
(ffi-struct "netinet/in.h" sockaddr_in
  (sin_family U16)
  (sin_port U16)
  (sin_addr U32))

### C Inline Escape
(c-inline "CONSTANT")            ; Emit C constant
(c-inline "sizeof(struct foo)")  ; Emit C expression

### FFI-Only Types

#### Char
For C functions expecting `char*` (distinct from `int8_t*` and `uint8_t*`):
```lisp
(ffi "stdlib.h"
  (strtol ((s (Ptr Char)) (endptr (Ptr (Ptr Char))) (base Int)) I64))
```
Use only at FFI boundaries. For general code, use `U8` or `String`.

### Type Casting
(cast Type expr)                 ; Cast expression to Type
""",

    'stdlib': """## Standard Library

### Memory
(arena-new size) -> Arena
(arena-alloc arena size) -> (Ptr U8)
(arena-free arena) -> Unit

### Strings
(string-new arena cstr) -> String
(string-len s) -> (Int 0 ..)
(string-concat arena a b) -> String
(string-eq a b) -> Bool
(string-slice s start end) -> (Slice U8)
(string-split arena s delim) -> (List String)
(int-to-string arena n) -> String

### Lists
(list-new arena Type) -> (List Type)
(list Type e1 e2...) -> (List Type)     ; Literal
(list-push list item) -> Unit
(list-pop list) -> (Option T)
(list-get list idx) -> (Option T)
(list-len list) -> (Int 0 ..)
(list-set list idx val) -> Unit

### Maps
(map-new arena K V) -> (Map K V)
(map K V (k1 v1)...) -> (Map K V)       ; Literal
(map-put map k v) -> Unit
(map-get map k) -> (Option V)
(map-has map k) -> Bool
(map-keys map) -> (List K)
(map-remove map k) -> Unit              ; Requires mutable map

### Results
(ok val) -> (Result T E)
(error e) -> (Result T E)
(is-ok r) -> Bool
(is-error r) -> Bool
(unwrap r) -> T                          ; Panics on error

### I/O
(file-read path) -> (Result String IoError)
(file-write path content) -> (Result Unit IoError)

### Time
(now-ms) -> (Int 0 ..)
(sleep-ms ms) -> Unit

### Concurrency (import thread, link -lpthread)
(import thread (chan chan-buffered chan-close send recv try-recv spawn join))

(chan arena) -> (Ptr (Chan T))              ; Unbuffered channel
(chan-buffered arena cap) -> (Ptr (Chan T)) ; Buffered channel
(chan-close ch) -> Unit                     ; Close channel
(send ch val) -> (Result Unit ChanError)    ; Blocking send
(recv ch) -> (Result T ChanError)           ; Blocking receive
(try-recv ch) -> (Result T ChanError)       ; Non-blocking receive
(spawn arena func) -> (Ptr (Thread T))      ; Spawn thread
(join thread) -> T                          ; Wait and get result
""",

    'expressions': """## Expressions

### Bindings
(let ((name expr)...) body)              ; Immutable
(let ((mut name expr)...) body)          ; Mutable
(let ((mut name Type expr)...) body)     ; Mutable with explicit type
(set! var value)                         ; Mutation (requires mut)

### Control Flow
(if cond then else)
(if cond then)                           ; else is Unit
(cond (test1 e1) (test2 e2) (else default))
(match expr ((pat1) body1) ((pat2) body2)...)

### Loops
(for (i start end) body)                 ; i from start to end-1
(for-each (x collection) body)           ; Iterate collection
(while cond body)
(break)                                  ; Exit loop
(continue)                               ; Next iteration
(return expr)                            ; Early return

### Sequencing
(do e1 e2 e3...)                         ; Evaluate in order, return last

### Data Construction
(array e1 e2...)                         ; Array literal
(list Type e1 e2...)                     ; List literal
(map K V (k1 v1)...)                     ; Map literal
(record-new Type (f1 v1) (f2 v2)...)     ; Record constructor
(TypeName v1 v2...)                      ; Positional constructor

### Data Access
(. expr field)                           ; Field access
expr.field                               ; Shorthand
(@ expr idx)                             ; Index access
(put expr field val)                     ; Functional update (new copy)
(set! expr.field val)                    ; Mutation (in-place)

### Operators
(+ - * / %)                              ; Arithmetic
(== != < <= > >=)                        ; Comparison
(and or not)                             ; Boolean
(& | ^ << >> ~)                          ; Bitwise
(min a b) (max a b)                      ; Min/max

### Error Handling
(? fallible-expr)                        ; Early return on error
(try expr (catch e body))                ; Try-catch
""",

    'patterns': """## Pattern Matching

### Basic Patterns
_                           ; Wildcard (matches anything)
identifier                  ; Binding (captures value)
literal                     ; Literal match (number, string)
'symbol                     ; Quoted symbol (for enum variants)

### Enum Matching (IMPORTANT: use quotes)
(match status
  ('active ...)             ; Quote the variant
  ('inactive ...)
  (_ ...))                  ; Wildcard for default

### Structured Patterns
(array p1 p2...)           ; Array destructuring
(list p1 p2... | rest)     ; List with rest binding
(record Type (f1 p1)...)   ; Record destructuring
(union Tag pat)            ; Union variant matching

### Guarded Patterns
(guard pat when expr)      ; Pattern with condition

; Example
(match value
  ((guard n when (> n 0)) (handle-positive n))
  ((guard n when (< n 0)) (handle-negative n))
  (0 (handle-zero)))

### Result/Option Matching
(match result
  ((ok val) (use val))
  ((error e) (handle e)))

(match option
  ((some x) (use x))
  ((none) (default)))

### Exhaustiveness
All variants must be covered, or use wildcard (_).
Type checker enforces exhaustive matching.
""",

    'mistakes': """## Common Mistakes

These DO NOT exist in SLOP - use the alternatives:

| Don't Use | Use Instead |
|-----------|-------------|
| `print-int n` | `(println (int-to-string arena n))` |
| `print-float n` | `(println (float-to-string arena n))` |
| `(println enum-value)` | Use `match` to print different strings |
| `arena` outside with-arena | Wrap code in `(with-arena size ...)` |
| `(block ...)` | `(do ...)` for sequencing |
| `(begin ...)` | `(do ...)` for sequencing |
| `strlen s` | `(string-len s)` |
| `malloc` | `(arena-alloc arena size)` |
| `list.length` | `(list-len list)` |
| `list-append` | `(list-push list elem)` |
| `map-set` | `(map-put map key val)` |
| `hash-get` | `(map-get map key)` |
| Definitions outside module | All `(type)`, `(fn)`, `(const)` inside `(module ...)` |

### Module Structure

All definitions must be INSIDE the module form:

; CORRECT:
(module my-module
  (export public-fn)

  (type MyType (Int 0 ..))

  (fn public-fn (...)
    ...))  ; <-- closing paren wraps entire module

; WRONG:
(module my-module
  (export public-fn))

(fn public-fn ...)  ; ERROR: outside module form

### Error Returns

IMPORTANT: Quote error variants!

(error 'not-found)     ; CORRECT: quoted
(error not-found)      ; WRONG: undefined variable

### Builtin vs Library Functions

These string/list functions are BUILTINS - do NOT import from strlib:

| Builtin (no import) | What it does |
|---------------------|--------------|
| `(string-len s)` | Get string length |
| `(string-concat arena a b)` | Concatenate strings |
| `(string-eq a b)` | Compare strings |
| `(string-new arena cstr)` | Create string from C string |
| `(string-slice s start end)` | Get substring slice |
| `(string-split arena s delim)` | Split string |
| `(int-to-string arena n)` | Convert int to string |
| `(list-len list)` | Get list length |
| `(list-get list idx)` | Get element at index |
| `(list-push list item)` | Append to list |

These ARE in strlib and need `(import strlib ...)`:

| strlib function | What it does |
|-----------------|--------------|
| `starts-with`, `ends-with` | String prefix/suffix check |
| `contains`, `index-of` | Substring search |
| `trim`, `trim-start`, `trim-end` | Whitespace removal |
| `substring`, `replace`, `replace-all` | String manipulation |
| `to-upper`, `to-lower`, `capitalize` | Case conversion |
| `parse-int`, `parse-float` | String to number |
| `float-to-string` | Float to string |
| `compare`, `compare-ignore-case` | String comparison |
| `join`, `reverse`, `repeat` | Advanced operations |
| `fill-bytes` | Fill memory region with byte value |
""",

    'cli': """## CLI Reference

### Commands

| Command | Description |
|---------|-------------|
| `slop parse FILE` | Parse and display AST |
| `slop check FILE` | Type check without transpiling |
| `slop transpile FILE` | Convert to C source |
| `slop build FILE` | Full pipeline: parse, check, transpile, compile |
| `slop fill FILE` | Fill holes with LLM-generated code |
| `slop verify FILE` | Verify contracts with Z3 |
| `slop ref [TOPIC]` | Show language reference |
| `slop doc FILE` | Generate documentation |

### Native Components (default)

SLOP includes self-hosted compiler components written in SLOP. **Native tools are used by default.** Use `--python` to fall back to Python implementations:

```bash
slop parse FILE               # Uses native parser (default)
slop check FILE               # Uses native type checker (default)
slop build FILE               # Uses native parser + transpiler (default)

slop parse FILE --python      # Use Python parser
slop check FILE --python      # Use Python type checker
slop build FILE --python      # Use Python toolchain
```

Native components are in `lib/compiler/`:
- `slop-parser` - S-expression parser (outputs JSON AST)
- `slop-checker` - Type checker with diagnostics
- `slop-transpiler` - SLOP to C transpiler

If a native component isn't found, automatically falls back to Python.

### Common Options

| Option | Commands | Description |
|--------|----------|-------------|
| `-o, --output` | transpile, build | Output file path |
| `-I, --include` | transpile, build | Add module search path |
| `--python` | parse, check, build | Use Python fallback |
| `--debug` | build | Include debug symbols |
| `--holes` | parse | Show only holes |
| `-v, --verbose` | fill, verify | Increase verbosity |

### Build Configuration

With `slop.toml`, commands use project settings:

```bash
slop build                    # Uses [project].entry, native tools
slop build --python           # Python toolchain + config
slop fill                     # Uses entry from config
```

See `slop.toml.example` for configuration options.
""",
}

# Ordered list of topics for display
TOPIC_ORDER = [
    'types',
    'functions',
    'contracts',
    'holes',
    'memory',
    'ffi',
    'stdlib',
    'expressions',
    'patterns',
    'mistakes',
    'cli',
]


def list_topics() -> list:
    """Return list of available topics in display order."""
    return TOPIC_ORDER


def get_reference(topic: str = 'all') -> str:
    """Get reference content for a topic or all topics.

    Args:
        topic: Topic name or 'all' for full reference

    Returns:
        Reference content as string
    """
    if topic == 'all':
        sections = []
        for t in TOPIC_ORDER:
            sections.append(TOPICS[t])
        return '\n\n'.join(sections)

    if topic in TOPICS:
        return TOPICS[topic]

    return f"Unknown topic: {topic}\nAvailable: {', '.join(TOPIC_ORDER)}"
