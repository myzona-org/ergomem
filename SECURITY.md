# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately, using:

- **GitHub private security advisories:** open one from this repo's Security tab
  ("Report a vulnerability").

Please don't open a public issue for a suspected vulnerability until it's been
triaged — see [Scope](#scope) below for cases that don't need private handling at all.

Include what you'd want to receive yourself: a minimal reproduction, the affected
version or commit, the environment you ran it in (from source vs. the Docker image),
and what you expected versus what happened.

### What to expect

- **Acknowledgement within 5 business days.** This is a single-maintainer project —
  that's the honest number, not a marketing one.
- After acknowledgement, you'll get an initial assessment (confirmed, not a
  vulnerability, or more information needed) and, if confirmed, a rough sense of
  timeline. There's no fixed SLA for a fix — severity and complexity both matter — but
  you won't be left wondering whether the report landed.
- Coordinated disclosure is welcome. If you'd like credit, say so in your report and how
  you'd like to be named.

## Supported versions

| What | Supported |
|---|---|
| The current `main` branch, run from source | ✅ |
| A Docker image you build yourself from the current source | ✅ |
| A previously published Docker image | ❌ (redistributing a built image is on hold — see [THIRD-PARTY-MODELS.md](THIRD-PARTY-MODELS.md)) |
| A fork or a modified build | ❌ (report to whoever maintains that fork) |

There's no long-term-support branch. This is one person maintaining one moving line —
if that changes, this file will say so.

## Scope

**In scope:** anything that lets someone do more than the documented surface allows
without the right credentials — an authentication bypass, reading or writing another
org's or project's data without the right scope, injection through any input path, a
crash or resource exhaustion triggered by a single request, or a real vulnerability in a
dependency this project actually uses at the version it's pinned to.

**Out of scope — by design, not oversight:**

- **`force_exception`.** A caller with write access can supply `force_exception` on a
  guarded write to store a claim over an active contradiction warning. This is a
  documented escape hatch for someone who has looked at the conflict and decided to
  proceed anyway — not a flaw in the check it steps around. Finding that you can supply
  it is not a bug; finding a way to trigger the same effect *without* supplying it would
  be.
- **The `/ingest` endpoint.** It skips the contradiction check entirely, on purpose — it
  is the fast path for facts that don't need one. Writing through it and having no
  contradiction fire is the documented behavior, not a bypass.
- **The practical read on both of the above:** the write guard this project ships is an
  **assistive integrity control, not tamper resistance.** It's built to catch a
  well-intentioned caller contradicting themselves by accident, not to resist an
  adversarial one who already has write access. The security boundary is *who holds a
  valid token*, not *whether the contradiction check can be defeated by someone who
  does.* A report that amounts to "I bypassed the check using a documented escape
  hatch" will get pointed back at this section rather than a fix.
- **Running with no token.** With no `ERGO_API_TOKEN` set, a loopback-only bind
  (`127.0.0.1`, `::1`, or `localhost`) stays open and logs a warning — documented
  dev/test behavior. A non-loopback bind refuses to start at all without a token,
  unless the operator has deliberately set `ERGO_INSECURE_NO_AUTH=1` — that variable
  exists specifically so someone can choose to run open on purpose (see
  [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)). If you set that yourself and then
  find the API open to the network, that's the documented outcome, not a
  vulnerability. Finding an unauthenticated path into an instance that did **not** set
  it, or that never chose a loopback bind, is in scope.

If you're not sure which side of that line something falls on, report it anyway — an
easy "that's by design, here's why" reply costs a maintainer far less than a real issue
going unreported.
