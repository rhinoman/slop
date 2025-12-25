# SLOP Hybrid Generation Pipeline

## Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      HYBRID PIPELINE                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Human                     Machine                              │
│  ─────                     ───────                              │
│                                                                 │
│  Intent ──────────────────► Contracts                           │
│  Constraints ─────────────► Types                               │
│  Examples ────────────────► Tests                               │
│                                                                 │
│                            │                                    │
│                            ▼                                    │
│                                                                 │
│                     ┌─────────────┐                             │
│                     │ Deterministic│                            │
│  Schemas ──────────►│ Type Gen    │                             │
│  Templates ────────►│ Scaffolding │                             │
│                     └──────┬──────┘                             │
│                            │                                    │
│                            ▼                                    │
│                     ┌─────────────┐                             │
│                     │ LLM         │ Tiered by complexity        │
│                     │ Hole Filling│ Small models for simple     │
│                     └──────┬──────┘                             │
│                            │                                    │
│                            ▼                                    │
│                     ┌─────────────┐                             │
│                     │ Deterministic│                            │
│                     │ Verification │                            │
│                     └──────┬──────┘                             │
│                            │                                    │
│                            ▼                                    │
│                     ┌─────────────┐                             │
│                     │ Transpile   │                             │
│                     │ to C        │                             │
│                     └──────┬──────┘                             │
│                            │                                    │
│                            ▼                                    │
│                     ┌─────────────┐                             │
│                     │ cc -O3      │ GCC/Clang optimization      │
│                     └──────┬──────┘                             │
│                            │                                    │
│                            ▼                                    │
│                     Native Binary                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Phase 1: Deterministic Generation

### 1.1 Schema → Types

**JSON Schema → SLOP**

```json
{
  "type": "object",
  "properties": {
    "id": { "type": "integer", "minimum": 1 },
    "name": { "type": "string", "minLength": 1, "maxLength": 100 },
    "age": { "type": "integer", "minimum": 0, "maximum": 150 }
  },
  "required": ["id", "name"]
}
```

↓ Deterministic ↓

```
(type UserId (Int 1 ..))
(type UserName (String 1 .. 100))
(type Age (Int 0 .. 150))

(type User (record
  (id UserId)
  (name UserName)
  (age (Option Age))))
```

↓ Transpiles to C ↓

```c
typedef struct { int64_t value; } UserId;
typedef slop_string UserName;
typedef struct { uint8_t value; } Age;

typedef struct {
    UserId id;
    UserName name;
    struct { uint8_t has_value; Age value; } age;
} User;

// Constructor with validation
User* User_new(slop_arena* arena, int64_t id, slop_string name) {
    SLOP_PRE(id >= 1, "id >= 1");
    SLOP_PRE(name.len >= 1 && name.len <= 100, "name length 1..100");
    User* u = slop_arena_alloc(arena, sizeof(User));
    u->id = (UserId){id};
    u->name = name;
    u->age.has_value = 0;
    return u;
}
```

### 1.2 Template Expansion

**Template Definition**

```
(deftemplate service (name entity operations)
  (module ,name
    (export ,@(map operations (fn (op) (list op 2))))
    
    (type State (Map EntityId Entity))
    
    ,@(map operations (fn (op)
        (gen-operation op entity)))))
```

**Template Instantiation**

```
(@derive-from (template service)
  :name user-service
  :entity User  
  :operations (create find update delete))
```

↓ Expands to scaffold with holes ↓

### 1.3 Contract Shapes

From signatures, infer contract templates:

```
(sig withdraw ((account (Ptr Account)) (amount (Int 1 ..)))
     (Result (Ptr Account) Error))
```

↓ Deterministic inference ↓

```
(@pre (!= account nil))
(@pre (>= (. account balance) amount))
(@post (match $result
         ((ok new) (== (. new balance) (- (. account balance) amount)))
         ((error _) true)))
```

## Phase 2: LLM Generation

### 2.1 Hole Classification

