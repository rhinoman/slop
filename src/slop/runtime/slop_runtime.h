/*
 * SLOP Runtime - Minimal runtime for SLOP-generated C code
 * 
 * ~500 lines total. Provides:
 * - Arena allocator
 * - String type
 * - List type
 * - Map type
 * - Contract macros
 */

#ifndef SLOP_RUNTIME_H
#define SLOP_RUNTIME_H

#include <stdint.h>
#include <stddef.h>
#include <stdbool.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

/* ============================================================
 * Configuration
 * ============================================================ */

#ifndef SLOP_ARENA_DEFAULT_SIZE
#define SLOP_ARENA_DEFAULT_SIZE 4096
#endif

#ifndef SLOP_MAP_INITIAL_CAPACITY
#define SLOP_MAP_INITIAL_CAPACITY 16
#endif

/* ============================================================
 * Contracts
 * ============================================================ */

#ifdef SLOP_DEBUG
    #define SLOP_PRE(cond, msg) \
        do { if (!(cond)) { \
            fprintf(stderr, "SLOP precondition failed: %s\n  at %s:%d\n", \
                    msg, __FILE__, __LINE__); \
            abort(); \
        }} while(0)
    
    #define SLOP_POST(cond, msg) \
        do { if (!(cond)) { \
            fprintf(stderr, "SLOP postcondition failed: %s\n  at %s:%d\n", \
                    msg, __FILE__, __LINE__); \
            abort(); \
        }} while(0)
    
    #define SLOP_ASSERT(cond, msg) \
        do { if (!(cond)) { \
            fprintf(stderr, "SLOP assertion failed: %s\n  at %s:%d\n", \
                    msg, __FILE__, __LINE__); \
            abort(); \
        }} while(0)
#else
    #define SLOP_PRE(cond, msg) ((void)0)
    #define SLOP_POST(cond, msg) ((void)0)
    #define SLOP_ASSERT(cond, msg) ((void)0)
#endif

/* ============================================================
 * Arena Allocator
 * ============================================================ */

typedef struct slop_arena {
    uint8_t* base;
    size_t offset;
    size_t capacity;
    struct slop_arena* next;  /* For overflow arenas */
} slop_arena;

static inline slop_arena slop_arena_new(size_t capacity) {
    slop_arena arena;
    arena.base = (uint8_t*)malloc(capacity);
    arena.offset = 0;
    arena.capacity = capacity;
    arena.next = NULL;
    return arena;
}

static inline void* slop_arena_alloc(slop_arena* arena, size_t size) {
    /* Align to 8 bytes */
    size = (size + 7) & ~7;
    
    /* Check if we need overflow arena */
    if (arena->offset + size > arena->capacity) {
        if (arena->next == NULL) {
            size_t new_cap = arena->capacity * 2;
            if (new_cap < size) new_cap = size * 2;
            arena->next = (slop_arena*)malloc(sizeof(slop_arena));
            *arena->next = slop_arena_new(new_cap);
        }
        return slop_arena_alloc(arena->next, size);
    }
    
    void* ptr = arena->base + arena->offset;
    arena->offset += size;
    return ptr;
}

static inline void slop_arena_free(slop_arena* arena) {
    if (arena->next) {
        slop_arena_free(arena->next);
        free(arena->next);
    }
    free(arena->base);
    arena->base = NULL;
    arena->offset = 0;
    arena->capacity = 0;
}

static inline void slop_arena_reset(slop_arena* arena) {
    arena->offset = 0;
    if (arena->next) {
        slop_arena_free(arena->next);
        free(arena->next);
        arena->next = NULL;
    }
}

/* ============================================================
 * String Type (immutable, length-prefixed)
 * ============================================================ */

typedef struct {
    size_t len;
    const char* data;
} slop_string;

#define SLOP_STR(literal) ((slop_string){sizeof(literal)-1, literal})

static inline slop_string slop_string_new(slop_arena* arena, const char* cstr) {
    size_t len = strlen(cstr);
    char* data = (char*)slop_arena_alloc(arena, len + 1);
    memcpy(data, cstr, len + 1);
    return (slop_string){len, data};
}

static inline slop_string slop_string_new_len(slop_arena* arena, const char* src, size_t len) {
    char* data = (char*)slop_arena_alloc(arena, len + 1);
    memcpy(data, src, len);
    data[len] = '\0';
    return (slop_string){len, data};
}

static inline bool slop_string_eq(slop_string a, slop_string b) {
    if (a.len != b.len) return false;
    return memcmp(a.data, b.data, a.len) == 0;
}

static inline slop_string slop_string_concat(slop_arena* arena, slop_string a, slop_string b) {
    size_t len = a.len + b.len;
    char* data = (char*)slop_arena_alloc(arena, len + 1);
    memcpy(data, a.data, a.len);
    memcpy(data + a.len, b.data, b.len);
    data[len] = '\0';
    return (slop_string){len, data};
}

static inline slop_string slop_string_slice(slop_string s, size_t start, size_t end) {
    SLOP_PRE(start <= end && end <= s.len, "valid slice bounds");
    return (slop_string){end - start, s.data + start};
}

