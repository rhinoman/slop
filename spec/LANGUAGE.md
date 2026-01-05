# SLOP Language Specification

**Symbolic LLM-Optimized Programming**

Version 0.2.0

## 1. Design Philosophy

SLOP is designed for hybrid generation where:
- Humans specify intent via contracts and types
- Machines generate implementation
- Machines verify correctness
- Machines compile to efficient native code via C

Core principles:
- S-expression syntax eliminates parsing ambiguity
- Range types catch bounds errors at compile time
- Mandatory contracts define correctness
- Explicit holes enable fine-grained LLM generation
- Transpiles to C for maximum portability and performance

## 2. Lexical Structure

```
; Comments start with semicolon
;; Documentation comments use double semicolon

; Atoms
identifier  = [a-z][a-z0-9-]*
type-name   = [A-Z][a-zA-Z0-9]*
keyword     = [a-z]+
number      = -?[0-9]+(\.[0-9]+)?
string      = "([^"\\]|\\.)*"
symbol      = '[a-z][a-z0-9-]*

; Delimiters
(  )        ; S-expression bounds
```

## 3. Grammar

```
program     = form*
form        = '(' keyword args ')'
args        = (form | atom | literal)*
atom        = identifier | type-name | symbol
literal     = number | string | 'true | 'false | 'nil
```

### 3.1 Top-Level Forms

```
(module name exports? form*)
(export name*)
(import module-name name*)
(type name type-expr)
(const name Type const-expr)
(fn name params body)
```

### 3.2 Constants

```
(const name Type const-expr)

; const-expr: literals, other constants, arithmetic/bitwise on constants, sizeof
; NOT allowed: function calls, variable references, runtime expressions

(const MAX_CONN Int 128)
(const BUF_SIZE U64 (* 4 1024))
(const FLAGS Int (| FLAG_A FLAG_B))
```

**C Mapping:** Integers → `#define`, others → `static const`.

### 3.3 Type Expressions

```
; Primitives with ranges
(Int)                    ; Any integer (int64_t)
(Int min ..)             ; Integer >= min
(Int .. max)             ; Integer <= max
(Int min .. max)         ; Integer in range [min, max]

(I8) (I16) (I32) (I64)   ; Explicit width signed
(U8) (U16) (U32) (U64)   ; Explicit width unsigned

(Float)                  ; 64-bit float (double)
(F32)                    ; 32-bit float

(Bool)                   ; true or false (uint8_t)

(String)                 ; Record: { data: (Ptr U8), len: U64 }
(String .. max-len)      ; String with max length
(String min .. max)      ; String with length in range

(Bytes)                  ; Record: { data: (Ptr U8), len: U64, cap: U64 }
(Bytes .. max-len)       ; Bounded byte buffer

; Collections
(List T)                 ; Dynamic array of T
(List T n)               ; Fixed-size array of exactly n elements
(List T min ..)          ; List with at least min elements
(List T min .. max)      ; List with length in range

(Array T n)              ; Fixed-size array (stack allocated)
(Slice T)                ; View into array/list (pointer + length)

(Map K V)                ; Hash map from K to V
(Set T)                  ; Hash set of T

; Algebraic types
(enum val1 val2 ...)           ; Enumeration (C enum)
(union (tag1 T1) (tag2 T2))    ; Tagged union
(record (field1 T1) (field2 T2))  ; Struct

; Pointers (explicit when needed)
(Ptr T)                  ; Pointer to T
(ScopedPtr T)            ; Scoped pointer (freed when scope ends)
(OptPtr T)               ; Nullable pointer

; Function types
(Fn (T1 T2) -> R)        ; Function pointer

; Utility types
(Option T)               ; T or none - sugar for (union (some T) (none))
(Result T E)             ; Success or error - sugar for (union (ok T) (error E))

; Type aliases
(alias Name Type)
```

### 3.4 Function Definitions

```
(fn name ((param1 Type1) (param2 Type2) ...) 
  annotations*
  body)

; Parameter modes
(fn name ((in param Type)      ; Read-only (default) - pass by value
          (out param (Ptr T))  ; Write-only - output pointer
          (mut param Type))    ; Mutable - value or pointer depending on Type
  ...)

; Memory context (explicit when needed)
(fn name ((arena Arena) (param Type) ...)
  ...)  ; Allocations use arena
```

### 3.5 Annotations

