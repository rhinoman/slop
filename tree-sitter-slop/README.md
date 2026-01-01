# tree-sitter-slop

Tree-sitter grammar for SLOP (Symbolic LLM-Optimized Programming).

## Installation

### Neovim (with nvim-treesitter)

Add to your nvim-treesitter config:

```lua
local parser_config = require("nvim-treesitter.parsers").get_parser_configs()
parser_config.slop = {
  install_info = {
    url = "/path/to/slop/tree-sitter-slop",
    files = {"src/parser.c"},
  },
  filetype = "slop",
}
```

Then run `:TSInstall slop`

### Helix

Add to `~/.config/helix/languages.toml`:

```toml
[[language]]
name = "slop"
scope = "source.slop"
injection-regex = "slop"
file-types = ["slop"]
roots = []
comment-token = ";"
indent = { tab-width = 2, unit = "  " }

[[grammar]]
name = "slop"
source = { path = "/path/to/slop/tree-sitter-slop" }
```

Then run `hx --grammar fetch` and `hx --grammar build`

### Emacs (with tree-sitter)

```elisp
(add-to-list 'treesit-language-source-alist
  '(slop . ("/path/to/slop/tree-sitter-slop")))
(treesit-install-language-grammar 'slop)
```

## Development

```bash
# Install dependencies
npm install

# Generate parser
npm run generate

# Run tests
npm run test

# Parse a file
npm run parse -- /path/to/file.slop
```

## Highlighting

The grammar includes queries for:

- **highlights.scm** - Syntax highlighting
- **indents.scm** - Automatic indentation

### Highlight Groups

| Node | Highlight Group |
|------|----------------|
| `special_form` | `@keyword` |
| `annotation` | `@attribute` |
| `type_name` | `@type` |
| `identifier` | `@variable` |
| `function` (in fn def) | `@function` |
| `builtin_op` | `@operator` |
| `number` | `@number` |
| `string` | `@string` |
| `comment` | `@comment` |
| `quoted_symbol` | `@constant` |
| `boolean`, `nil` | `@constant.builtin` |
| `keyword` | `@property` |

## SLOP Syntax Overview

```slop
; Comments start with semicolon

; Module definition
(module my-module
  (export (my-func 1)))

; Type definitions
(type Age (Int 0 .. 150))
(type User (record (name String) (age Age)))
(type Status (enum active inactive))

; Function with contracts
(fn greet ((name String))
  (@intent "Greet a user by name")
  (@spec ((String) -> String))
  (@pre (!= name ""))
  (string-concat "Hello, " name))

; Holes for LLM generation
(fn validate ((user User))
  (hole Bool "Check if user is valid"
    :complexity tier-1
    :required (user)))
```
