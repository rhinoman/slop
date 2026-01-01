; SLOP syntax highlighting queries for tree-sitter

; Comments
(comment) @comment

; Strings
(string) @string

; Numbers
(number) @number

; Boolean and nil
(boolean) @constant.builtin
(nil) @constant.builtin

; Quoted symbols (enum values like 'ok, 'error)
(quoted_symbol) @constant

; Keywords (:complexity, :required, etc.)
(keyword) @property

; Type names (PascalCase)
(type_name) @type

; Annotations (@intent, @spec, @pre, @post)
(annotation) @attribute

; Range dots
(range_dots) @operator

; Special forms - first identifier in a list
; Function definition
(list
  .
  (identifier) @keyword
  (#any-of? @keyword
    "fn" "sig" "impl" "module" "export" "import"
    "type" "const" "record" "enum" "union"
    "structure" "logic"
    "let" "let*" "mut"
    "if" "cond" "match" "when"
    "while" "for" "for-each" "do"
    "hole" "ffi" "ffi-struct"))

; Built-in operators as first element
(list
  .
  (identifier) @operator
  (#any-of? @operator
    ; Arithmetic
    "+" "-" "*" "/" "%"
    ; Bitwise
    "&" "|" "^" "<<" ">>"
    ; Comparison
    "==" "!=" "<" "<=" ">" ">="
    ; Boolean
    "and" "or" "not"
    ; Data access
    "." "@" "put" "set!" "deref"
    ; Result/Option
    "ok" "error" "try" "?" "is-ok" "unwrap" "some" "none"
    ; Control
    "break" "continue" "return"
    ; Type/Memory
    "cast" "sizeof" "addr"
    ; Data construction
    "array" "list" "map" "record-new" "union-new"
    ; Arena
    "arena-new" "arena-alloc" "arena-free" "with-arena"
    ; String operations
    "string-new" "string-len" "string-concat" "string-eq" "string-slice" "int-to-string"
    ; List operations
    "list-new" "list-push" "list-get" "list-len"
    ; Map operations
    "map-new" "map-put" "map-get" "map-has"
    ; Time
    "now-ms" "sleep-ms"
    ; Console I/O
    "print" "println"))

; Function name (second element after 'fn')
(list
  .
  (identifier) @_fn
  (#eq? @_fn "fn")
  .
  (identifier) @function)

; Type name in type definition
(list
  .
  (identifier) @_type
  (#eq? @_type "type")
  .
  (type_name) @type.definition)

; Constant name in const definition
(list
  .
  (identifier) @_const
  (#eq? @_const "const")
  .
  (identifier) @constant)

; Module name
(list
  .
  (identifier) @_mod
  (#eq? @_mod "module")
  .
  (identifier) @namespace)

; Generic identifiers (variables, function calls)
(identifier) @variable

; Brackets
"(" @punctuation.bracket
")" @punctuation.bracket
"{" @punctuation.bracket
"}" @punctuation.bracket

; Infix operators
(infix_binary
  ["and" "or"] @keyword.operator)

(infix_binary
  ["==" "!=" "<" "<=" ">" ">=" "+" "-" "*" "/" "%"] @operator)

(infix_unary
  "not" @keyword.operator)

(infix_unary
  "-" @operator)