```
; Required annotations
(@intent "Human-readable description")
(@spec ((ParamTypes...) -> ReturnType))

; Optional annotations (prefix or infix notation)
(@pre boolean-expr)              ; Precondition
(@post boolean-expr)             ; Postcondition ($result for return value)
(@assume boolean-expr)           ; Trusted axiom for verification (e.g., FFI behavior)

; Infix notation (contracts only) - curly braces denote infix expressions
(@pre {x > 0})                   ; Equivalent to (@pre (> x 0))
(@pre {x >= 0 and x <= 100})     ; Equivalent to (@pre (and (>= x 0) (<= x 100)))
(@post {$result == a + b})       ; Equivalent to (@post (== $result (+ a b)))
; Precedence: *, /, % > +, - > comparisons > and > or
; Use () for grouping: {(a + b) * c}
; Use prefix for function calls: {(len arr) > 0}
(@example (args...) -> result)   ; Example for testing
(@property (forall (x T) expr))  ; Property that should hold
(@deprecated "message")          ; Mark as deprecated with migration hint

; Memory annotations
(@alloc arena)                   ; Function allocates in arena
(@alloc static)                  ; Function returns static/global data
(@alloc none)                    ; Function does not allocate
(@pure)                          ; No side effects, deterministic

; Hybrid generation annotations
(@generation-mode deterministic)
(@generation-mode template name)
(@generation-mode llm)
(@derived-from "source-path")
(@generated-by source :version v :timestamp t)

; Dependency annotations (for scaffolded/generated code)
(@requires category
  :prompt "Question"              ; Optional - triggers user prompt
  :options ((desc sym)...)        ; Optional - choices for user
  (fn-sig ...)                    ; Required function signatures
  ...)
```

### 3.6 Expressions

```
; Literals
42                       ; Integer
3.14                     ; Float
"hello"                  ; String
'symbol                  ; Symbol (enum value)
true false nil           ; Boolean and nil

; Variables and binding
identifier               ; Variable reference
(let ((name expr)...) body)           ; Immutable bindings (set! disallowed)
(let ((mut name expr)...) body)       ; Mutable bindings (set! allowed)
(let ((mut name Type expr)...) body)  ; Mutable with explicit type
(let* ((name expr)...) body)          ; Sequential bindings

; Control flow
(if cond then else)
(cond (test1 expr1) (test2 expr2) ... (else default))
(match expr ((pattern1) body1) ((pattern2) body2) ...)
(while cond body)
(for (i start end) body)
(for-each (item collection) body)
(do expr1 expr2 ...)         ; Sequence, returns last
(break)
(continue)
(return expr)

; Functions
(fn ((param Type)...) body)      ; Lambda
(name arg1 arg2...)              ; Application

; Data construction
(array e1 e2...)                         ; Fixed array literal
(list [Type] e1 e2...)                   ; Dynamic list (Type optional)
(map [KeyType ValueType] (k1 v1)...)     ; Map literal (types optional)
(record-new Type (f1 v1) (f2 v2)...)     ; Struct construction (named fields)
(TypeName v1 v2 ...)                     ; Struct construction (positional)
(union-new Type Tag value)               ; Tagged union construction

; Collection Literal Type Inference:
; When explicit type is provided, it is used directly:
;   (list Int 1 2 3)              → (List Int)
;   (map String Int ("a" 1))      → (Map String Int)
;
; When type is omitted, inference follows these rules:
;   1. Binding with annotation: (let ((x (List Int) (list 1 2))) ...) → infer from binding
;   2. Passed to typed param: (fn ((lst (List Int))) ...) called with (list 1 2) → infer from param
;   3. Return with @spec: function returns (List Int), (list 1 2) → infer from spec
;   4. Non-empty, no context: (list 1 2 3) → infer from first element
;   5. Empty, no context: (list) → ERROR: explicit type required

; Data access
(. expr field)                   ; Field access (see semantics below)
(@ expr index)                   ; Index access: expr[index]
(put expr field value)           ; Functional update (returns new)
(set! var value)                 ; Variable mutation (requires mut binding)
(set! expr field value)          ; Field mutation (modifies in place)
(deref ptr)                      ; Dereference pointer: (Ptr T) -> T
(addr expr)                      ; Address-of: T -> (Ptr T)

; Field Access Semantics:
; The transpiler automatically selects -> vs . based on pointer tracking:
;   (. value-var field)   → value_var.field   (struct value)
;   (. pointer-var field) → pointer_var->field (pointer to struct)
;
; Pointer types are detected from:
;   - Function parameters declared as (Ptr T)
;   - Variables assigned from arena-alloc

; Arithmetic
(+ a b) (- a b) (* a b) (/ a b) (% a b)
(& a b) (| a b) (^ a b) (<< a n) (>> a n)
(min a b) (max a b)              ; Minimum/maximum of two values

; Comparison  
(== a b) (!= a b) (< a b) (<= a b) (> a b) (>= a b)

; Boolean
(and a b) (or a b) (not a)

; Type casting
(cast Type expr)                 ; Cast expr to Type

; Memory
(arena-new size)                 ; Create arena
(arena-alloc arena size)         ; Allocate in arena
(arena-free arena)               ; Free entire arena
(with-arena size body)           ; Scoped arena

; Error handling
(ok value)
(error reason)
(try expr (catch pattern body))
(? expr)                         ; Early return on error
```

