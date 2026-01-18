#ifndef MYLIB_PUBLIC_H
#define MYLIB_PUBLIC_H

#include <stdint.h>
#include <stdbool.h>
#include <string.h>

/* String type for SLOP interop */
typedef struct { uint8_t* data; size_t len; } slop_string;

static inline slop_string slop_str(const char* s) {
    return (slop_string){ .data = (uint8_t*)s, .len = strlen(s) };
}

/* Types used by public API */
/* Forward decl: Int - see module header for definition */
/* Forward decl: int64_ - see module header for definition */
/* Forward decl: Status - see module header for definition */

/* Public API functions */
int64_t mylib_add(int64_t a, int64_t b);
int64_t mylib_multiply(int64_t a, int64_t b);
int64_t mylib_status_code(mylib_Status s);

#endif
