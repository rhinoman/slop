# SLOP Quick Reference

Reference guide for LLM code generation. See LANGUAGE.md for the full specification.

## Common Mistakes

These functions/patterns do NOT exist in SLOP - use the alternatives:

| Don't Use | Use Instead |
|-----------|-------------|
| `print-int n` | `(println (int-to-string arena n))` |
| `print-float n` | `(println (float-to-string arena n))` |
| `(println enum-value)` | Use `match` to print different strings |
| `arena` outside with-arena | Wrap code in `(with-arena size ...)` |
| `(block ...)` | `(do ...)` for sequencing |
| `(begin ...)` | `(do ...)` for sequencing |
| `(progn ...)` | `(do ...)` for sequencing |
| `read-line` | FFI to stdio.h |
| `sqrt`, `sin`, `cos` | FFI to math.h |
| `strlen s` | `(string-len s)` |
| `malloc` | `(arena-alloc arena size)` |
| `arr.length` | Arrays are fixed size - use declared size |
| `list.length` | `(list-len list)` |
| `list-append` | `(list-push list elem)` |
| `list-add` | `(list-push list elem)` |
| `map-set` | `(map-put map key val)` |
| `hash-get` | `(map-get map key)` |
| `parse-int` | Implement manually or FFI |
| `json-parse` | Implement manually or FFI |
| `string-find` | Iterate with for-each |
| Definitions outside `(module ...)` | All `(type)`, `(fn)`, `(const)` go inside the module form |

## Module Structure

All definitions must be inside the module form:

```lisp
(module my-module
  (export public-fn)
  (import other-module (helper))

  (type MyType (Int 0 ..))

  (fn public-fn (...)
    ...)

  (fn private-fn (...)
    ...))  ; <-- closing paren wraps entire module
```

**Wrong** (definitions outside module):
```lisp
(module my-module
  (export public-fn))

(fn public-fn ...)  ; ERROR: outside module form
```

## Built-in Functions

### I/O (Strings Only)

```lisp
(print str)              ; print string, no newline
(println str)            ; print string with newline
```

### String Operations

```lisp
(int-to-string arena n)   ; Int -> String (requires arena)
(string-len s)            ; String -> Int
(string-concat arena a b) ; String String -> String
(string-eq a b)           ; String String -> Bool
(string-slice s start end) ; Substring
(string-split arena s delim) ; Split by delimiter (single char)
```

### Memory

```lisp
(arena-new size)         ; create arena
(arena-alloc arena size) ; allocate from arena
(arena-free arena)       ; free arena
(with-arena size body)   ; scoped arena (implicit 'arena' var)
(sizeof Type)            ; size of type in bytes
(addr expr)              ; address-of (&expr)
(deref ptr)              ; dereference pointer (*ptr)
```

### Data Construction

```lisp
(ok val)                 ; Result success
(error 'variant)         ; Result error (QUOTE the variant!)
(some val)               ; Option some
(none)                   ; Option none
(record-new Type (field1 val1) ...)  ; create record
(list elem1 elem2 ...)   ; create list literal
(array elem1 elem2 ...)  ; create array literal
```

### Collections

```lisp
(list-new arena)         ; create empty list
(list-push list elem)    ; append element to list
(list-get list idx)      ; get element at index -> Option
(list-len list)          ; get list length

(map-new arena)          ; create empty map
(map-put map key val)    ; insert/update key-value pair
(map-get map key)        ; get value -> Option
(map-has map key)        ; check if key exists -> Bool
```

### Field Access

```lisp
(. record field)         ; field access (auto -> for pointers)
(set! record field val)  ; field mutation
(@ arr idx)              ; array indexing
```

## Loop Patterns

### Find Index Matching Predicate

```lisp
(let ((result -1))
  (for (i 0 SIZE)
    (when PREDICATE
      (do
        (set! result i)
        (break))))
  result)
```

### Count Matching Elements

```lisp
(let ((count 0))
  (for (i 0 SIZE)
    (when PREDICATE
      (set! count (+ count 1))))
  count)
```

### Sum Values

```lisp
(let ((total 0))
  (for (i 0 SIZE)
    (set! total (+ total ACCESSOR)))
  total)
```

### Find Empty Slot

```lisp
(let ((idx -1))
  (for (i 0 SIZE)
    (when (== (. (@ storage i) id) 0)
      (do
        (set! idx i)
        (break))))
  idx)
```

### Array Shift Delete

```lisp
(for (i idx (- SIZE 1))
  (set! (@ arr i) (@ arr (+ i 1))))
```

## Match Patterns

### Simple Enums (No Bindings)

Simple enums have no data - use bare variant names:

```lisp
(match status
  (Pending (println "waiting"))
  (Active (println "running"))
  (Done (println "finished")))
```

### Tagged Unions (With Bindings)

Result and Option carry data - bind with parens:

```lisp
(match result
  ((ok val) (use val))
  ((error e) (handle e)))

(match option
  ((some x) (use x))
  ((none) (handle-none)))
```

### Error Returns

IMPORTANT: Quote the error variant!

```lisp
(if (< fd 0)
  (error 'file-not-found)    ; CORRECT: quoted
  (ok data))

; WRONG: (error file-not-found) - unquoted is undefined variable
```

## Arena Allocation Pattern

```lisp
(fn handle-request ((req (Ptr Request)))
  (@intent "Process incoming request")
  (@spec (((Ptr Request)) -> (Ptr Response)))

  (with-arena 4096
    (let ((user (parse-user arena req))
          (result (process arena user)))
      (send-response result))))
;; Arena freed automatically at end
```

For functions that allocate:

```lisp
(fn create-user ((arena Arena) (name String) (email String))
  (@intent "Create a new user")
  (@spec ((Arena String String) -> (Ptr User)))
  (@alloc arena)

  (let ((user (cast (Ptr User) (arena-alloc arena (sizeof User)))))
    (set! user name name)
    (set! user email email)
    user))
```

## Result Pattern

```lisp
(fn read-file ((arena Arena) (path String))
  (@intent "Read file contents")
  (@spec ((Arena String) -> (Result (Ptr Bytes) IoError)))
  (@alloc arena)

  (let ((fd (open path)))
    (if (< fd 0)
      (error 'file-not-found)
      (let ((data (read-all arena fd)))
        (close fd)
        (ok data)))))
```

### Early Return with ?

```lisp
(fn process-all ((arena Arena) (paths (List String)))
  (@spec ((Arena (List String)) -> (Result (List Data) Error)))

  (let ((results (list-new arena)))
    (for-each (path paths)
      (let ((data (? (read-file arena path))))  ; returns early on error
        (list-push results data)))
    (ok results)))
```
