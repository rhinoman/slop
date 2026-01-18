# SLOP Standard Library

The standard library provides common functionality for SLOP programs. All modules use type-safe wrappers around C functions via FFI.

## Modules

| Module | Import | Description |
|--------|--------|-------------|
| `strlib` | `(import strlib (...))` | String manipulation and parsing |
| `io` | `(import file (...))` | File I/O operations |
| `math` | `(import mathlib (...))` | Mathematical functions |
| `os` | `(import env (...))` | Environment variables |
| `thread` | `(import thread (...))` | Concurrency primitives |

## Usage

Include the module path when building:

```bash
slop build myprogram.slop -I lib/std/strlib -I lib/std/io
```

Or configure in `slop.toml`:

```toml
[build]
include = ["lib/std/strlib", "lib/std/io", "lib/std/thread"]

[build.link]
libraries = ["pthread"]  # Required for thread module
```

---

## strlib - String Manipulation

```lisp
(import strlib (contains starts-with parse-int trim))
```

### Character Classification

| Function | Signature | Description |
|----------|-----------|-------------|
| `is-alpha` | `(U8) -> Bool` | Alphabetic character |
| `is-digit` | `(U8) -> Bool` | Decimal digit |
| `is-alnum` | `(U8) -> Bool` | Alphanumeric |
| `is-space` | `(U8) -> Bool` | Whitespace |
| `is-upper` | `(U8) -> Bool` | Uppercase letter |
| `is-lower` | `(U8) -> Bool` | Lowercase letter |
| `is-ascii` | `(U8) -> Bool` | ASCII character (0-127) |
| `is-printable` | `(U8) -> Bool` | Printable character |
| `is-control` | `(U8) -> Bool` | Control character |

### Character Conversion

| Function | Signature | Description |
|----------|-----------|-------------|
| `char-to-upper` | `(U8) -> U8` | Convert to uppercase |
| `char-to-lower` | `(U8) -> U8` | Convert to lowercase |
| `char-at` | `(String (Int 0 ..)) -> U8` | Get character at index |

### String Search

| Function | Signature | Description |
|----------|-----------|-------------|
| `index-of` | `(String String) -> (Option (Int 0 ..))` | Find first occurrence |
| `last-index-of` | `(String String) -> (Option (Int 0 ..))` | Find last occurrence |
| `contains` | `(String String) -> Bool` | Check if substring exists |
| `starts-with` | `(String String) -> Bool` | Check prefix |
| `ends-with` | `(String String) -> Bool` | Check suffix |
| `count-occurrences` | `(String String) -> (Int 0 ..)` | Count substring occurrences |

### String Transformation

| Function | Signature | Description |
|----------|-----------|-------------|
| `trim` | `(Arena String) -> String` | Remove leading/trailing whitespace |
| `trim-start` | `(Arena String) -> String` | Remove leading whitespace |
| `trim-end` | `(Arena String) -> String` | Remove trailing whitespace |
| `pad-start` | `(Arena String (Int 0 ..) U8) -> String` | Pad to length from start |
| `pad-end` | `(Arena String (Int 0 ..) U8) -> String` | Pad to length from end |
| `reverse` | `(Arena String) -> String` | Reverse string |
| `repeat` | `(Arena String (Int 0 ..)) -> String` | Repeat string n times |
| `substring` | `(Arena String (Int 0 ..) (Int 0 ..)) -> String` | Extract substring |
| `to-upper` | `(Arena String) -> String` | Convert to uppercase |
| `to-lower` | `(Arena String) -> String` | Convert to lowercase |
| `to-title` | `(Arena String) -> String` | Convert to title case |
| `capitalize` | `(Arena String) -> String` | Capitalize first letter |
| `replace` | `(Arena String String String) -> String` | Replace first occurrence |
| `replace-all` | `(Arena String String String) -> String` | Replace all occurrences |
| `join` | `(Arena (List String) String) -> String` | Join strings with delimiter |

### String Comparison

| Function | Signature | Description |
|----------|-----------|-------------|
| `compare` | `(String String) -> Int` | Compare strings (like strcmp) |
| `compare-ignore-case` | `(String String) -> Int` | Case-insensitive compare |

### Parsing

| Function | Signature | Description |
|----------|-----------|-------------|
| `parse-int` | `(String) -> (Result Int ParseError)` | Parse integer from string |
| `parse-float` | `(String) -> (Result Float ParseError)` | Parse float from string |
| `float-to-string` | `(Arena Float) -> String` | Convert float to string |
| `cstring-to-string` | `((Ptr U8)) -> String` | Convert C string to SLOP string |

