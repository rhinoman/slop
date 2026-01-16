#!/bin/bash
# Test script for native transpiler
# Runs native transpiler on test cases and verifies output

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TRANSPILER="$REPO_ROOT/lib/compiler/transpiler/slop-transpiler"
RUNTIME_DIR="$REPO_ROOT/src/slop/runtime"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

echo "=== Native Transpiler Tests ==="
echo ""

# Test 1: Import resolution test
echo "Test 1: Import resolution"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/import_a.slop" "$SCRIPT_DIR/import_b.slop" "$SCRIPT_DIR/import_main.slop" -o "$OUTPUT"

# Verify: my_foo field should use import_a_Foo, not import_b_Foo
if grep -q "import_b_Foo my_foo" "$OUTPUT"; then
    echo -e "${RED}FAIL: Field type should be import_a_Foo, got import_b_Foo${NC}"
    exit 1
fi

if ! grep -q "import_a_Foo my_foo" "$OUTPUT"; then
    echo -e "${RED}FAIL: Field type import_a_Foo not found${NC}"
    exit 1
fi

# Verify: List type should be slop_list_import_a_Token, not slop_list_import_b_Token
if grep -q "slop_list_import_b_Token" "$OUTPUT"; then
    echo -e "${RED}FAIL: List type should be slop_list_import_a_Token, got slop_list_import_b_Token${NC}"
    exit 1
fi

if ! grep -q "slop_list_import_a_Token" "$OUTPUT"; then
    echo -e "${RED}FAIL: List type slop_list_import_a_Token not found${NC}"
    exit 1
fi

echo -e "${GREEN}PASS${NC}"

# Test 2: Try to compile the output
echo ""
echo "Test 2: Compilation check"
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
else
    echo -e "${RED}FAIL: Generated C code doesn't compile${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -20
    exit 1
fi

rm -f "$OUTPUT"

# Test 3: Type alias ordering
echo ""
echo "Test 3: Type alias ordering"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/alias_a.slop" "$SCRIPT_DIR/alias_main.slop" -o "$OUTPUT"

# Verify: MyResult should use alias_a_MyError, not alias_main_MyError
if grep -q "alias_main_MyError" "$OUTPUT"; then
    echo -e "${RED}FAIL: Type alias should use alias_a_MyError, got alias_main_MyError${NC}"
    exit 1
fi

echo -e "${GREEN}PASS${NC}"

# Test 4: Compilation check for alias test
echo ""
echo "Test 4: Alias compilation check"
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
else
    echo -e "${RED}FAIL: Type alias generated code doesn't compile${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -20
    exit 1
fi

rm -f "$OUTPUT"

# Test 5: Result type alias ordering
echo ""
echo "Test 5: Result type alias ordering"
OUTPUT=$(mktemp)
"$TRANSPILER" "$SCRIPT_DIR/result_alias.slop" "$SCRIPT_DIR/result_alias_main.slop" -o "$OUTPUT"

# Verify: The typedef for MyResult should come AFTER the slop_result_... definition
# Check that it compiles (ordering error would prevent compilation)
if cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>/dev/null; then
    echo -e "${GREEN}PASS${NC}"
else
    echo -e "${RED}FAIL: Result type alias ordering is wrong - typedef before definition${NC}"
    echo "Errors:"
    cc -std=c2x -D_DEFAULT_SOURCE -fsyntax-only "$OUTPUT" -I"$RUNTIME_DIR" 2>&1 | head -10
    exit 1
fi

rm -f "$OUTPUT"

echo ""
echo "=== All tests passed ==="