static inline size_t string_len(slop_string s) {
    return s.len;
}

static inline int32_t string_char_at(slop_string s, size_t index) {
    if (index >= s.len) return 0;
    return (int32_t)(unsigned char)s.data[index];
}

static inline slop_string string_slice(slop_string s, size_t start, size_t end) {
    return slop_string_slice(s, start, end);
}

static inline int64_t string_to_int(slop_string s) {
    int64_t result = 0;
    int negative = 0;
    size_t i = 0;
    if (s.len > 0 && s.data[0] == '-') {
        negative = 1;
        i = 1;
    }
    for (; i < s.len; i++) {
        if (s.data[i] >= '0' && s.data[i] <= '9') {
            result = result * 10 + (s.data[i] - '0');
        }
    }
    return negative ? -result : result;
}

static inline slop_string int_to_string(slop_arena* arena, int64_t n) {
    char buf[32];
    int len = snprintf(buf, sizeof(buf), "%ld", (long)n);
    return slop_string_new_len(arena, buf, len);
}

static inline slop_string string_concat(slop_arena* arena, slop_string a, slop_string b) {
    return slop_string_concat(arena, a, b);
}

/* ============================================================
 * Bytes Type (mutable, length + capacity)
 * ============================================================ */

typedef struct {
    size_t len;
    size_t cap;
    uint8_t* data;
} slop_bytes;

static inline slop_bytes slop_bytes_new(slop_arena* arena, size_t capacity) {
    uint8_t* data = (uint8_t*)slop_arena_alloc(arena, capacity);
    return (slop_bytes){0, capacity, data};
}

static inline slop_bytes slop_bytes_from(slop_arena* arena, const uint8_t* src, size_t len) {
    uint8_t* data = (uint8_t*)slop_arena_alloc(arena, len);
    memcpy(data, src, len);
    return (slop_bytes){len, len, data};
}

/* ============================================================
 * List Type (dynamic array, generic via macros)
 * ============================================================ */

#define SLOP_LIST_DEFINE(T, Name) \
    typedef struct { \
        size_t len; \
        size_t cap; \
        T* data; \
    } Name; \
    \
    static inline Name Name##_new(slop_arena* arena, size_t initial_cap) { \
        T* data = (T*)slop_arena_alloc(arena, initial_cap * sizeof(T)); \
        return (Name){0, initial_cap, data}; \
    } \
    \
    static inline void Name##_push(slop_arena* arena, Name* list, T item) { \
        if (list->len >= list->cap) { \
            size_t new_cap = list->cap * 2; \
            T* new_data = (T*)slop_arena_alloc(arena, new_cap * sizeof(T)); \
            memcpy(new_data, list->data, list->len * sizeof(T)); \
            list->data = new_data; \
            list->cap = new_cap; \
        } \
        list->data[list->len++] = item; \
    } \
    \
    static inline T* Name##_get(Name* list, size_t i) { \
        SLOP_PRE(i < list->len, "list index in bounds"); \
        return &list->data[i]; \
    }

/* Pre-define common list types */
SLOP_LIST_DEFINE(int64_t, slop_list_int)
SLOP_LIST_DEFINE(double, slop_list_float)
SLOP_LIST_DEFINE(slop_string, slop_list_string)
SLOP_LIST_DEFINE(void*, slop_list_ptr)

/* ============================================================
 * Map Type (simple hash map)
 * ============================================================ */

typedef struct {
    slop_string key;
    void* value;
    bool occupied;
} slop_map_entry;

typedef struct {
    size_t len;
    size_t cap;
    slop_map_entry* entries;
} slop_map;

static inline uint64_t slop_hash_string(slop_string s) {
    uint64_t hash = 14695981039346656037ULL;
    for (size_t i = 0; i < s.len; i++) {
        hash ^= (uint8_t)s.data[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}

static inline slop_map slop_map_new(slop_arena* arena, size_t capacity) {
    slop_map_entry* entries = (slop_map_entry*)slop_arena_alloc(
        arena, capacity * sizeof(slop_map_entry));
    memset(entries, 0, capacity * sizeof(slop_map_entry));
    return (slop_map){0, capacity, entries};
}

static inline void* slop_map_get(slop_map* map, slop_string key) {
    uint64_t hash = slop_hash_string(key);
    size_t idx = hash % map->cap;
    
    for (size_t i = 0; i < map->cap; i++) {
        size_t probe = (idx + i) % map->cap;
        if (!map->entries[probe].occupied) {
            return NULL;
        }
        if (slop_string_eq(map->entries[probe].key, key)) {
            return map->entries[probe].value;
        }
    }
    return NULL;
}

static inline void slop_map_put(slop_arena* arena, slop_map* map, 
                                 slop_string key, void* value) {
    /* Grow if needed (75% load factor) */
    if (map->len * 4 >= map->cap * 3) {
        size_t new_cap = map->cap * 2;
        slop_map new_map = slop_map_new(arena, new_cap);
        for (size_t i = 0; i < map->cap; i++) {
            if (map->entries[i].occupied) {
                slop_map_put(arena, &new_map, 
                            map->entries[i].key, map->entries[i].value);
            }
        }
        *map = new_map;
    }
    
    uint64_t hash = slop_hash_string(key);
    size_t idx = hash % map->cap;
    
    for (size_t i = 0; i < map->cap; i++) {
        size_t probe = (idx + i) % map->cap;
        if (!map->entries[probe].occupied) {
            map->entries[probe].key = key;
            map->entries[probe].value = value;
            map->entries[probe].occupied = true;
            map->len++;
            return;
        }
        if (slop_string_eq(map->entries[probe].key, key)) {
            map->entries[probe].value = value;
            return;
        }
    }
}

static inline bool slop_map_has(slop_map* map, slop_string key) {
    return slop_map_get(map, key) != NULL;
}

/* ============================================================
 * Option Type (generic via macros)
 * ============================================================ */

#define SLOP_OPTION_DEFINE(T, Name) \
    typedef struct { \
        bool has_value; \
        T value; \
    } Name; \
    \
    static inline Name Name##_some(T val) { \
        return (Name){true, val}; \
    } \
    \
    static inline Name Name##_none(void) { \
        return (Name){false}; \
    } \
    \
    static inline bool Name##_is_some(Name opt) { \
        return opt.has_value; \
    } \
    \
    static inline T Name##_unwrap(Name opt) { \
        SLOP_PRE(opt.has_value, "unwrap on Some"); \
        return opt.value; \
    }

