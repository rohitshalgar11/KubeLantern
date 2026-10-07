---
title: Segmentation fault (exit 139, SIGSEGV)
category: application-error
---
# Symptoms
The process dies with exit code 139 or logs `Segmentation fault (core dumped)`,
`SIGSEGV: segmentation violation`, or `fatal error: unexpected signal during runtime execution`.

# Why it happens
Native code accessed invalid memory: a bug in a native library or extension
(image processing, database drivers, ML libraries), a library compiled for another
CPU or libc (Alpine/musl vs glibc), or a stack overflow in native code.

# What to check
1. Did the image's base OS or a native dependency change recently?
2. Alpine-based images with libraries built for glibc (or the opposite) crash like this.
3. Does it happen on specific input? Reproduce locally with the same image.

# Fix
Roll back to the last working image, align native libraries with the base image
(use a glibc-based image such as Debian slim if a library needs glibc), and update
or pin the native dependency that crashes.