### Memory Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `fill-bytes` | `((Ptr U8) U8 (Int 0 ..)) -> Unit` | Fill memory with byte value |

### Types

```lisp
(type ParseError (enum empty-string invalid-format overflow underflow))
(type AsciiChar (Int 0 .. 127))
```

---

## file - File I/O

```lisp
(import file (file-open file-read-all file-write file-close FileMode FileError))
```

### Types

```lisp
(type FileMode (enum
  read           ;; "r"  - read only, file must exist
  write          ;; "w"  - write only, truncates or creates
  append         ;; "a"  - append only, creates if missing
  read-write     ;; "r+" - read/write, file must exist
  write-read     ;; "w+" - read/write, truncates or creates
  append-read))  ;; "a+" - read/append, creates if missing

(type FileError (enum
  not-found      ;; File does not exist
  permission     ;; Permission denied
  io-error       ;; General I/O error
  eof            ;; End of file reached
  invalid-mode)) ;; Invalid file mode
```

### File Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `file-open` | `(String FileMode) -> (Result (Ptr File) FileError)` | Open file |
| `file-close` | `((Ptr File)) -> Unit` | Close file handle |
| `file-read` | `((Ptr File) (Ptr U8) (Int 1 ..)) -> (Result (Int 0 ..) FileError)` | Read bytes |
| `file-read-line` | `(Arena (Ptr File)) -> (Result String FileError)` | Read line |
| `file-read-all` | `(Arena (Ptr File)) -> (Result String FileError)` | Read entire file |
| `file-write` | `((Ptr File) String) -> (Result (Int 0 ..) FileError)` | Write string |
| `file-write-line` | `((Ptr File) String) -> (Result (Int 0 ..) FileError)` | Write line with newline |
| `file-flush` | `((Ptr File)) -> (Result Unit FileError)` | Flush buffer |
| `file-seek` | `((Ptr File) I64 Int) -> (Result Unit FileError)` | Seek position |
| `file-tell` | `((Ptr File)) -> (Result I64 FileError)` | Get current position |
| `file-exists` | `(String) -> Bool` | Check if file exists |
| `file-size` | `(String) -> (Result I64 FileError)` | Get file size |

### Example

```lisp
(with-arena 4096
  (match (file-open "data.txt" 'read)
    ((ok f)
      (match (file-read-all arena f)
        ((ok contents) (println contents))
        ((error e) (println "Read error")))
      (file-close f))
    ((error e) (println "Open error"))))
```

---

## mathlib - Mathematical Functions

```lisp
(import mathlib (PI sqrt sin cos pow clamp))
```

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `PI` | 3.14159... | Pi |
| `E` | 2.71828... | Euler's number |
| `TAU` | 6.28318... | 2 * Pi |

### Integer Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `abs` | `(Int) -> (Int 0 ..)` | Absolute value |
| `sign` | `(Int) -> Int` | Sign (-1, 0, or 1) |
| `clamp` | `(Int Int Int) -> Int` | Clamp to range |
| `gcd` | `(Int Int) -> Int` | Greatest common divisor |
| `lcm` | `(Int Int) -> Int` | Least common multiple |

### Float Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `abs-float` | `(Float) -> Float` | Absolute value |
| `floor` | `(Float) -> Float` | Round down |
| `ceil` | `(Float) -> Float` | Round up |
| `round` | `(Float) -> Float` | Round to nearest |
| `trunc` | `(Float) -> Float` | Truncate toward zero |
| `frac` | `(Float) -> Float` | Fractional part |
| `clamp-float` | `(Float Float Float) -> Float` | Clamp to range |
| `lerp` | `(Float Float Float) -> Float` | Linear interpolation |

### Power and Roots

| Function | Signature | Description |
|----------|-----------|-------------|
| `sqrt` | `(Float) -> Float` | Square root |
| `cbrt` | `(Float) -> Float` | Cube root |
| `pow` | `(Float Float) -> Float` | Power |
| `exp` | `(Float) -> Float` | e^x |
| `exp2` | `(Float) -> Float` | 2^x |
| `log` | `(Float) -> Float` | Natural logarithm |
| `log10` | `(Float) -> Float` | Base-10 logarithm |
| `log2` | `(Float) -> Float` | Base-2 logarithm |

### Trigonometry

