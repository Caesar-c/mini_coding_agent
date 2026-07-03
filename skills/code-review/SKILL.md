---
name: code-review
description: Systematic code review checklist for correctness, style, and security
version: "1.0"
author: mini-agent
tags: [review, quality, best-practices]
---

# Code Review Skill

## Review Process

Follow this checklist systematically. Don't just look for bugs — review for maintainability too.

## 1. Correctness

- [ ] Logic errors: off-by-one, wrong operator, missing edge cases
- [ ] Null/undefined handling: are all nullable paths covered?
- [ ] Error handling: are exceptions caught at the right level?
- [ ] Concurrency: race conditions, deadlocks, shared mutable state
- [ ] Resource leaks: unclosed files, connections, or processes
- [ ] Boundary conditions: empty lists, zero values, max values

## 2. Design & Maintainability

- [ ] Single responsibility: each function/class does one thing
- [ ] Naming: variables and functions have clear, descriptive names
- [ ] Duplication: no copy-pasted logic that should be extracted
- [ ] Complexity: no function longer than ~50 lines or deeper than 3 nesting levels
- [ ] Comments: explain WHY, not WHAT (the code explains what)
- [ ] API design: interfaces are minimal and intuitive

## 3. Security

- [ ] Input validation: user inputs are sanitized before use
- [ ] Authentication: endpoints check auth before processing
- [ ] Secrets: no hardcoded API keys, passwords, or tokens
- [ ] SQL injection: queries use parameterized statements
- [ ] Path traversal: file operations validate paths stay within bounds
- [ ] Dependencies: no known vulnerable packages

## 4. Testing

- [ ] Test coverage: new logic has corresponding tests
- [ ] Edge cases: boundary values, empty inputs, error paths
- [ ] Test quality: assertions are meaningful (not just "no crash")
- [ ] Test isolation: tests don't depend on execution order or external state

## 5. Performance (when relevant)

- [ ] N+1 queries: database access in loops
- [ ] Unnecessary allocations: large objects created in hot paths
- [ ] Missing indexes: queries on unindexed columns
- [ ] Caching opportunities: expensive computations that could be memoized

## Output Format

Structure your review as:

1. **Summary**: one-paragraph overall assessment
2. **Critical issues**: bugs or security problems that must be fixed
3. **Suggestions**: improvements that would make the code better
4. **Positive notes**: what was done well (always include at least one)
