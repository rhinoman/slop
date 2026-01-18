#include "slop_runtime.h"
#include "slop_mylib.h"

mylib_Config mylib_create_config(int64_t timeout, int64_t retries, uint8_t enabled) {
    return (mylib_Config){timeout, retries, enabled};
}

int64_t mylib_config_get_timeout(mylib_Config cfg) {
    return cfg.timeout;
}

int64_t mylib_config_get_retries(mylib_Config cfg) {
    return cfg.retries;
}

uint8_t mylib_config_is_enabled(mylib_Config cfg) {
    return cfg.enabled;
}

int64_t mylib_add(int64_t a, int64_t b) {
    return (a + b);
}

int64_t mylib_multiply(int64_t a, int64_t b) {
    return (a * b);
}

int64_t mylib_status_code(mylib_Status s) {
    if ((s == mylib_Status_ok)) {
        return 0;
    } else if ((s == mylib_Status_error)) {
        return 1;
    } else if ((s == mylib_Status_timeout)) {
        return 2;
    } else {
        return -1;
    }
}

mylib_Config mylib_parse_config(char* timeout_str, char* retries_str) {
    return (mylib_Config){30, 3, 1};
}

uint8_t mylib_validate_timeout(int64_t timeout) {
    return ((timeout >= 0) && (timeout <= 3600));
}

uint8_t mylib_validate_retries(int64_t retries) {
    return ((retries >= 0) && (retries <= 10));
}

