---
name: git-workflow
description: Git commit, branch, and PR workflow conventions
version: "1.0"
author: mini-agent
tags: [git, workflow, vcs]
---

# Git Workflow Skill

## Commit Messages

Use Conventional Commits format:

| Type       | Usage                                          |
| ---------- | ---------------------------------------------- |
| `feat`     | A new feature                                  |
| `fix`      | A bug fix                                      |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `docs`     | Documentation only changes                     |
| `test`     | Adding or updating tests                       |
| `chore`    | Maintenance tasks (deps, config, CI)           |

Format: `<type>(<scope>): <short summary>`
Example: `feat(auth): add OAuth2 login support`

Rules:
- Summary line ≤ 72 characters
- Use imperative mood: "add feature" not "added feature"
- Do not capitalize first letter of summary
- No period at the end

## Branch Naming

- Feature: `feat/<short-description>`
- Bugfix: `fix/<issue-number>-<short-description>`
- Release: `release/<version>`
- Hotfix: `hotfix/<short-description>`

## Before Commit Checklist

1. Run all tests and ensure they pass
2. Check for untracked files: `git status`
3. Review your diff: `git diff --staged`
4. Remove debug prints and temporary comments
5. Write a clear commit message following the format above
6. End commit message with: `Co-Authored-By: Claude <noreply@anthropic.com>`

## Pull Request Guidelines

1. **Title**: summarize the change in one line
2. **Body**: explain WHY (motivation), WHAT (changes), HOW (testing)
3. Link related issues with `Closes #123`
4. Keep PRs small — under 400 lines of diff when possible
5. Add screenshots for UI changes
