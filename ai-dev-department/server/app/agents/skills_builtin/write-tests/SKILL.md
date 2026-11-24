---
name: write-tests
description: Write automated tests for new or changed code and run them (pytest, vitest/jest, playwright). Use when a task adds behaviour, fixes a bug, or asks for tests.
---

# Writing tests

- Find the project's test setup first (pytest.ini / pyproject, package.json scripts, existing tests folder) and follow its style. Do not add a new framework when one exists.
- For a bug fix: write the test that reproduces the bug first, run it, see it fail, then fix, then see it pass.
- Test behaviour through public functions or endpoints, not private details. One clear reason to fail per test; descriptive test names.
- Cover the edge cases that matter: empty input, wrong types, limits, errors from dependencies.
- Keep tests fast and independent: no real network, temporary folders, fixed random seeds.
- Run the whole suite with Bash at the end and report the exact command and the result (passed / failed counts). Never skip or delete a failing test to get green.
