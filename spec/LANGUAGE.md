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
(export (name arity)*)
(import module-name (name arity)*)
(type name type-expr)
(fn name params body)
(structure form*)
(logic form*)
```

### 3.2 Type Expressions

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

(String)                 ; Immutable, length-prefixed
(String .. max-len)      ; String with max length
(String min .. max)      ; String with length in range

(Bytes)                  ; Raw byte buffer
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
(OwnPtr T)               ; Owning pointer (freed when scope ends)
(OptPtr T)               ; Nullable pointer

; Function types
(Fn (T1 T2) -> R)        ; Function pointer

; Utility types
(Option T)               ; T or none - sugar for (union (some T) (none))
(Result T E)             ; Success or error - sugar for (union (ok T) (error E))

; Type aliases
(alias Name Type)
```

### 3.3 Function Definitions

```
(fn name ((param1 Type1) (param2 Type2) ...) 
  annotations*
  body)

; Parameter modes **[PLANNED]**
; Note: Parameter modes are not yet implemented. All parameters are currently pass-by-value.
(fn name ((in param Type)      ; Read-only (default) - pass by value or const pointer
          (out param Type)     ; Write-only - pointer to uninitialized
          (mut param Type))    ; Read-write - pointer to initialized
  ...)

; Memory context (explicit when needed)
(fn name ((arena Arena) (param Type) ...)
  ...)  ; Allocations use arena
```

### 3.4 Annotations

```
; Required annotations
(@intent "Human-readable description")
(@spec ((ParamTypes...) -> ReturnType))

; Optional annotations  
(@pre boolean-expr)              ; Precondition
(@post boolean-expr)             ; Postcondition ($result for return value)
(@example (args...) -> result)   ; Example for testing
(@property (forall (x T) expr))  ; Property that should hold

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
```

### 3.5 Expressions

```
; Literals
42                       ; Integer
3.14                     ; Float
"hello"                  ; String
'symbol                  ; Symbol (enum value)
true false nil           ; Boolean and nil

; Variables and binding
identifier               ; Variable reference
(let ((name expr)...) body)
(let* ((name expr)...) body)

; Control flow
(if cond then else)
(cond (test1 expr1) (test2 expr2) ... (else default))
(match expr ((pattern1) body1) ((pattern2) body2) ...)
(while cond body)
(for (i start end) body)
(for-each (item collection) body)
(break)
(continue)
(return expr)

; Functions
(fn ((param Type)...) body)      ; Lambda
(name arg1 arg2...)              ; Application

; Data construction
(array e1 e2...)                     ; Fixed array literal
(list e1 e2...)                      ; Dynamic list
(map (k1 v1) (k2 v2)...)             ; Map literal **[PLANNED]**
(record-new Type (f1 v1) (f2 v2)...) ; Struct construction
(union-new Type Tag value)           ; Tagged union construction

; Data access
(. expr field)                   ; Field access (see semantics below)
(@ expr index)                   ; Index access: expr[index]
(put expr field value)           ; Functional update (returns new)
(set! expr field value)          ; Mutation (modifies in place)

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

### 3.6 Holes (Hybrid Generation)

```
(hole Type "prompt")

(hole Type "prompt"
  :constraints (expr...)
  :examples ((input...) -> output)...
  :must-use (identifier...)
  :complexity tier)

