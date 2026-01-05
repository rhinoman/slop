#!/bin/sh

## Build the native slop toolchain

# Build the parser
cd lib/compiler/parser
slop build --native
cp ./slop-parser ../../../src/slop/bin

## Build the checker
cd ../checker
slop build --native
cp ./slop-checker ../../../src/slop/bin

## Build the transpiler
cd ../transpiler
slop build --native
cp ./slop-transpiler ../../../src/slop/bin
