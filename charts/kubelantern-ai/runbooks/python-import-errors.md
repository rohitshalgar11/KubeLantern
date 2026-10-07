---
title: Python ModuleNotFoundError or ImportError at startup
category: application-error
---
# Symptoms
- `ModuleNotFoundError: No module named 'requests'`
- `ImportError: cannot import name 'X' from 'package'`
- `ImportError: libpq.so.5: cannot open shared object file: No such file or directory`
preceded by `Traceback (most recent call last):`.

# Why it happens
A package is missing from the image (not in requirements, or installed into a
different Python/virtualenv than the one that runs), a package upgrade removed or
moved a name, or a native library the package needs is not installed in the base image.

# What to check
1. Is the package in `requirements.txt` / `pyproject.toml`, with a pinned version?
2. Does the image install into the same interpreter that runs (`python -m pip`, venv PATH)?
3. For `cannot open shared object file`: the OS package is missing (e.g. `libpq5`), common with slim/Alpine images.
4. `cannot import name`: a dependency was upgraded; check its changelog and pin it.

# Fix
Add and pin the dependency, install it into the interpreter that runs the app,
install the missing OS library in the Dockerfile, and rebuild. Pinning versions
(a lock file) prevents surprise upgrades between builds.
