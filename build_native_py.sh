#!/bin/sh

## Build the native slop toolchain

# Build the parser
cd lib/compiler/parser
slop build --python
mv ./slop-parser ../../../bin

## Build the checker
cd ../checker
slop build --python
mv ./slop-checker ../../../bin

## Build the transpiler
cd ../transpiler
slop build --python
mv ./slop-transpiler ../../../bin