### 3.7 Holes (Hybrid Generation)

Holes support two modes: **generation** (create new code) and **refactoring** (improve existing code).

```
;; Generation mode - no existing code
(hole Type "prompt")

(hole Type "prompt"
  :constraints (expr...)
  :examples ((input...) -> output)...
  :context (identifier...)      ; Whitelist of available identifiers
  :required (identifier...)     ; Identifiers that must appear in output
  :complexity tier)

;; Refactoring mode - existing code provided
(hole Type "prompt"
  existing-code-expression
  :complexity tier)
```

**Refactoring Example:**
```lisp
;; Wrap existing code in a hole to request improvements
(hole Bool "simplify this nested conditional"
  (if (> x 0)
    (if (> y 0)
      (do-thing)
      (do-other))
    (do-default))
  :complexity tier-2)

;; The hole-filler will refactor to something like:
(cond
  ((and (> x 0) (> y 0)) (do-thing))
  ((> x 0) (do-other))
  (else (do-default)))
```

When existing code is present, the hole-filler:
1. Preserves the original semantics
2. Applies the improvement described in the prompt
3. Returns refactored code with the same type

**Complexity Tiers:**
```
; tier-1: 1-3B models (trivial expressions)
; tier-2: 7-8B models (simple conditionals)
; tier-3: 13-34B models (loops, multiple conditions)
; tier-4: 70B+ models (algorithms, complex logic)
```

### 3.8 Requirements (Scaffold Dependencies)

The `@requires` annotation declares external dependencies that must be provided
before code can be filled or compiled. This is used by code generators (like
`slop derive`) to mark incomplete scaffolds.

```
; Basic form - just documents dependencies
(@requires category
  (fn-name ((param Type)...) -> ReturnType)
  ...)

; Interactive form - prompts user for implementation choice
(@requires category
  :prompt "Which approach do you want?"
  :options (("Description one" symbol1)
            ("Description two" symbol2))
  (fn-name ((param Type)...) -> ReturnType)
  ...)
```

**Example: Storage requirements for a generated API**

```lisp
(@requires storage
  :prompt "Which storage approach for the Pet API?"
  :options (
    ("In-memory Map - good for prototypes" map)
    ("Database via FFI - I'll provide db-* functions" db)
    ("Custom implementation - I'll implement myself" custom))
  ;; Required function signatures
  (state-get-pet ((state (Ptr PetState)) (id PetId)) -> (Option (Ptr Pet)))
  (state-insert-pet ((state (Ptr PetState)) (pet (Ptr NewPet))) -> Pet)
  (state-delete-pet ((state (Ptr PetState)) (id PetId)) -> Bool))
```

**Behavior:**

1. **LLM/Claude Code**: When `:prompt` is present, the LLM should ask the user
   which option they prefer and generate appropriate code based on choice
2. **`slop fill`**: Warns if unresolved `@requires` exist and suggests resolution
3. **Type checker**: Can verify that `:context` and `:required` in holes reference
   functions declared in `@requires`

### 3.9 Patterns

```
_                            ; Wildcard
identifier                   ; Binding (captures matched value)
'identifier                  ; Quoted value (enum variant match)
literal                      ; Literal match (number, string)
(array p1 p2...)             ; Array pattern
(list p1 p2... | rest)       ; List with rest
(record Type (f1 p1)...)     ; Struct pattern
(union Tag pattern)          ; Tagged union variant
(guard pattern when expr)    ; Guarded pattern
```

