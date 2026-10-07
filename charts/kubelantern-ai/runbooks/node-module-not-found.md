---
title: Node.js: Cannot find module
category: application-error
---
# Symptoms
- `Error: Cannot find module 'express'`
- `Error: Cannot find module '/usr/src/app/dist/index.js'`
- `Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'x' imported from /app/...`
followed by `code: 'MODULE_NOT_FOUND'` and a stack.

# Why it happens
`node_modules` is missing or incomplete in the image (dependency listed under
`devDependencies` but installed with `--omit=dev`, `.dockerignore` excluding needed
files), the build output (`dist/`) was not copied, or the start command points to
the wrong file.

# What to check
1. Is the package in `dependencies` (not only `devDependencies`)?
2. Dockerfile: `npm ci` runs, and the build output is copied into the final stage.
3. The start command (`node dist/index.js`, `npm start`) matches the built file paths.
4. ESM vs CommonJS: `"type": "module"` changes how imports resolve (file extensions required).

# Fix
Move runtime packages to `dependencies`, copy the build output and `node_modules`
into the final image stage, and fix the start path. Rebuild and redeploy.
