# C Interop Example Makefile
#
# Build a SLOP library and link it with a C program

CC = cc
CFLAGS = -O2 -Wall
RUNTIME_DIR = ../../src/slop/runtime

.PHONY: all clean run lib

all: main

# Build the SLOP library using slop build
lib:
	slop build

# Build the C program (assumes library already built via slop build)
main: main.c slop_mylib.h libmylib.a
	$(CC) $(CFLAGS) -o main main.c -L. -lmylib -I$(RUNTIME_DIR) -I.

# Run the example
run: main
	./main

clean:
	rm -f main libmylib.a libmylib.c slop_mylib.h mylib_public.h