**Enum matching**: Use quoted symbols `'Fizz` for enum value matches. Bare identifiers
are bindings (capture the value), not value matches.

```lisp
(match result
  ('Fizz (println "Fizz"))     ; Match enum value Fizz
  ('Buzz (println "Buzz"))     ; Match enum value Buzz
  (_ (println "other")))       ; Wildcard
```

**Exhaustiveness**: Match expressions must be exhaustive—all variants of the
matched type must be covered, or a wildcard (`_` or `else`) must be present.

## 4. Module System

```
(module user-service
  (export create find update delete)
  (import core arena-new arena-free)
  (import strings concat len)
  
  (type UserId (Int 1 ..))
  (type User (record
    (id UserId)
    (name (String 1 .. 100))
    (email (String 1 .. 255))
    (status (enum active inactive deleted))))
  
  (fn create ((arena Arena) (name String) (email String))
    (@intent "Create a new user")
    (@spec ((Arena String String) -> (Result (Ptr User) Error)))
    (@alloc arena)
    ...))
```

## 5. Memory Model

### 5.1 Arena Allocation (Primary)

Most allocations use arenas for simplicity and performance:

```
(fn handle-request ((req Request))
  (@intent "Handle incoming request")
  (with-arena 4096
    (let ((user (parse-user arena req))
          (response (process arena user)))
      (send-response response))))
; Arena automatically freed at end of with-arena
```

### 5.2 Ownership (When Needed)

For data that outlives a request:

```
(type Connection (record
  (socket (ScopedPtr Socket))    ; Freed when Connection is freed
  (buffer (ScopedPtr Bytes))))

(fn connection-close ((conn (ScopedPtr Connection)))
  (@intent "Close connection and free resources")
  ; Compiler generates cleanup code
  ...)
```

### 5.3 Borrowing (Views)

For read-only access without ownership:

```
(fn process-name ((name (Slice U8)))
  (@intent "Process a name without copying")
  (@pure)
  ; name is a view, no allocation or freeing
  ...)
```

## 6. C Mapping

### 6.1 Types

```
SLOP                    C
────                    ─
(Int)                   int64_t
(Int 0 .. 255)          uint8_t (with range check)
(I32)                   int32_t
(U64)                   uint64_t
(Float)                 double
(F32)                   float
(Bool)                  uint8_t
(String)                slop_string
(Bytes)                 slop_bytes
(List T)                slop_list_T
(Array T n)             T[n]
(Map K V)               slop_map_K_V
(enum a b c)            enum { a, b, c }
(record (x T) (y U))    struct { T x; U y; }
(union (a T) (b U))     struct { uint8_t tag; union { T a; U b; } data; }
(Ptr T)                 T*
(ScopedPtr T)           T* (with cleanup)
(Fn (A B) -> R)         R (*)(A, B)
```

#### Range Type Optimization

The transpiler automatically selects the smallest C integer type that fits the range:

```
(Int 0 .. 255)          → uint8_t
(Int 0 .. 65535)        → uint16_t
(Int -128 .. 127)       → int8_t
(Int -32768 .. 32767)   → int16_t
(Int 0 ..)              → int64_t (unbounded)
```

Range constructors (e.g., `TypeName_new(v)`) are generated with `SLOP_PRE` checks to validate bounds at runtime.

### 6.2 Contracts

```
SLOP                    C
────                    ─
(@pre (> x 0))          SLOP_PRE(x > 0, "x > 0")
(@post (> $result 0))   SLOP_POST(result > 0, "result > 0")

; In debug mode: assertion
; In release mode: removed or __assume()
```

## 7. Verification Levels

```
1. Parse-time
   - S-expression well-formed
   - Keywords recognized

2. Generation-time (LLM self-check)
   - Holes have valid types
   - Intent matches implementation

3. Compile-time  
   - Type inference and checking
   - Range analysis
   - Contract consistency
   - Exhaustiveness checking
   - Memory safety (ownership/borrowing)

4. Test-time
   - @example execution
   - @property testing
   - Contract assertion (debug builds)

5. Runtime (debug mode)
   - @pre/@post assertions
   - Range bounds checking
   - Null checks
```

## 8. Standard Library

Minimal runtime (~500 lines of C):

