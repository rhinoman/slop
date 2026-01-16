# SLOP Project Makefile
#
# Usage:
#   make parse FILE=examples/rate-limiter.slop
#   make transpile FILE=examples/rate-limiter.slop
#   make build FILE=examples/rate-limiter.slop

PYTHON ?= .venv/bin/python
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra
DEBUG_CFLAGS ?= -g -DSLOP_DEBUG -Wall -Wextra
RUNTIME = runtime

.PHONY: help parse transpile check fill build clean test install

help:
	@echo "SLOP Build System"
	@echo ""
	@echo "Usage: make <target> FILE=<path/to/file.slop>"
	@echo ""
	@echo "Targets:"
	@echo "  parse         Parse and display AST"
	@echo "  holes         Show holes in file"
	@echo "  transpile     Convert to C"
	@echo "  check         Validate file"
	@echo "  fill          Fill holes with LLM"
	@echo "  build         Full pipeline to binary"
	@echo "  clean         Remove build artifacts"
	@echo "  test          Run pytest test suite"
	@echo "  install       Install package in dev mode"
	@echo ""
	@echo "Native Toolchain:"
	@echo "  native        Build all native components"
	@echo "  native-parser Build native parser only"
	@echo "  clean-native  Remove native binaries"
	@echo ""
	@echo "Native binaries are installed to bin/"
	@echo "Use 'slop --native' to use native components where available"

install:
	uv pip install -e ".[dev]"

parse:
	$(PYTHON) -m slop.cli parse $(FILE)

holes:
	$(PYTHON) -m slop.cli parse $(FILE) --holes

transpile:
	$(PYTHON) -m slop.cli transpile $(FILE) -o $(basename $(FILE)).c

check:
	$(PYTHON) -m slop.cli check $(FILE)

fill:
	$(PYTHON) -m slop.cli fill $(FILE) -o $(FILE).filled

build:
	$(PYTHON) -m slop.cli build $(FILE)

build-debug:
	$(PYTHON) -m slop.cli build $(FILE) --debug

# Compile C directly
%.o: %.c
	$(CC) $(CFLAGS) -I$(RUNTIME) -c $< -o $@

%-debug.o: %.c
	$(CC) $(DEBUG_CFLAGS) -I$(RUNTIME) -c $< -o $@

clean:
	rm -f *.o *.c.filled build/*
	rm -rf __pycache__ .pytest_cache
	find . -name "*.pyc" -delete

test:
	$(PYTHON) -m pytest tests/ -v

test-quick:
	$(PYTHON) -m pytest tests/ -q

# Example targets
example-hello:
	$(PYTHON) -m slop.cli build examples/hello.slop -o build/hello
	./build/hello

example-rate-limiter:
	$(PYTHON) -m slop.cli transpile examples/rate-limiter.slop -o build/rate_limiter.c
	$(CC) $(CFLAGS) -Isrc/slop/runtime -DRATE_LIMITER_TEST build/rate_limiter.c -o build/rate_limiter

# ==============================================================================
# Native Toolchain Build
# ==============================================================================
# These targets build native SLOP components that can be used by the CLI
# with the --native flag for improved performance.

# Directory where native binaries are installed for CLI use
NATIVE_BIN_DIR = bin

# Native component binaries
NATIVE_PARSER = $(NATIVE_BIN_DIR)/slop-parser

# Parser source files
PARSER_SOURCES = lib/compiler/parser/main.slop \
                 lib/compiler/parser/parser.slop \
                 lib/std/strlib/strlib.slop \
                 lib/std/io/file.slop \
                 lib/std/os/env.slop

.PHONY: native native-parser clean-native

# Build all native components
native: native-parser
	@echo "Native toolchain built successfully"
	@echo "Components installed to $(NATIVE_BIN_DIR)/"
	@echo "Use 'slop --native' to use native components"

# Build the native parser
native-parser: $(NATIVE_PARSER)

$(NATIVE_PARSER): $(PARSER_SOURCES)
	@echo "Building native parser..."
	@mkdir -p $(NATIVE_BIN_DIR)
	cd lib/compiler/parser && uv run slop build -o ../../../$(NATIVE_PARSER)
	@echo "Native parser installed to $(NATIVE_PARSER)"

# Clean native binaries only
clean-native:
	rm -f $(NATIVE_BIN_DIR)/slop-*
	@echo "Native binaries cleaned"

# ==============================================================================
# Legacy Self-hosted Parser (deprecated - use native-parser instead)
# ==============================================================================
parser-transpile:
	$(PYTHON) -m slop.cli transpile lib/parser/slop-parser.slop -o build/slop_parser.c

parser-test: parser-transpile
	$(CC) $(CFLAGS) -Isrc/slop/runtime lib/parser/test-parser.c -o build/test-parser
	./build/test-parser
