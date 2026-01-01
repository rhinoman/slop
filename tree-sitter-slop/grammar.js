/**
 * Tree-sitter grammar for SLOP (Symbolic LLM-Optimized Programming)
 *
 * SLOP uses S-expression syntax with:
 * - Modules, functions, and type definitions
 * - Contract annotations (@intent, @spec, @pre, @post)
 * - Range types (Int 0 .. 100)
 * - Holes for LLM generation
 * - Infix notation in contracts: (@pre {x > 0 and x < 100})
 */

module.exports = grammar({
  name: 'slop',

  extras: $ => [
    /\s/,
    $.comment,
  ],

  // Precedence for infix operators (higher = binds tighter)
  precedences: $ => [
    ['unary', 'multiplicative', 'additive', 'comparison', 'and', 'or'],
  ],

  // No conflicts needed - prefix calls explicitly require identifier

  rules: {
    source_file: $ => repeat($._form),

    _form: $ => choice(
      $.list,
      $.infix_expr,
      $._atom
    ),

    // S-expression list
    list: $ => seq(
      '(',
      repeat($._form),
      ')'
    ),

    // Infix expression (contracts only): {x > 0 and y < 10}
    infix_expr: $ => seq(
      '{',
      $._infix_expr,
      '}'
    ),

    _infix_expr: $ => choice(
      $.infix_binary,
      $.infix_unary,
      $.infix_group,
      $._infix_atom
    ),

    // Binary infix operations
    infix_binary: $ => choice(
      // or (lowest precedence)
      prec.left('or', seq($._infix_expr, 'or', $._infix_expr)),
      // and
      prec.left('and', seq($._infix_expr, 'and', $._infix_expr)),
      // comparison
      prec.left('comparison', seq($._infix_expr, choice('==', '!=', '<', '<=', '>', '>='), $._infix_expr)),
      // additive
      prec.left('additive', seq($._infix_expr, choice('+', '-'), $._infix_expr)),
      // multiplicative (highest binary precedence)
      prec.left('multiplicative', seq($._infix_expr, choice('*', '/', '%'), $._infix_expr)),
    ),

    // Unary operations
    infix_unary: $ => choice(
      prec.right('unary', seq('not', $._infix_expr)),
      prec.right('unary', seq('-', $._infix_expr)),
    ),

    // Grouping with parentheses or prefix S-expression call
    infix_group: $ => seq(
      '(',
      choice(
        $._infix_expr,                      // grouping: (a + b)
        seq($.identifier, repeat1($._form)) // prefix call: (len arr) or (. obj field)
      ),
      ')'
    ),

    // Atoms usable in infix expressions
    _infix_atom: $ => choice(
      $.number,
      $.string,
      $.quoted_symbol,
      $.boolean,
      $.nil,
      $.type_name,
      $.identifier
    ),

    // Atoms
    _atom: $ => choice(
      $.number,
      $.string,
      $.quoted_symbol,
      $.boolean,
      $.nil,
      $.annotation,
      $.keyword,
      $.type_name,
      $.identifier,
      $.range_dots
    ),

    // Comments
    comment: $ => token(seq(';', /.*/)),

    // Literals
    number: $ => token(choice(
      // Integer
      /-?[0-9]+/,
      // Float
      /-?[0-9]+\.[0-9]+/,
      // Hex
      /0x[0-9a-fA-F]+/
    )),

    string: $ => token(seq(
      '"',
      repeat(choice(
        /[^"\\]/,
        /\\./
      )),
      '"'
    )),

    // Quoted symbol (enum value)
    quoted_symbol: $ => token(seq(
      "'",
      /[a-z][a-z0-9-]*/
    )),

    // Boolean and nil
    boolean: $ => choice('true', 'false'),
    nil: $ => 'nil',

    // Contract and metadata annotations (@intent, @spec, etc.)
    annotation: $ => token(seq(
      '@',
      /[a-z][a-z-]*/
    )),

    // Keywords (:keyword style, used in hole attributes)
    keyword: $ => token(seq(
      ':',
      /[a-z][a-z0-9-]*/
    )),

    // Type names (PascalCase)
    type_name: $ => /[A-Z][a-zA-Z0-9]*/,

    // Identifiers (kebab-case, includes special forms and operators)
    identifier: $ => /[a-z_+\-*\/%<>=!&|^.@?][a-z0-9_+\-*\/%<>=!&|^.@?]*/,

    // Range dots for type bounds
    range_dots: $ => '..',
  }
});
