/*
 * SLOP Runtime - Minimal runtime for SLOP-generated C code
 * 
 * Provides:
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

static inline slop_string string_new(slop_arena* arena, const char* cstr) {
    return slop_string_new(arena, cstr);
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

static inline bool string_eq(slop_string a, slop_string b) {
    return slop_string_eq(a, b);
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

/* String split - splits string on single-char delimiter */
static inline slop_list_string string_split(slop_arena* arena, slop_string s, slop_string delim) {
    SLOP_PRE(delim.len == 1, "delimiter must be single character");
    char dc = delim.data[0];

    /* Count segments */
    size_t count = 1;
    for (size_t i = 0; i < s.len; i++) {
        if (s.data[i] == dc) count++;
    }

    slop_list_string result = slop_list_string_new(arena, count);

    size_t start = 0;
    for (size_t i = 0; i <= s.len; i++) {
        if (i == s.len || s.data[i] == dc) {
            slop_string seg = slop_string_new_len(arena, s.data + start, i - start);
            slop_list_string_push(arena, &result, seg);
            start = i + 1;
        }
    }
    return result;
}

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
 * Typed String-Keyed Map (generic via macro)
 *
 * Generates a type-safe wrapper around slop_map for (Map String V) types.
 * The typedef allows assignment compatibility while providing typed accessors.
 * ============================================================ */

#define SLOP_STRING_MAP_DEFINE(V, Name, OptName) \
    typedef slop_map Name; \
    \
    static inline Name Name##_new(slop_arena* arena, size_t cap) { \
        return slop_map_new(arena, cap); \
    } \
    \
    static inline OptName Name##_get(Name* map, slop_string key) { \
        void* v = slop_map_get(map, key); \
        if (v) return (OptName){ .has_value = true, .value = *(V*)v }; \
        return (OptName){ .has_value = false }; \
    } \
    \
    static inline void Name##_put(slop_arena* arena, Name* map, slop_string key, V value) { \
        V* stored = (V*)slop_arena_alloc(arena, sizeof(V)); \
        *stored = value; \
        slop_map_put(arena, map, key, stored); \
    }

/* ============================================================
 * Generic Map Operations (for transpiled SLOP code)
 *
 * Integer-keyed map with fixed-size value storage (up to 256 bytes).
 * Used for SLOP Map types with integer keys (e.g., Map PetId Pet).
 * ============================================================ */

/* Generic map entry with fixed-size value storage */
typedef struct slop_gmap_entry_t {
    int64_t gmap_key;
    uint8_t gmap_value[256];
    size_t gmap_value_size;
    bool gmap_occupied;
} slop_gmap_entry_t;

typedef struct slop_gmap_t {
    size_t gmap_len;
    size_t gmap_cap;
    slop_gmap_entry_t* gmap_entries;
} slop_gmap_t;

/* Create empty generic map */
static inline void* map_empty(void) {
    slop_gmap_t* m = (slop_gmap_t*)malloc(sizeof(slop_gmap_t));
    m->gmap_len = 0;
    m->gmap_cap = SLOP_MAP_INITIAL_CAPACITY;
    m->gmap_entries = (slop_gmap_entry_t*)calloc(m->gmap_cap, sizeof(slop_gmap_entry_t));
    return m;
}

/* Check if key exists - use unique param name to avoid shadowing */
static inline bool map_has(void* gmap_ptr, int64_t gmap_lookup_key) {
    slop_gmap_t* m = (slop_gmap_t*)gmap_ptr;
    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied && m->gmap_entries[idx].gmap_key == gmap_lookup_key) {
            return true;
        }
    }
    return false;
}

/* Remove key (returns map for functional style) */
static inline void* map_remove(void* gmap_ptr, int64_t gmap_lookup_key) {
    slop_gmap_t* m = (slop_gmap_t*)gmap_ptr;
    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied && m->gmap_entries[idx].gmap_key == gmap_lookup_key) {
            m->gmap_entries[idx].gmap_occupied = false;
            return m;
        }
    }
    return m;
}

/* Map put - implemented as a macro to handle any value type */
/* Returns the map pointer (functional style but mutates in place) */
#define map_put(gmap_ptr, gmap_k, gmap_v) \
    _slop_map_put_impl((gmap_ptr), (gmap_k), &(gmap_v), sizeof(gmap_v))

static inline void* _slop_map_put_impl(void* gmap_ptr, int64_t gmap_k, const void* gmap_v, size_t gmap_vsz) {
    slop_gmap_t* m = (slop_gmap_t*)gmap_ptr;

    /* Check if key exists - update in place */
    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied && m->gmap_entries[idx].gmap_key == gmap_k) {
            memcpy(m->gmap_entries[idx].gmap_value, gmap_v, gmap_vsz);
            m->gmap_entries[idx].gmap_value_size = gmap_vsz;
            return m;
        }
    }

    /* Add new entry */
    if (m->gmap_len >= m->gmap_cap) {
        size_t new_cap = m->gmap_cap * 2;
        m->gmap_entries = (slop_gmap_entry_t*)realloc(m->gmap_entries, new_cap * sizeof(slop_gmap_entry_t));
        memset(m->gmap_entries + m->gmap_cap, 0, (new_cap - m->gmap_cap) * sizeof(slop_gmap_entry_t));
        m->gmap_cap = new_cap;
    }

    m->gmap_entries[m->gmap_len].gmap_key = gmap_k;
    memcpy(m->gmap_entries[m->gmap_len].gmap_value, gmap_v, gmap_vsz);
    m->gmap_entries[m->gmap_len].gmap_value_size = gmap_vsz;
    m->gmap_entries[m->gmap_len].gmap_occupied = true;
    m->gmap_len++;

    return m;
}

