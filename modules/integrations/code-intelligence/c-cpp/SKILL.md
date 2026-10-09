---
name: c-cpp
description: C and C++ language guidance — compilers, build systems, memory safety, and common idioms. Use when writing, editing, building, or debugging C or C++ code.
---

# C and C++

Practical guidance for C and C++ development.

## Tooling
- Compilers: `gcc`/`g++` or `clang`/`clang++`. Check availability: `which gcc clang`.
- Build systems: prefer `CMake` for C++, plain `make` + `gcc` for small C projects. `pkg-config` for dependencies.
- Static analysis / linters: `clang-tidy`, `cppcheck` (via nix-shell if not on PATH).
- Sanitizers during development: `-fsanitize=address,undefined -g` to catch memory and UB bugs.

## Compiling
- C: `gcc -Wall -Wextra -std=c17 -O2 -o prog src.c`
- C++: `g++ -Wall -Wextra -std=c++20 -O2 -o prog src.cpp`
- Always enable warnings (`-Wall -Wextra`); treat warnings as errors in CI (`-Werror`) where feasible.
- Debug builds: `-g -O0`. Release: `-O2`/`-O3` (avoid `-O3` UB-heavy patterns unless benchmarked).
- Link libraries with `-l<name>`; find flags with `pkg-config --cflags --libs <pkg>`.

## CMake
- `CMakeLists.txt` with `cmake_minimum_required`, `project()`, `add_executable`/`add_library`, `target_include_directories`, `target_link_libraries`.
- Configure: `cmake -B build -DCMAKE_BUILD_TYPE=Debug`; build: `cmake --build build`.
- Set C++ standard: `set(CMAKE_CXX_STANDARD 20)` or `target_compile_features(... cxx_std_20)`.
- Enable warnings globally with `add_compile_options(-Wall -Wextra)`.

## C idioms
- Manual memory management: every `malloc` needs a matching `free`; free on all exit paths.
- Bounds-check arrays; use `strncpy`/`snprintf` not unsafe `strcpy`/`sprintf`.
- Check return values of `malloc`, `fopen`, `realloc`.
- Use `sizeof(arr)/sizeof(arr[0])` for array length, or pass explicit sizes.

## C++ idioms
- Prefer RAII and the standard library: `std::vector`, `std::string`, `std::unique_ptr`/`std::shared_ptr` over raw ownership.
- Pass by `const&` for read-only large objects; by value for small/movable types; `std::move` for transfers.
- Avoid raw `new`/`delete` — use smart pointers and containers.
- Prefer `constexpr`/`consteval` for compile-time values; `auto` for type inference where clarity holds.
- Use exceptions for error handling, not error codes, unless a codebase convention says otherwise.
- `nullptr`, not `NULL` or `0`.

## Memory & UB gotchas
- Watch for: dangling pointers, use-after-free, buffer overruns, uninitialized reads, signed overflow.
- Enable sanitizers + `-Wall -Wextra` before claiming a fix is correct.
- Integer overflow and `reinterpret_cast` between unrelated types are common UB sources.

## Testing & debugging
- Use `gdb` for debugging; `valgrind` (or ASan) for memory checks.
- Keep tests in a `test/` dir; CTest (`enable_testing()`, `add_test`) integrates with CMake.
