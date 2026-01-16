#include "slop_runtime.h"

#ifndef SLOP_fibonacci_H
#define SLOP_fibonacci_H

#include "slop_runtime.h"
#include <stdint.h>
#include <stdbool.h>

typedef int64_t fibonacci_Natural;

typedef int64_t fibonacci_FibIndex;

#ifndef SLOP_LIST_FIBONACCI_NATURAL_DEFINED
#define SLOP_LIST_FIBONACCI_NATURAL_DEFINED
SLOP_LIST_DEFINE(fibonacci_Natural, slop_list_fibonacci_Natural)
#endif

#ifndef SLOP_OPTION_FIBONACCI_NATURAL_DEFINED
#define SLOP_OPTION_FIBONACCI_NATURAL_DEFINED
SLOP_OPTION_DEFINE(fibonacci_Natural, slop_option_fibonacci_Natural)
#endif

fibonacci_Natural fibonacci_fib(fibonacci_FibIndex n);
slop_list_fibonacci_Natural fibonacci_fibonacci_sequence(fibonacci_FibIndex count, slop_arena* arena);
int main(void);


#endif

fibonacci_Natural fibonacci_fib(fibonacci_FibIndex n) {
    if ((n <= 1)) {
        return n;
    } else {
        return (fibonacci_fib((n - 1)) + fibonacci_fib((n - 2)));
    }
}

slop_list_fibonacci_Natural fibonacci_fibonacci_sequence(fibonacci_FibIndex count, slop_arena* arena) {
    {
        __auto_type result = ((slop_list_fibonacci_Natural){ .data = (fibonacci_Natural*)slop_arena_alloc(arena, 16 * sizeof(fibonacci_Natural)), .len = 0, .cap = 16 });
        for (int64_t i = 0; i < count; i++) {
            ({ __auto_type _lst_p = &(result); __auto_type _item = (fibonacci_fib(i)); if (_lst_p->len >= _lst_p->cap) { size_t _new_cap = _lst_p->cap == 0 ? 16 : _lst_p->cap * 2; __typeof__(_lst_p->data) _new_data = (__typeof__(_lst_p->data))slop_arena_alloc(arena, _new_cap * sizeof(*_lst_p->data)); if (_lst_p->len > 0) memcpy(_new_data, _lst_p->data, _lst_p->len * sizeof(*_lst_p->data)); _lst_p->data = _new_data; _lst_p->cap = _new_cap; } _lst_p->data[_lst_p->len++] = _item; });
        }
        return result;
    }
}

int main(void) {
    {
        slop_arena _arena = slop_arena_new(4096);
        slop_arena* arena = &_arena;
        {
            int64_t count = 10;
            {
                __auto_type seq = fibonacci_fibonacci_sequence(count, arena);
                for (size_t _i = 0; _i < seq.len; _i++) {
                    __auto_type n = seq.data[_i];
                    printf("%lld\n", (long long)(n));
                }
            }
        }
        slop_arena_free(arena);
    }
}