/* Map get - returns Option-like struct matching generated types layout:
 * typedef struct { uint8_t tag; union { T some; } data; } slop_option_T;
 * We use a large enough buffer to hold any value type.
 */
typedef struct {
    uint8_t tag;
    union { uint8_t some[256]; } data;
} slop_gmap_option_raw;

static inline slop_gmap_option_raw _slop_map_get_raw(void* gmap_ptr, int64_t gmap_k) {
    slop_gmap_option_raw result = {1, {{0}}}; /* tag=1 means none */
    slop_gmap_t* m = (slop_gmap_t*)gmap_ptr;

    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied && m->gmap_entries[idx].gmap_key == gmap_k) {
            result.tag = 0; /* tag=0 means some */
            memcpy(result.data.some, m->gmap_entries[idx].gmap_value, m->gmap_entries[idx].gmap_value_size);
            break;
        }
    }
    return result;
}

/* Map values - returns List-like struct matching generated types layout:
 * typedef struct { T* data; size_t len; size_t cap; } slop_list_T;
 */
typedef struct { uint8_t* data; size_t len; size_t cap; } slop_gmap_list;

static inline slop_gmap_list _slop_map_values_raw(void* gmap_ptr, size_t value_size) {
    slop_gmap_t* m = (slop_gmap_t*)gmap_ptr;
    size_t count = 0;
    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied) count++;
    }
    if (count == 0) {
        return (slop_gmap_list){NULL, 0, 0};
    }
    uint8_t* data = (uint8_t*)malloc(count * value_size);
    size_t write_idx = 0;
    for (size_t idx = 0; idx < m->gmap_len; idx++) {
        if (m->gmap_entries[idx].gmap_occupied) {
            memcpy(data + (write_idx * value_size),
                   m->gmap_entries[idx].gmap_value, value_size);
            write_idx++;
        }
    }
    return (slop_gmap_list){data, count, count};
}

/* Take first n elements from list - modifies in place and returns */
static inline slop_gmap_list _slop_take_raw(slop_gmap_list lst, int64_t n) {
    if ((size_t)n < lst.len) lst.len = (size_t)n;
    return lst;
}

/*
 * Type-specific map_get/map_values/take must be generated by the transpiler.
 * Define a macro that generates them for a given value type:
 */
#define SLOP_MAP_OPS_DEFINE(V, OptName, ListName) \
    static inline OptName map_get_##V(void* m, int64_t k) { \
        slop_gmap_option_raw raw = _slop_map_get_raw(m, k); \
        OptName result; \
        result.tag = raw.tag; \
        if (raw.tag == 0) memcpy(&result.data.some, raw.some, sizeof(V)); \
        return result; \
    } \
    static inline ListName map_values_##V(void* m) { \
        slop_gmap_list raw = _slop_map_values_raw(m, sizeof(V)); \
        return (ListName){(V*)raw.data, raw.len, raw.cap}; \
    } \
    static inline ListName take_##V(int64_t n, ListName lst) { \
        if ((size_t)n < lst.len) lst.len = (size_t)n; \
        return lst; \
    }

/*
 * Type-specific map operations are generated by the transpiler.
 * Use SLOP_MAP_GET_DEFINE(ValueType, OptionType) to define map_get for a type.
 * Use SLOP_MAP_VALUES_DEFINE(ValueType, ListType) to define map_values for a type.
 */

#define SLOP_MAP_GET_DEFINE(V, OptType) \
    static inline OptType map_get_##V(void* m, int64_t k) { \
        slop_gmap_option_raw raw = _slop_map_get_raw(m, k); \
        OptType result = {0}; \
        result.has_value = (raw.tag == 0); \
        if (raw.tag == 0) memcpy(&result.value, raw.data.some, sizeof(V)); \
        return result; \
    }

#define SLOP_MAP_VALUES_DEFINE(V, ListType) \
    static inline ListType map_values_##V(void* m) { \
        slop_gmap_list raw = _slop_map_values_raw(m, sizeof(V)); \
        return (ListType){(V*)raw.data, raw.len, raw.cap}; \
    }

#define SLOP_TAKE_DEFINE(V, ListType) \
    static inline ListType take_##V(int64_t n, ListType lst) { \
        if ((size_t)n < lst.len) lst.len = (size_t)n; \
        return lst; \
    }

/* Generic take that works with any list type */
#define take(n, lst) ({ \
    __auto_type _take_lst = (lst); \
    int64_t _take_n = (n); \
    if ((size_t)_take_n < _take_lst.len) _take_lst.len = (size_t)_take_n; \
    _take_lst; \
})

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

/* Pre-define common string-keyed maps (must come after SLOP_OPTION_DEFINE) */
SLOP_STRING_MAP_DEFINE(slop_string, slop_map_string_string, slop_option_string)
SLOP_STRING_MAP_DEFINE(int64_t, slop_map_string_int, slop_option_int)

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

static inline void sleep_ms(int64_t ms) {
    slop_sleep_ms(ms);
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
