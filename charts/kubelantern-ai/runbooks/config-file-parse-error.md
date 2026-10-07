---
title: Configuration file cannot be parsed - YAML or JSON syntax error, or a value of the wrong type
---
# Symptoms
- YAML: `yaml: line 12: mapping values are not allowed in this context`,
  `found character that cannot start any token`, `did not find expected key`
- JSON: `JSONDecodeError: Expecting ',' delimiter: line 4 column 5`,
  `SyntaxError: Unexpected token } in JSON at position 87`
- Values: `invalid boolean`, `strconv.Atoi: parsing "8080 ": invalid syntax`,
  `NumberFormatException: For input string: "30s"`, `invalid duration`

# Why it happens
A config file in a ConfigMap has a syntax error (tabs in YAML, wrong indentation,
a trailing comma in JSON), or an environment variable has a value of the wrong type
or with stray whitespace/quotes — common when values come from Helm templates.

# What to check
1. The exact file/variable and line in the error.
2. The rendered ConfigMap: `kubectl -n <ns> get configmap <cm> -o yaml`.
3. Environment values: `kubectl -n <ns> get deploy <name> -o jsonpath='{..env}'` — quotes, units, whitespace.

# Fix
Fix the syntax or value, validate config in CI (yamllint, a JSON schema, a dry run
of the app's config loader), and quote values in Helm templates with `| quote`.
