;;; slop-ts-mode.el --- Tree-sitter support for SLOP -*- lexical-binding: t; -*-

;; Copyright (C) 2024

;; Author: SLOP Authors
;; Keywords: languages, lisp, tree-sitter
;; Version: 0.1.0
;; Package-Requires: ((emacs "29.1"))

;;; Commentary:

;; Major mode for editing SLOP (Symbolic LLM-Optimized Programming) files,
;; powered by tree-sitter.

;; Installation:
;;
;; 1. Add tree-sitter-slop to your load path
;; 2. Add to your init.el:
;;
;;    (add-to-list 'treesit-language-source-alist
;;      '(slop . ("/path/to/slop/tree-sitter-slop")))
;;    (treesit-install-language-grammar 'slop)
;;    (require 'slop-ts-mode)

;;; Code:

(require 'treesit)

(defgroup slop nil
  "Support for SLOP."
  :group 'languages)

(defcustom slop-ts-mode-indent-offset 2
  "Number of spaces for each indentation step in `slop-ts-mode'."
  :type 'integer
  :group 'slop)

;; Font-lock settings
(defvar slop-ts-mode--font-lock-settings
  (treesit-font-lock-rules
   :language 'slop
   :feature 'comment
   '((comment) @font-lock-comment-face)

   :language 'slop
   :feature 'string
   '((string) @font-lock-string-face)

   :language 'slop
   :feature 'number
   '((number) @font-lock-number-face)

   :language 'slop
   :feature 'constant
   '((boolean) @font-lock-constant-face
     (nil) @font-lock-constant-face
     (quoted_symbol) @font-lock-constant-face)

   :language 'slop
   :feature 'type
   '((type_name) @font-lock-type-face)

   :language 'slop
   :feature 'property
   '((keyword) @font-lock-property-name-face)

   :language 'slop
   :feature 'annotation
   '((annotation) @font-lock-preprocessor-face)

   :language 'slop
   :feature 'keyword
   '((list
      :anchor
      (identifier) @font-lock-keyword-face
      (:match "^\\(fn\\|sig\\|impl\\|module\\|export\\|import\\|type\\|const\\|record\\|enum\\|union\\|structure\\|logic\\|let\\|let\\*\\|mut\\|if\\|cond\\|match\\|when\\|while\\|for\\|for-each\\|do\\|hole\\|ffi\\|ffi-struct\\)$"
              @font-lock-keyword-face)))

   :language 'slop
   :feature 'operator
   '((list
      :anchor
      (identifier) @font-lock-operator-face
      (:match "^\\(\\+\\|-\\|\\*\\|/\\|%\\|&\\||\\|\\^\\|<<\\|>>\\|==\\|!=\\|<\\|<=\\|>\\|>=\\|and\\|or\\|not\\|\\.\\|@\\|put\\|set!\\|deref\\|ok\\|error\\|try\\|\\?\\|is-ok\\|unwrap\\|some\\|none\\|break\\|continue\\|return\\|cast\\|sizeof\\|addr\\|array\\|list\\|map\\|record-new\\|union-new\\|arena-new\\|arena-alloc\\|arena-free\\|with-arena\\|string-new\\|string-len\\|string-concat\\|string-eq\\|string-slice\\|int-to-string\\|list-new\\|list-push\\|list-get\\|list-len\\|map-new\\|map-put\\|map-get\\|map-has\\|now-ms\\|sleep-ms\\|print\\|println\\)$"
              @font-lock-operator-face))
     (range_dots) @font-lock-operator-face)

   :language 'slop
   :feature 'function
   '((list
      :anchor
      (identifier) @_fn
      (:match "^fn$" @_fn)
      (identifier) @font-lock-function-name-face))

   :language 'slop
   :feature 'definition
   '((list
      :anchor
      (identifier) @_type
      (:match "^type$" @_type)
      (type_name) @font-lock-type-face))

   :language 'slop
   :feature 'variable
   :override t
   '((identifier) @font-lock-variable-name-face)

   :language 'slop
   :feature 'bracket
   '(["(" ")" "{" "}"] @font-lock-bracket-face)

   :language 'slop
   :feature 'infix
   '((infix_binary ["and" "or"] @font-lock-keyword-face)
     (infix_binary ["==" "!=" "<" "<=" ">" ">=" "+" "-" "*" "/" "%"] @font-lock-operator-face)
     (infix_unary "not" @font-lock-keyword-face)
     (infix_unary "-" @font-lock-operator-face)))
  "Tree-sitter font-lock settings for SLOP.")

;; Indentation
(defvar slop-ts-mode--indent-rules
  '((slop
     ((parent-is "source_file") column-0 0)
     ((node-is ")") parent-bol 0)
     ((node-is "}") parent-bol 0)
     ((parent-is "list") parent-bol slop-ts-mode-indent-offset)
     ((parent-is "infix_expr") parent-bol slop-ts-mode-indent-offset)))
  "Tree-sitter indentation rules for SLOP.")

;;;###autoload
(define-derived-mode slop-ts-mode prog-mode "SLOP"
  "Major mode for editing SLOP files, powered by tree-sitter.

\\{slop-ts-mode-map}"
  :group 'slop

  (unless (treesit-ready-p 'slop)
    (error "Tree-sitter grammar for SLOP is not available"))

  (treesit-parser-create 'slop)

  ;; Comments
  (setq-local comment-start "; ")
  (setq-local comment-end "")
  (setq-local comment-start-skip ";+ *")

  ;; Font-lock
  (setq-local treesit-font-lock-settings slop-ts-mode--font-lock-settings)
  (setq-local treesit-font-lock-feature-list
              '((comment string)
                (keyword type annotation)
                (constant number property operator function definition infix)
                (variable bracket)))

  ;; Indentation
  (setq-local treesit-simple-indent-rules slop-ts-mode--indent-rules)

  ;; Navigation (S-expression aware)
  (setq-local treesit-defun-type-regexp "list")

  (treesit-major-mode-setup))

;;;###autoload
(add-to-list 'auto-mode-alist '("\\.slop\\'" . slop-ts-mode))

(provide 'slop-ts-mode)
;;; slop-ts-mode.el ends here