```
; Memory
(arena-new size) -> Arena
(arena-alloc arena size) -> (Ptr U8)  
(arena-free arena) -> Unit

; Strings
(string-new arena cstr) -> String
(string-len s) -> (Int 0 ..)
(string-concat arena a b) -> String
(string-eq a b) -> Bool
(string-slice s start end) -> (Slice U8)
(string-split arena s delimiter) -> (List String)  ; delimiter must be single char
(int-to-string arena n) -> String                  ; Convert integer to string

; Lists (homogeneous, type-safe)
(list-new arena Type) -> (List Type)   ; Type parameter required for type safety
(list-push list item) -> Unit
(list-get list index) -> (Option T)
(list-pop list) -> (Option T)          ; Remove and return last element
(list-len list) -> (Int 0 ..)

; Maps (homogeneous, type-safe)
(map-new arena KeyType ValueType) -> (Map KeyType ValueType)  ; Type parameters required
(map-put map key val) -> Unit
(map-get map key) -> (Option V)
(map-has map key) -> Bool
(map-keys map) -> (List K)               ; Return list of all keys
(map-remove map key) -> Unit             ; Remove key from mutable map

; Results
(ok val) -> (Result T E)
(error e) -> (Result T E)
(is-ok result) -> Bool
(unwrap result) -> T

; Time
(now-ms) -> (Int 0 ..)
(sleep-ms ms) -> Unit
```

**Note:** The functions above are BUILTINS - no import needed. For additional string
operations (starts-with, contains, trim, parse-int, etc.), import from strlib:

```
(import strlib (starts-with ends-with contains trim parse-int float-to-string))
```

## 9. FFI

SLOP provides direct access to C libraries through FFI declarations. C libraries are the SLOP ecosystem.

### 9.1 Function Declarations

```
(ffi "header.h"
  (func-name ((param1 Type1) (param2 Type2)) ReturnType)
  ...)
```

Example:
```
(ffi "openssl/sha.h"
  (SHA256 ((data (Ptr U8)) (len U64) (out (Ptr U8))) (Ptr U8)))

(ffi "curl/curl.h"
  (curl-easy-init () (Ptr CurlHandle))
  (curl-easy-setopt ((handle (Ptr CurlHandle)) (opt Int) (val (Ptr U8))) Int))

;; Use directly
(fn hash-data ((arena Arena) (data Bytes))
  (@intent "Hash data with SHA256")
  (let ((out (cast (Ptr U8) (arena-alloc arena 32))))
    (SHA256 data.ptr data.len out)
    (bytes-from-ptr out 32)))
```

**Constants**: The `ffi` form can also declare constants from C headers:

```
(ffi "stdio.h"
  (SEEK_SET Int)    ;; constant - (name Type)
  (SEEK_CUR Int)
  (EOF Int)
  (fclose ((file (Ptr Void))) Int))  ;; function - (name ((params)) ReturnType)
```

Constants are distinguished from functions by having a type symbol as the second element instead of a parameter list. No C code is emitted - the symbol passes through directly to C.

### 9.2 Struct Declarations

Map C structs for field access:

```
(ffi-struct "netinet/in.h" sockaddr_in
  (sin_family U16)
  (sin_port U16)
  (sin_addr U32)
  (sin_zero (Array U8 8)))

;; Access fields with dot notation
(fn make-sockaddr ((arena Arena) (port (Int 1 .. 65535)))
  (let ((addr (cast (Ptr sockaddr_in) (arena-alloc arena (sizeof sockaddr_in)))))
    (set! addr.sin_family 2)       ; AF_INET
    (set! addr.sin_port (htons port))
    (set! addr.sin_addr 0)         ; INADDR_ANY
    addr))
```

**C name override**: When the SLOP name differs from the C struct name, use `:c-name`:

```
(ffi-struct "sys/stat.h" stat_buf :c-name "stat"
  (st_size I64)
  (st_mode U32))
```

This maps `stat_buf` in SLOP to `struct stat` in C (the `struct` prefix is added automatically for non-`_t` types).

### 9.3 C Inline Escape Hatch

For C expressions SLOP doesn't model yet:

```
(c-inline "INADDR_ANY")                    ; C constant
(c-inline "sizeof(struct sockaddr_in)")    ; Complex sizeof
(c-inline "((struct foo){.x = 1})")        ; Compound literal
```

### 9.4 Build Integration

Link required libraries when building:

```bash
slop build app.slop -o app -lssl -lcrypto -lcurl -lpq
```