```
┌─────────────────────────────────────────────────────────────────┐
│ TIER 1 (1-3B models): Trivial                                   │
├─────────────────────────────────────────────────────────────────┤
│ - Boolean expressions                                           │
│ - Simple arithmetic                                             │
│ - Direct field access                                           │
│ - Single function calls                                         │
│                                                                 │
│ Example: (hole Bool "Check if user is adult")                   │
│ Output:  (>= (. user age) 18)                                   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ TIER 2 (7-8B models): Simple                                    │
├─────────────────────────────────────────────────────────────────┤
│ - Single conditional                                            │
│ - Result construction                                           │
│ - Simple transformations                                        │
│                                                                 │
│ Example: (hole (Result Account Error) "Withdraw if sufficient") │
│ Output:  (if (< (. account balance) amount)                     │
│            (error 'insufficient-funds)                          │
│            (ok (put account balance (- (. account balance)      │
│                                         amount))))              │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ TIER 3 (13-34B models): Moderate                                │
├─────────────────────────────────────────────────────────────────┤
│ - Multiple conditionals                                         │
│ - Loops                                                         │
│ - State manipulation                                            │
│                                                                 │
│ Example: (hole (List User) "Filter active adults, sort by age") │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ TIER 4 (70B+ models): Complex                                   │
├─────────────────────────────────────────────────────────────────┤
│ - Algorithms                                                    │
│ - Complex state machines                                        │
│ - Multiple interacting concerns                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Prompt Construction

```
You are filling a typed hole in SLOP code.

## Context
Function: withdraw
Parameters: (account (Ptr Account)) (amount (Int 1 ..))
Return type: (Result (Ptr Account) Error)

## Hole
Type: (Result (Ptr Account) Error)
Description: Deduct amount from balance if sufficient

## Constraints
- account is non-null (guaranteed by @pre)
- amount >= 1 (guaranteed by type)
- Must check balance >= amount

