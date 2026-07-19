# Security Policy

## Supported versions

loopd is pre-1.0 and ships from `main`. Security fixes land on `main` and in the latest
release on PyPI. Please always reproduce on the newest release before reporting.

| Version | Supported |
|---------|-----------|
| latest `0.1.x` | ✅ |
| older | ❌ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **[Private Vulnerability Reporting](https://github.com/ruchirk22/loopd/security/advisories/new)**
(the *Report a vulnerability* button under the repository's **Security** tab). If that is
unavailable to you, open a minimal public issue that says only "security report — please open
a private channel," and a maintainer will follow up.

When reporting, include: affected version, a clear description, reproduction steps, and the
impact you believe it has. We aim to acknowledge reports within a few days and to keep you
updated as we work on a fix. Please give us a reasonable window to release a fix before any
public disclosure.

## Scope — what loopd's threat model already assumes

loopd runs a coding agent that **executes commands and edits files without asking for
approval on each step**. This is intentional and only safe inside a sandbox. The design,
the `bypassPermissions` trade-off, the gate-authorship trust boundary, and how to run loopd
safely (e.g. in a container) are documented in **[docs/security.md](docs/security.md)** — read
that first. Behavior that is a documented consequence of running an autonomous agent on your
own machine (rather than a flaw in loopd's own boundaries) is out of scope.

In scope: anything that lets loopd exceed its stated boundaries — e.g. a way for a plan or
handover to escape the intended verification rails, leak the user's credentials, or run
outside the target repository/sandbox in a way the docs say it should not.
