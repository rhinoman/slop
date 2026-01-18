#!/bin/bash
# Build script for C interop example
# This script builds the SLOP library and links it with the C program

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUNTIME_DIR="../../src/slop/runtime"

echo "Building SLOP library..."
slop build

echo ""
echo "Building C program..."
cc -O2 -Wall -o main main.c -L"$SCRIPT_DIR" -lmylib -I"$SCRIPT_DIR/$RUNTIME_DIR" -I"$SCRIPT_DIR"

echo ""
echo "Build complete! Run with: ./main"
