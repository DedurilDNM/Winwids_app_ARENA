---
name: debugging
description: Find the root cause of a failure systematically - reproduce, isolate, fix, verify. Use when tests fail, a build breaks or behaviour is wrong.
---

# Debugging

1. **Reproduce**: run the failing command with Bash and keep the exact error output. No fix before a reliable reproduction.
2. **Read the error**: the first error in the output, its file and line; read that code with Read.
3. **Isolate**: form one hypothesis at a time and test it (a print, a smaller input, a single test). Use Grep to find all callers of the broken code.
4. **Fix the cause**, not the symptom: no catching-and-ignoring, no skipped tests, no special cases for the test input.
5. **Verify**: re-run the reproduction and the whole test suite. Report what was wrong, the fix, and the command that now passes.
