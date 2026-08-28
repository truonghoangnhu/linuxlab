# ADR-012 — Guest-first Identity & Contextual Authorization

**Status:** Accepted

## Context

LinuxLab is primarily a self-learning lab and must not make account management a barrier, while later classrooms and exams need stronger identity and scoped permissions.

## Decision

Allow learning without an account. Separate Account, Learner, AnonymousLearner, LearningContext, and later ExamCandidate concepts. Authorization is role + scope/context, not fixed StudentUser/TeacherUser types.

Node/service principals have their own identities. Central password databases are not copied to Node. Anonymous progress is local by default and can be explicitly claimed after registration.

## Consequences

- The initial learner experience remains frictionless.
- Later school/teacher/exam roles can be added without replacing the account schema.
- Offline authentication can use limited-scope credentials instead of Central passwords.
