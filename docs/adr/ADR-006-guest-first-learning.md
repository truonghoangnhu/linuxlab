# ADR-006 — Guest-First Learning

**Status:** Accepted

## Context

LinuxLab is primarily a self-learning lab rather than a school-management LMS. Requiring registration before a learner can read lessons, try basic activities, or explore the platform would add friction and unnecessary user-management overhead.

## Decision

Allow learning to begin without a registered account.

Anonymous/guest learning state is local by default and is not automatically synchronized to Central. Registration becomes useful when a learner wants durable cross-device progress, bookmarks, achievements, formal identity, or later exam participation.

When a guest registers, LinuxLab may explicitly claim selected local progress into a Learner identity rather than silently uploading all anonymous browser history.

This ADR establishes the product-level choice. ADR-012 refines Account, Learner, ExamCandidate, contextual authorization, Node/service identities, and offline credentials.

## Consequences

- The first-use experience remains lightweight.
- LinuxLab collects less personal data by default.
- Guest progress requires a clear local storage and claim strategy.
- Formal exams can still require stronger identity assurance independently of normal self-study.