; Complexity tiers
; tier-1: 1-3B models (trivial expressions)
; tier-2: 7-8B models (simple conditionals)
; tier-3: 13-34B models (loops, multiple conditions)
; tier-4: 70B+ models (algorithms, complex logic)
```

### 3.7 Patterns

```
_                            ; Wildcard
identifier                   ; Binding
literal                      ; Literal match
(array p1 p2...)             ; Array pattern
(list p1 p2... | rest)       ; List with rest
(record Type (f1 p1)...)     ; Struct pattern
(union Tag pattern)          ; Tagged union variant
(guard pattern when expr)    ; Guarded pattern
```

## 4. Module System

> **Note**: Module exports are parsed but linking is not yet implemented. Imports are **[PLANNED]**.

```
(module user-service
  (export (create 1) (find 1) (update 2) (delete 1))
  (import core (arena-new 1) (arena-free 1))      ; [PLANNED]
  (import strings (concat 2) (len 1))             ; [PLANNED]
  
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

## 5. Structure/Logic Separation **[PLANNED]**

> **Note**: `structure`/`logic` blocks and `sig`/`impl` separation are planned features not yet implemented in the transpiler. Currently, use `fn` definitions directly.

```
; Structure block - deterministic generation
(structure
  (module payment
    (export (process 1) (refund 1))

    (type Payment (record ...))
    (type Receipt (record ...))

    (sig process ((arena Arena) (payment (Ptr Payment)))
         (Result (Ptr Receipt) Error))
    (sig refund ((arena Arena) (id PaymentId))
         (Result (Ptr Receipt) Error))))

; Logic block - LLM generation
(logic
  (impl process ((arena Arena) (payment (Ptr Payment)))
    (@intent "Process payment through gateway")
    (@pre (!= payment nil))
    (@pre (== (. payment status) 'pending))
    (@alloc arena)

    ; Implementation with holes
    ...))
```

## 6. Memory Model

### 6.1 Arena Allocation (Primary)

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

### 6.2 Ownership (When Needed)

For data that outlives a request:

```
(type Connection (record
  (socket OwnPtr Socket)    ; Freed when Connection is freed
  (buffer OwnPtr Bytes)))

(fn connection-close ((conn (OwnPtr Connection)))
  (@intent "Close connection and free resources")
  ; Compiler generates cleanup code
  ...)
```

### 6.3 Borrowing (Views)

For read-only access without ownership:

```
(fn process-name ((name (Slice U8)))
  (@intent "Process a name without copying")
  (@pure)
  ; name is a view, no allocation or freeing
  ...)
```

## 7. C Mapping

### 7.1 Types

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
(OwnPtr T)              T* (with cleanup)
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

### 7.2 Contracts

```
SLOP                    C
────                    ─
(@pre (> x 0))          SLOP_PRE(x > 0, "x > 0")
(@post (> $result 0))   SLOP_POST(result > 0, "result > 0")

; In debug mode: assertion
; In release mode: removed or __assume()
```

## 8. Verification Levels

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

## 9. Standard Library

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

; Lists
(list-new arena) -> (List T)
(list-push arena list item) -> Unit
(list-get list index) -> (Option T)
(list-len list) -> (Int 0 ..)

; Maps
(map-new arena) -> (Map K V)
(map-put arena map key val) -> Unit
(map-get map key) -> (Option V)
(map-has map key) -> Bool

; Results
(ok val) -> (Result T E)
(error e) -> (Result T E)
(is-ok result) -> Bool
(unwrap result) -> T

; I/O (via FFI)
(file-read path) -> (Result Bytes IoError)
(file-write path data) -> (Result Unit IoError)

; Time
(now-ms) -> (Int 0 ..)
(sleep-ms ms) -> Unit
```

## 10. FFI

SLOP provides direct access to C libraries through FFI declarations. 50 years of C libraries are the SLOP ecosystem.

### 10.1 Function Declarations

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
  (let ((out (arena-alloc arena 32)))
    (SHA256 data.ptr data.len out)
    (bytes-from-ptr out 32)))
```

### 10.2 Struct Declarations

Map C structs for field access:

```
(ffi-struct "netinet/in.h" sockaddr_in
  (sin_family U16)
  (sin_port U16)
  (sin_addr U32)
  (sin_zero (Array U8 8)))

;; Access fields with dot notation
(fn make-sockaddr ((arena Arena) (port (Int 1 .. 65535)))
  (let ((addr (arena-alloc arena (sizeof sockaddr_in))))
    (set! addr.sin_family 2)       ; AF_INET
    (set! addr.sin_port (htons port))
    (set! addr.sin_addr 0)         ; INADDR_ANY
    addr))
```

### 10.3 C Inline Escape Hatch

For C expressions SLOP doesn't model yet:

```
(c-inline "INADDR_ANY")                    ; C constant
(c-inline "sizeof(struct sockaddr_in)")    ; Complex sizeof
(c-inline "((struct foo){.x = 1})")        ; Compound literal
```

### 10.4 Build Integration

Link required libraries when building:

```bash
slop build app.slop -o app -lssl -lcrypto -lcurl -lpq
```
