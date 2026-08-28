# ADR-013 — Shared Assessment & Versioned Exam Architecture

**Status:** Accepted

## Context

Quiz, practice, diagnostics, and future exams should reuse one assessment model rather than growing separate question engines.

## Decision

Use a shared Question Bank with immutable QuestionVersions and versioned scoring policies. Quiz is an Assessment Activity provider. The later Exam Engine orchestrates sessions, candidates, timing, randomization, submissions, and audit while reusing Assessment and other Activity providers.

Coding and Terminal remain Activities rather than being forced into generic Question types. Final exam submissions are sealed immutable records.

## Consequences

- One Question can serve multiple learning contexts with different policies.
- Historical attempts remain reproducible.
- A future exam may combine MCQ, short answer, Python, and Terminal activities under one orchestration layer.
