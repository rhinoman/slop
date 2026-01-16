#include "slop_runtime.h"

#ifndef SLOP_comprehensive_test_H
#define SLOP_comprehensive_test_H

#include "slop_runtime.h"
#include <stdint.h>
#include <stdbool.h>

typedef struct comprehensive_test_Player comprehensive_test_Player;
typedef struct comprehensive_test_Value comprehensive_test_Value;

typedef enum {
    comprehensive_test_GameState_waiting,
    comprehensive_test_GameState_playing,
    comprehensive_test_GameState_paused,
    comprehensive_test_GameState_finished
} comprehensive_test_GameState;

typedef int64_t comprehensive_test_Score;

typedef int64_t comprehensive_test_Scores[10];

struct comprehensive_test_Player {
    slop_string name;
    comprehensive_test_Score score;
    uint8_t active;
};
typedef struct comprehensive_test_Player comprehensive_test_Player;

typedef enum {
    comprehensive_test_Value_number,
    comprehensive_test_Value_text,
    comprehensive_test_Value_nothing
} comprehensive_test_Value_tag;

struct comprehensive_test_Value {
    comprehensive_test_Value_tag tag;
    union {
        int64_t number;
        slop_string text;
    } data;
};
typedef struct comprehensive_test_Value comprehensive_test_Value;

#ifndef SLOP_RESULT_INT_STRING_DEFINED
#define SLOP_RESULT_INT_STRING_DEFINED
typedef struct { bool is_ok; union { int64_t ok; slop_string err; } data; } slop_result_int_string;
#endif

int64_t comprehensive_test_bitwise_ops(int64_t a, int64_t b);
int64_t comprehensive_test_sum_array(int64_t* scores);
int64_t comprehensive_test_count_down(int64_t n);
int64_t comprehensive_test_state_name(comprehensive_test_GameState s);
int64_t comprehensive_test_extract_value(comprehensive_test_Value v);
slop_result_int_string comprehensive_test_divide(int64_t a, int64_t b);
slop_result_int_string comprehensive_test_safe_divide_twice(int64_t a, int64_t b, int64_t c);
int64_t comprehensive_test_classify(int64_t n);
int64_t comprehensive_test_arena_test(void);
int64_t comprehensive_test_sequence_test(int64_t x);
int64_t* comprehensive_test_get_address(int64_t x);
uint8_t comprehensive_test_to_byte(int64_t n);
int64_t comprehensive_test_inline_c(void);
int64_t comprehensive_test_always_positive(int64_t x);
int64_t comprehensive_test_nested_calls(int64_t a, int64_t b);
int64_t comprehensive_test_option_test(slop_option_int opt);
int main(void);


#endif

int64_t comprehensive_test_bitwise_ops(int64_t a, int64_t b) {
    return ((a & b) | (a ^ (b << 2)));
}

int64_t comprehensive_test_sum_array(int64_t* scores) {
    {
        __auto_type total = 0;
        for (int64_t i = 0; i < 10; i++) {
            total = (total + scores[i]);
        }
        return total;
    }
}

int64_t comprehensive_test_count_down(int64_t n) {
    {
        __auto_type count = 0;
        while ((n > 0)) {
            count = (count + 1);
            n = (n - 1);
        }
        return count;
    }
}

int64_t comprehensive_test_state_name(comprehensive_test_GameState s) {
    if ((s == comprehensive_test_GameState_waiting)) {
        return 0;
    } else if ((s == comprehensive_test_GameState_playing)) {
        return 1;
    } else if ((s == comprehensive_test_GameState_paused)) {
        return 2;
    } else {
        return 3;
    }
}

int64_t comprehensive_test_extract_value(comprehensive_test_Value v) {
    __auto_type _match_val = v;
    switch (_match_val.tag) {
        case comprehensive_test_Value_number:
        {
            __auto_type n = _match_val.data.number;
            return n;
        }
        case comprehensive_test_Value_text:
        {
            __auto_type t = _match_val.data.text;
            return 0;
        }
        case comprehensive_test_Value_nothing:
        {
            return -1;
        }
    }
}

slop_result_int_string comprehensive_test_divide(int64_t a, int64_t b) {
    if ((b == 0)) {
        return ((slop_result_int_string){ .is_ok = false, .data.err = SLOP_STR("division by zero") });
    } else {
        return ((slop_result_int_string){ .is_ok = true, .data.ok = (a / b) });
    }
}

slop_result_int_string comprehensive_test_safe_divide_twice(int64_t a, int64_t b, int64_t c) {
    {
        __auto_type first = ({ __auto_type _tmp = comprehensive_test_divide(a, b); if (!_tmp.is_ok) return _tmp; _tmp.data.ok; });
        __auto_type second = ({ __auto_type _tmp = comprehensive_test_divide(first, c); if (!_tmp.is_ok) return _tmp; _tmp.data.ok; });
        return ((slop_result_int_string){ .is_ok = true, .data.ok = second });
    }
}

int64_t comprehensive_test_classify(int64_t n) {
    if ((n < 0)) {
        return -1;
    } else if ((n == 0)) {
        return 0;
    } else {
        return 1;
    }
}

int64_t comprehensive_test_arena_test(void) {
    {
        slop_arena _arena = slop_arena_new(1024);
        slop_arena* arena = &_arena;
        {
            __auto_type p = (comprehensive_test_Player*)slop_arena_alloc(arena, sizeof(comprehensive_test_Player));
            p->score = 42;
            return p->score;
        }
        slop_arena_free(arena);
    }
}

int64_t comprehensive_test_sequence_test(int64_t x) {
    printf("%s\n", "testing");
    return (x + 1);
}

int64_t* comprehensive_test_get_address(int64_t x) {
    return (&x);
}

uint8_t comprehensive_test_to_byte(int64_t n) {
    return ((uint8_t)(n));
}

int64_t comprehensive_test_inline_c(void) {
    return 42;
}

int64_t comprehensive_test_always_positive(int64_t x) {
    if ((x < 1)) {
        return 1;
    } else {
        return x;
    }
}

int64_t comprehensive_test_nested_calls(int64_t a, int64_t b) {
    return comprehensive_test_classify(comprehensive_test_bitwise_ops(a, b));
}

int64_t comprehensive_test_option_test(slop_option_int opt) {
    __auto_type _match_val = opt;
    if (_match_val.has_value) {
        __auto_type v = _match_val.value;
        return v;
    }
}

int main(void) {
    return 0;
}

