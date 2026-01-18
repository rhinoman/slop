#ifndef SLOP_mylib_H
#define SLOP_mylib_H

#include "slop_runtime.h"
#include <stdint.h>
#include <stdbool.h>

typedef struct mylib_Config mylib_Config;

typedef enum {
    mylib_Status_ok,
    mylib_Status_error,
    mylib_Status_timeout
} mylib_Status;

struct mylib_Config {
    int64_t timeout;
    int64_t retries;
    uint8_t enabled;
};
typedef struct mylib_Config mylib_Config;

mylib_Config mylib_create_config(int64_t timeout, int64_t retries, uint8_t enabled);
int64_t mylib_config_get_timeout(mylib_Config cfg);
int64_t mylib_config_get_retries(mylib_Config cfg);
uint8_t mylib_config_is_enabled(mylib_Config cfg);
int64_t mylib_add(int64_t a, int64_t b);
int64_t mylib_multiply(int64_t a, int64_t b);
int64_t mylib_status_code(mylib_Status s);
mylib_Config mylib_parse_config(char* timeout_str, char* retries_str);
uint8_t mylib_validate_timeout(int64_t timeout);
uint8_t mylib_validate_retries(int64_t retries);

/* Function name aliases for C interop */
#define mylib_add_numbers mylib_add
#define mylib_multiply_numbers mylib_multiply
#define mylib_get_status_code mylib_status_code


#endif