SLOP_OPTION_DEFINE(int64_t, slop_option_int)
SLOP_OPTION_DEFINE(double, slop_option_float)
SLOP_OPTION_DEFINE(slop_string, slop_option_string)
SLOP_OPTION_DEFINE(void*, slop_option_ptr)

/* ============================================================
 * Result Type (generic via macros)
 * ============================================================ */

#define SLOP_RESULT_DEFINE(T, E, Name) \
    typedef struct { \
        bool is_ok; \
        union { T ok; E err; } data; \
    } Name; \
    \
    static inline Name Name##_ok(T val) { \
        Name r; r.is_ok = true; r.data.ok = val; return r; \
    } \
    \
    static inline Name Name##_err(E e) { \
        Name r; r.is_ok = false; r.data.err = e; return r; \
    } \
    \
    static inline T Name##_unwrap(Name res) { \
        SLOP_PRE(res.is_ok, "unwrap on Ok"); \
        return res.data.ok; \
    }

/* Common error type */
typedef enum {
    SLOP_ERR_NONE = 0,
    SLOP_ERR_NULL_POINTER,
    SLOP_ERR_OUT_OF_BOUNDS,
    SLOP_ERR_INVALID_ARGUMENT,
    SLOP_ERR_NOT_FOUND,
    SLOP_ERR_ALREADY_EXISTS,
    SLOP_ERR_IO_ERROR,
    SLOP_ERR_PARSE_ERROR,
    SLOP_ERR_INSUFFICIENT_FUNDS,  /* Example domain error */
} slop_error;

static inline const char* slop_error_str(slop_error e) {
    switch (e) {
        case SLOP_ERR_NONE: return "none";
        case SLOP_ERR_NULL_POINTER: return "null pointer";
        case SLOP_ERR_OUT_OF_BOUNDS: return "out of bounds";
        case SLOP_ERR_INVALID_ARGUMENT: return "invalid argument";
        case SLOP_ERR_NOT_FOUND: return "not found";
        case SLOP_ERR_ALREADY_EXISTS: return "already exists";
        case SLOP_ERR_IO_ERROR: return "io error";
        case SLOP_ERR_PARSE_ERROR: return "parse error";
        case SLOP_ERR_INSUFFICIENT_FUNDS: return "insufficient funds";
        default: return "unknown error";
    }
}

SLOP_RESULT_DEFINE(int64_t, slop_error, slop_result_int)
SLOP_RESULT_DEFINE(void*, slop_error, slop_result_ptr)
SLOP_RESULT_DEFINE(slop_string, slop_error, slop_result_string)

/* ============================================================
 * Time
 * ============================================================ */

#ifdef _WIN32
#include <windows.h>
static inline int64_t slop_now_ms(void) {
    return (int64_t)GetTickCount64();
}
#else
#include <time.h>
static inline int64_t slop_now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
#endif

static inline void slop_sleep_ms(int64_t ms) {
#ifdef _WIN32
    Sleep((DWORD)ms);
#else
    struct timespec ts = {ms / 1000, (ms % 1000) * 1000000};
    nanosleep(&ts, NULL);
#endif
}

/* ============================================================
 * Integer formatting (for output)
 * ============================================================ */

static inline slop_string slop_int_to_string(slop_arena* arena, int64_t n) {
    char buf[32];
    int len = snprintf(buf, sizeof(buf), "%ld", (long)n);
    return slop_string_new_len(arena, buf, len);
}

static inline slop_string slop_float_to_string(slop_arena* arena, double n) {
    char buf[64];
    int len = snprintf(buf, sizeof(buf), "%g", n);
    return slop_string_new_len(arena, buf, len);
}

#endif /* SLOP_RUNTIME_H */
