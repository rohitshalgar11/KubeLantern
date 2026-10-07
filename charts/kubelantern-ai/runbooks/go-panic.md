---
title: Go panic: nil pointer, index out of range
category: application-error
---
# Symptoms
- `panic: runtime error: invalid memory address or nil pointer dereference`
- `[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=...]`
- `panic: runtime error: index out of range [3] with length 3`
- `fatal error: concurrent map writes`
followed by `goroutine 1 [running]:` and a stack trace.

# Why it happens
A code bug: an unchecked nil value (often an error path, a missing config value or
an empty response), a slice index not checked, or a map used from several goroutines
without a lock.

# What to check
1. The first frames of the stack trace under `goroutine N [running]:` name the file and line.
2. Did it start with a release, or with new input (empty config, a new field, a null in data)?
3. `concurrent map writes`: shared maps need a mutex or `sync.Map`.

# Fix
Roll back if it started with a release, then fix the bug at the reported line
(check errors and nil values before use). This is the application team's code;
the cluster is not the cause.
