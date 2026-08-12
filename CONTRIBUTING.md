# Contributing to Ergo

Thanks for the interest. Please read this before opening a pull request — the mechanics
here are unusual for a source-available repo, and skipping this section will surprise you
later.

## How this repo is published

This repository is **generated** from a private engine tree. The sync only runs one
way: private to public. There is no path for a change made here to flow back into the
source tree on its own.

That has a direct consequence for pull requests:

**A merged contribution does not merge your PR — it closes it.** When a contribution
here is accepted, it is ported into the private tree with your authorship preserved
(`git am`, or a `Co-Authored-By:` trailer if the commit needs rework), applied there,
and the next regeneration republishes this repo from that updated source. The pull
request you opened is then closed as ported, not merged in the usual GitHub sense.

**Please read that closure as intended: it is not a rejection.** An unexplained closure
would read as contempt, so an accepted PR should also get a comment saying what
happened to the change. If that comment is missing, ask — that's a process gap, not
your answer.

The reason for this topology is structural, not a preference for control: the private
tree also contains hosted-service code that must never ship here, and generating from
one source is the only way to keep that boundary from drifting file by file over time.
It also means that, today, a contribution here cannot become the canonical copy of that
code — the private tree stays canonical.

## Issues are the better first move

Especially for anything that isn't a small, obvious fix, **open an issue before writing
code.** A design discussion in an issue costs you nothing if the direction turns out to
be wrong; a pull request that has to be reworked before it can be ported costs you a
rewrite. Bug reports and feature requests are also useful entirely on their own — you
don't need a patch attached for an issue to be worth filing.

## What a good contribution looks like

- **A bug fix starts with a failing test.** Reproduce the problem under `tests/` first,
  confirm it's red, then fix it, then confirm it's green. A fix with no test that would
  have caught the regression is likely to regress again.
- **Match the existing style.** This project is deliberately light on dependencies and
  avoids frameworks where the standard library does the job — a new dependency needs a
  real justification, not just convenience.
- **Keep the diff scoped.** A pull request that also reformats unrelated code, renames
  things in passing, or "cleans up while I'm in here" is harder to port and harder to
  review. Something else worth fixing is worth its own issue.
- **The full test suite passes before you open the PR** — see below.

## Running the test suite

```bash
git clone https://github.com/myzona-org/ergomem.git
cd ergomem
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

This installs the full engine + MCP-adapter + test surface and runs everything offline
except a one-time embedder-model download, which then caches locally — see
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for the exact cache step and the full
local-setup walkthrough.

## Before this repo republishes your change

Once a contribution is ported inward, the maintainer regenerates this repository from
the private source and runs a verification pass — the retained suite, a fresh install,
a boot of the image, and a sweep for anything that shouldn't be public — before anything
goes out again. That step runs on the private side and isn't something you need to run
yourself; it's mentioned here so a gap between "ported" and "shows up in this repo"
isn't a mystery.

## Authorship in code and commits

Credit real people. Commit messages and code comments should never attribute a change,
a decision, or a piece of authorship to an AI assistant or coding tool — whoever
directed the work is the author, regardless of what tools they used to get there. This
applies to contributions here the same way it applies to the rest of this codebase.

## License

By contributing, you agree your contribution may be used under this project's license —
see [`LICENSE`](LICENSE).
