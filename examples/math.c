#include "slop_runtime.h"

#ifndef SLOP_math_H
#define SLOP_math_H

#include "slop_runtime.h"
#include <stdint.h>
#include <stdbool.h>

typedef int64_t math_Positive;

int64_t math_divide(int64_t numerator, math_Positive denominator);
math_Positive math_abs_positive(math_Positive x);


#endif

int64_t math_divide(int64_t numerator, math_Positive denominator) {
    return (numerator / denominator);
}

math_Positive math_abs_positive(math_Positive x) {
    return x;
}

