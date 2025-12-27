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

; Keywords (:complexity, :must-use, etc.)
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
    "let" "let*"
    "if" "cond" "match" "when"
    "while" "for" "for-each" "do"
    "hole" "ffi" "ffi-struct"))

; Built-in operators as first element
(list
  .
  (identifier) @operator
  (#any-of? @operator
    "+" "-" "*" "/" "%"
    "&" "|" "^" "<<" ">>"
    "==" "!=" "<" "<=" ">" ">="
    "and" "or" "not"
    "." "@" "put" "set!"
    "ok" "error" "try" "?"
    "break" "continue" "return"
    "cast" "sizeof" "addr"
    "array" "list" "map" "record-new" "union-new"
    "arena-new" "arena-alloc" "arena-free" "with-arena"))

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
