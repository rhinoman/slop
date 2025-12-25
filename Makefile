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
	@echo "  parse      Parse and display AST"
	@echo "  holes      Show holes in file"
	@echo "  transpile  Convert to C"
	@echo "  check      Validate file"
	@echo "  fill       Fill holes with LLM"
	@echo "  build      Full pipeline to binary"
	@echo "  clean      Remove build artifacts"
	@echo "  test       Run pytest test suite"
	@echo "  install    Install package in dev mode"

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
