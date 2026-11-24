---
name: code-review
description: Review a change before it is merged - correctness, security, readability, tests. Use when asked to review code, a diff or a finished task.
---

# Code review

1. Find what changed: `git diff` (or the files named in the task). Read each changed file around the change, not only the lines.
2. Check, in this order:
   - **Correctness**: wrong conditions, off-by-one, missing awaits, unhandled errors, race conditions, broken edge cases (empty input, None, unicode, large data).
   - **Security**: user input reaching SQL, shell, file paths or HTML; secrets in code or logs; missing permission checks.
   - **Tests**: does a test cover the new behaviour? Would it fail without the change? Run the test suite with Bash if there is one.
   - **Readability**: names, dead code, duplication that will drift.
3. Report each finding as: file:line - what is wrong - a concrete failure scenario - the fix. Most severe first. Say plainly when there is nothing to fix.
4. Do not rewrite working code to your taste; propose changes only where there is a real problem.
