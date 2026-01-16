#!/bin/bash
# Test script for native transpiler strlib-related issues
# Tests: primitive type constructors, @example annotations, FFI functions

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
TRANSPILER="$REPO_ROOT/lib/compiler/transpiler/slop-transpiler"
RUNTIME_DIR="$REPO_ROOT/src/slop/runtime"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

echo "=== Native Transpiler Strlib Tests ==="
echo ""

PASS_COUNT=0
FAIL_COUNT=0

# Test 1: String constructor
echo "Test 1: Primitive type constructor (String data len)"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/string_constructor.slop" -o "$OUTPUT"

# Check: Should NOT generate function call like string_constructor_test_String(...)
if grep -q "string_constructor_test_String" "$OUTPUT"; then
    echo -e "${RED}FAIL: (String data len) generated function call instead of struct literal${NC}"
    echo "Found: $(grep 'string_constructor_test_String' "$OUTPUT")"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    # Check: Should generate struct literal syntax
    if grep -q "slop_string" "$OUTPUT"; then
        echo -e "${GREEN}PASS${NC}"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "${YELLOW}WARN: No slop_string type found in output${NC}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
fi

# Test 2: Compilation check for string constructor
echo ""
echo "Test 2: String constructor compilation check"
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo -e "${RED}FAIL: String constructor code doesn't compile${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -5
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi
rm -f "$OUTPUT"

# Test 3: @example annotation
echo ""
echo "Test 3: @example annotation handling"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/example_annotation.slop" -o "$OUTPUT"

# Check: Should NOT generate any _at_example function calls
if grep -q "_at_example\|@example" "$OUTPUT"; then
    echo -e "${RED}FAIL: @example annotation was transpiled to C code${NC}"
    echo "Found: $(grep -o '.*_at_example.*\|.*@example.*' "$OUTPUT" | head -3)"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    echo -e "${GREEN}PASS${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
fi

# Test 4: Compilation check for example annotation
echo ""
echo "Test 4: @example annotation compilation check"
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo -e "${RED}FAIL: @example annotation code doesn't compile${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -5
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi
rm -f "$OUTPUT"

# Test 5: FFI function prefixing
echo ""
echo "Test 5: FFI function call prefixing"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/ffi_functions.slop" -o "$OUTPUT"

# Check: Should NOT generate module-prefixed FFI call
if grep -q "ffi_functions_test_isalpha" "$OUTPUT"; then
    echo -e "${RED}FAIL: FFI function has incorrect module prefix${NC}"
    echo "Found: $(grep 'ffi_functions_test_isalpha' "$OUTPUT")"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    # Check: Should have unprefixed isalpha call
    if grep -q "isalpha(" "$OUTPUT"; then
        echo -e "${GREEN}PASS${NC}"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        echo -e "${YELLOW}WARN: isalpha call not found${NC}"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
fi

# Test 6: Compilation check for FFI functions
echo ""
echo "Test 6: FFI function compilation check"
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
    PASS_COUNT=$((PASS_COUNT + 1))
else
    echo -e "${RED}FAIL: FFI function code doesn't compile${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -5
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi
rm -f "$OUTPUT"

echo ""
echo "=== Results: $PASS_COUNT passed, $FAIL_COUNT failed ==="

if [ $FAIL_COUNT -gt 0 ]; then
    exit 1
fi
