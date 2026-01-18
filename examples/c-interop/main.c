/**
 * C Interop Example - Main Program
 *
 * This program demonstrates calling a SLOP library from C code
 * using the :c-name feature for clean C function names.
 *
 * Build with:
 *   ./build.sh
 *
 * Or manually:
 *   uv run slop build mylib.slop --library static -o libmylib
 *   cc -o main main.c -L. -lmylib -I../../src/slop/runtime
 */

#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>

/* Include the generated module header (has type definitions) */
#include "slop_mylib.h"

int main(void) {
    printf("=== SLOP C Interop Example ===\n\n");

    /* Test basic math functions */
    printf("Testing math functions:\n");
    int64_t sum = mylib_add(10, 20);
    printf("  mylib_add(10, 20) = %ld\n", (long)sum);

    int64_t product = mylib_multiply(6, 7);
    printf("  mylib_multiply(6, 7) = %ld\n", (long)product);

    /* Test config creation and accessors */
    printf("\nTesting Config type:\n");
    mylib_Config cfg = mylib_create_config(60, 5, true);
    printf("  Created config with timeout=%ld, retries=%ld, enabled=%s\n",
           (long)mylib_config_get_timeout(cfg),
           (long)mylib_config_get_retries(cfg),
           mylib_config_is_enabled(cfg) ? "true" : "false");

    /* Test config parsing */
    printf("\nTesting config parsing:\n");
    mylib_Config parsed = mylib_parse_config("30", "3");
    printf("  Parsed config: timeout=%ld, retries=%ld\n",
           (long)mylib_config_get_timeout(parsed),
           (long)mylib_config_get_retries(parsed));

    /* Test status enum and code conversion */
    printf("\nTesting Status enum:\n");
    int64_t ok_code = mylib_status_code(mylib_Status_ok);
    int64_t error_code = mylib_status_code(mylib_Status_error);
    int64_t timeout_code = mylib_status_code(mylib_Status_timeout);

    printf("  Status ok code: %ld\n", (long)ok_code);
    printf("  Status error code: %ld\n", (long)error_code);
    printf("  Status timeout code: %ld\n", (long)timeout_code);

    printf("\n=== All tests passed! ===\n");
    return 0;
}
