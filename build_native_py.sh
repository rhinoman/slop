#!/bin/sh

## Build the native slop toolchain

# Build the parser
cd lib/compiler/parser
slop build
cp ./slop-parser ../../../src/slop/bin

## Build the checker
cd ../checker
slop build
cp ./slop-checker ../../../src/slop/bin

## Build the transpiler
cd ../transpiler
slop build
cp ./slop-transpiler ../../../src/slop/bin
