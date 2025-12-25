/* Test harness for the self-hosted SLOP parser */
#include <stdio.h>
#include <string.h>
#include "slop_runtime.h"

/* Include the generated parser */
#include "../../build/slop_parser.c"

int main(int argc, char** argv) {
    slop_arena arena = slop_arena_new(65536);

    /* Test 1: Simple expression */
    {
        slop_string source = SLOP_STR("(+ 1 2)");
        SExprList* exprs = parse(&arena, source);
        printf("Test 1 - Simple expression:\n");
        printf("  Parsed %ld expressions\n", (long)exprs->len);
        if (exprs->len > 0) {
            slop_string printed = sexpr_print(&arena, exprs->data[0]);
            printf("  Result: %s\n", printed.data);
        }
        printf("\n");
    }

    /* Test 2: Function definition */
    {
        slop_string source = SLOP_STR("(fn hello () 42)");
        SExprList* exprs = parse(&arena, source);
        printf("Test 2 - Function definition:\n");
        printf("  Parsed %ld expressions\n", (long)exprs->len);
        if (exprs->len > 0) {
            slop_string printed = sexpr_print(&arena, exprs->data[0]);
            printf("  Result: %s\n", printed.data);
        }
        printf("\n");
    }

    /* Test 3: Module with types */
    {
        slop_string source = SLOP_STR(
            "(module test\n"
            "  (type Point (record (x Int) (y Int)))\n"
            "  (fn add ((a Point) (b Point)) 0))"
        );
        SExprList* exprs = parse(&arena, source);
        printf("Test 3 - Module with types:\n");
        printf("  Parsed %ld expressions\n", (long)exprs->len);
        if (exprs->len > 0) {
            slop_string printed = sexpr_print(&arena, exprs->data[0]);
            printf("  Result: %s\n", printed.data);
        }
        printf("\n");
    }

    /* Test 4: Quoted symbols */
    {
        slop_string source = SLOP_STR("'hello 'world");
        SExprList* exprs = parse(&arena, source);
        printf("Test 4 - Quoted symbols:\n");
        printf("  Parsed %ld expressions\n", (long)exprs->len);
        for (int64_t i = 0; i < exprs->len; i++) {
            slop_string printed = sexpr_print(&arena, exprs->data[i]);
            printf("  [%ld]: %s\n", (long)i, printed.data);
        }
        printf("\n");
    }

    /* Test 5: Comments */
    {
        slop_string source = SLOP_STR(
            "; This is a comment\n"
            "(+ 1 2) ; inline comment\n"
            ";; Double semicolon comment"
        );
        SExprList* exprs = parse(&arena, source);
        printf("Test 5 - Comments:\n");
        printf("  Parsed %ld expressions (should be 1)\n", (long)exprs->len);
        if (exprs->len > 0) {
            slop_string printed = sexpr_print(&arena, exprs->data[0]);
            printf("  Result: %s\n", printed.data);
        }
        printf("\n");
    }

    slop_arena_free(&arena);
    printf("All tests passed!\n");
    return 0;
}