| Function | Signature | Description |
|----------|-----------|-------------|
| `sin` | `(Float) -> Float` | Sine |
| `cos` | `(Float) -> Float` | Cosine |
| `tan` | `(Float) -> Float` | Tangent |
| `asin` | `(Float) -> Float` | Arc sine |
| `acos` | `(Float) -> Float` | Arc cosine |
| `atan` | `(Float) -> Float` | Arc tangent |
| `atan2` | `(Float Float) -> Float` | Two-argument arc tangent |
| `degrees-to-radians` | `(Float) -> Float` | Convert degrees to radians |
| `radians-to-degrees` | `(Float) -> Float` | Convert radians to degrees |

### Hyperbolic

| Function | Signature | Description |
|----------|-----------|-------------|
| `sinh` | `(Float) -> Float` | Hyperbolic sine |
| `cosh` | `(Float) -> Float` | Hyperbolic cosine |
| `tanh` | `(Float) -> Float` | Hyperbolic tangent |

### Utilities

| Function | Signature | Description |
|----------|-----------|-------------|
| `is-nan` | `(Float) -> Bool` | Check if NaN |
| `is-inf` | `(Float) -> Bool` | Check if infinite |
| `is-finite` | `(Float) -> Bool` | Check if finite |
| `copysign` | `(Float Float) -> Float` | Copy sign from second to first |

---

## env - Environment Variables

```lisp
(import env (get-env set-env unset-env EnvError))
```

### Types

```lisp
(type EnvError (enum
  invalid-name   ;; Name is empty or contains '='
  system-error)) ;; setenv/unsetenv returned error
```

### Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `get-env` | `(String) -> (Option String)` | Get environment variable |
| `set-env` | `(String String) -> (Result Bool EnvError)` | Set environment variable |
| `unset-env` | `(String) -> (Result Bool EnvError)` | Remove environment variable |

### Example

```lisp
(match (get-env "HOME")
  ((some home) (println home))
  (none (println "HOME not set")))

(set-env "MY_VAR" "my_value")
```

---

## thread - Concurrency

```lisp
(import thread (chan chan-buffered send recv spawn join ChanError))
```

**Requires:** Link with pthread (`libraries = ["pthread"]` in slop.toml)

### Types

```lisp
(type ChanError (enum
  closed           ;; Channel has been closed
  would-block      ;; try-recv on empty channel
  send-on-closed)) ;; Attempted send on closed channel

(type ThreadWithChan (record
  (func (Ptr Void))
  (chan (Ptr (Chan Int)))
  (id U64)
  (result Int)
  (done Bool)))
```

### Channel Creation

| Function | Signature | Description |
|----------|-----------|-------------|
| `chan` | `(Arena) -> (Ptr (Chan T))` | Create unbuffered channel |
| `chan-buffered` | `(Arena (Int 1 ..)) -> (Ptr (Chan T))` | Create buffered channel |
| `chan-close` | `((Ptr (Chan T))) -> Unit` | Close channel |

### Channel Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `send` | `((Ptr (Chan T)) T) -> (Result Unit ChanError)` | Blocking send |
| `recv` | `((Ptr (Chan T))) -> (Result T ChanError)` | Blocking receive |
| `try-recv` | `((Ptr (Chan T))) -> (Result T ChanError)` | Non-blocking receive |

### Thread Operations

| Function | Signature | Description |
|----------|-----------|-------------|
| `spawn` | `(Arena (Fn () T)) -> (Ptr (Thread T))` | Spawn thread |
| `spawn-with-chan` | `(Arena (Fn ((Ptr (Chan T))) T) (Ptr (Chan T))) -> (Ptr ThreadWithChan)` | Spawn thread with channel |
| `join` | `((Ptr (Thread T))) -> T` | Wait for thread and get result |

### Example: Worker Pool

```lisp
(with-arena 8192
  ;; Create buffered channel for work items
  (let ((work-ch (chan-buffered arena 10)))

    ;; Spawn worker threads
    (for (i 0 4)
      (spawn-with-chan arena worker-fn work-ch))

    ;; Send work items
    (for (i 0 100)
      (send work-ch i))

    ;; Close channel to signal workers to exit
    (chan-close work-ch)))
```

See `examples/http-server-threaded/` for a complete multi-threaded server example.

---

## Note on Builtins

Some common operations are **builtins** and do not require imports:

- `string-len`, `string-concat`, `string-eq`, `string-new`, `string-slice`, `string-split`
- `int-to-string`
- `list-len`, `list-get`, `list-push`, `list-new`
- `arena-new`, `arena-alloc`, `arena-free`
- `println`

See `slop ref stdlib` for the complete builtin reference.