## Examples
Input: {balance: 100}, 30 → (ok {balance: 70})
Input: {balance: 100}, 150 → (error 'insufficient-funds)

## Available
account: (Ptr Account) with fields: id, balance, status
amount: (Int 1 ..)

Respond with ONLY the SLOP expression.
```

### 2.3 Model Routing

```yaml
models:
  tier-1:
    provider: ollama
    model: phi3:mini
    context: 2048

  tier-2:
    provider: ollama
    model: llama3:8b
    context: 4096

  tier-3:
    provider: interactive      # Use Claude Code subscription
    command: claude
    mode: clipboard

  tier-4:
    provider: anthropic        # Or interactive for subscription-only
    model: claude-sonnet-4-20250514

routing:
  max_retries: 2
  escalate_on_failure: true
  batch_interactive: true      # Batch interactive tier holes together
```

### 2.4 Provider Types

| Provider | Description | Use Case |
|----------|-------------|----------|
| `ollama` | Local Ollama instance | Fast, private, tier-1/2 |
| `openai-compatible` | OpenAI API format | Hosted LLMs, enterprise |
| `anthropic` | Anthropic API | High-quality tier-4 |
| `interactive` | External tools (Claude Code, Cursor, Aider) | Use existing subscriptions |

**Interactive providers** route holes to external AI tools, avoiding API costs:
- `clipboard` mode: Copies prompt to clipboard, waits for pasted response
- `file` mode: Writes prompt to `.slop-hole-request`, reads response from `.slop-hole-response`
- Use `--batch-interactive` flag to batch multiple holes into a single session

Example workflow with interactive provider:
```
$ slop fill app.slop --batch-interactive

Tier 1-2 (local): 18 holes filled in 12s
Tier 3-4 (interactive): 5 holes batched

→ Prompt copied to clipboard. Paste into claude
← [user pastes response]

✓ All holes filled
```

## Phase 3: Verification

### 3.1 Type Checking

Including range type analysis:

```
(+ (Int 0 .. 100) (Int 0 .. 100)) : (Int 0 .. 200)
(- (Int 0 .. 100) (Int 0 .. 50))  : (Int -50 .. 100)

; Array access with bounds
(@ arr i) where arr : (Array T 10), i : (Int 0 .. 9) → T  ✓
(@ arr i) where arr : (Array T 10), i : (Int 0 .. 20) → Error!
```

### 3.2 Contract Verification

```
; Static verification where possible
(@pre (> x 0))
(@post (> $result 0))
(+ x 1)  ; Provably satisfies post given pre

; Generate runtime checks otherwise
(@pre (< amount (. account balance)))
; Becomes: SLOP_PRE(amount < account->balance, "...")
```

### 3.3 Memory Safety

```
; Track ownership through types
(fn process ((data (OwnPtr Data))))
  ; data is moved in, caller can't use after

(fn read ((data (Ptr Data))))
  ; data is borrowed, caller retains ownership

; Compiler rejects:
(let ((p (alloc-data arena)))
  (process p)      ; p moved
  (read p))        ; Error: use after move
```

## Phase 4: Transpilation to C

### 4.1 Type Generation

```
SLOP                              C
────                              ─

(type Age (Int 0 .. 150))    →    typedef struct {
                                      uint8_t value;
                                  } Age;
                                  
                                  static inline Age Age_new(int64_t v) {
                                      SLOP_PRE(v >= 0 && v <= 150, "Age range");
                                      return (Age){(uint8_t)v};
                                  }

(type User (record              → typedef struct {
  (id UserId)                         UserId id;
  (name String)                       slop_string name;
  (age (Option Age))))                struct { uint8_t has; Age val; } age;
                                  } User;

(type Status (enum              → typedef enum {
  active                              Status_active,
  inactive                            Status_inactive,
  deleted))                           Status_deleted
                                  } Status;

(type Result (union             → typedef struct {
  (ok T)                              uint8_t tag;
  (error E)))                         union {
                                          T ok;
                                          E err;
                                      } data;
                                  } Result_T_E;
                                  
                                  #define IS_OK(r) ((r).tag == 0)
                                  #define UNWRAP(r) ((r).data.ok)
```

### 4.2 Function Generation

```
SLOP:
(fn withdraw ((account (Ptr Account)) (amount (Int 1 ..)))
  (@intent "Withdraw funds from account")
  (@spec (((Ptr Account) (Int 1 ..)) -> (Result (Ptr Account) Error)))
  (@pre (!= account nil))
  (@pre (>= (. account balance) amount))
  
  (if (< (. account balance) amount)
    (error 'insufficient-funds)
    (ok (put account balance (- (. account balance) amount)))))

C:
// Withdraw funds from account
Result_PtrAccount_Error withdraw(Account* account, int64_t amount) {
    // Preconditions
    SLOP_PRE(account != NULL, "account != nil");
    SLOP_PRE(account->balance >= amount, "balance >= amount");
    
    // Implementation
    if (account->balance < amount) {
        return (Result_PtrAccount_Error){
            .tag = 1,
            .data.err = Error_insufficient_funds
        };
    } else {
        account->balance = account->balance - amount;
        return (Result_PtrAccount_Error){
            .tag = 0,
            .data.ok = account
        };
    }
}
```

### 4.3 Memory Patterns

**Arena allocation:**
```c
void handle_request(Request* req) {
    slop_arena arena = slop_arena_new(4096);
    
    User* user = User_parse(&arena, req->body);
    Response* resp = process_user(&arena, user);
    send_response(resp);
    
    slop_arena_free(&arena);  // Everything freed
}
```

**Scoped cleanup:**
```c
// Generated for OwnPtr
void connection_close(Connection* conn) {
    if (conn->socket) socket_free(conn->socket);
    if (conn->buffer) buffer_free(conn->buffer);
    free(conn);
}
```

### 4.4 Control Flow

```
SLOP                              C
────                              ─

(if cond then else)          →    cond ? then : else
                                  // or if statement for complex bodies

(match expr                  →    switch (expr.tag) {
  ((ok val) body1)                    case 0: { T val = expr.data.ok; body1; }
  ((error e) body2))                  case 1: { E e = expr.data.err; body2; }
                                  }

(for (i 0 10) body)          →    for (int64_t i = 0; i < 10; i++) { body; }

(for-each (x list) body)     →    for (size_t _i = 0; _i < list.len; _i++) {
                                      T x = list.data[_i];
                                      body;
                                  }

(? expr)                     →    { auto _tmp = expr;
                                    if (!IS_OK(_tmp)) return _tmp;
                                  }
```

## Phase 5: Compilation

```bash
# Generate C
slop transpile app.slop -o build/app.c

# Compile with optimizations
cc -O3 -march=native -o app build/app.c slop_runtime.c -lssl -lcrypto

# Or for development (fast compile, debug checks)  
tcc -g -DSLOP_DEBUG -o app build/app.c slop_runtime.c
```

### Build Modes

| Mode | Flags | Contracts | Speed |
|------|-------|-----------|-------|
| Debug | `-g -DSLOP_DEBUG` | Runtime checks | Slow |
| Release | `-O3 -DNDEBUG` | Removed | Fast |
| Safe | `-O2 -DSLOP_SAFE` | Checks + optimize | Medium |

## CLI Commands

```bash
# Derive types from schema
slop derive schema.json -o types.slop

# Parse and validate
slop check app.slop

# Fill holes with LLM
slop fill app.slop -o filled.slop --config models.yaml

# Transpile to C
slop transpile filled.slop -o app.c

# Full pipeline
slop build app.slop -o app

# Run tests (from @example and @property)
slop test app.slop
```
