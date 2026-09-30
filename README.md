# claude-pr-review

A Claude code review for every push to a pull request, run as a reusable
GitHub Actions workflow. Reviews happen in rounds. Each review comment carries
a findings ledger, and the next round reads it back: it settles the previous
round's findings against the new code first, then looks for new problems only
in the commits since. An author declines a finding by replying
`F3: by design, <reason>`, and a declined finding is not raised again. Earlier
rounds stay on the PR, collapsed as outdated.

This repository is the only copy of the review: the workflow, the ledger
script, the usage summarizer, the telemetry collector config and the
`github-pr-review` skill. Consumers call it; they do not vendor it.

## Adopting it

1. Add the secret `CLAUDE_CODE_OAUTH_TOKEN` to the repository (from
   `claude setup-token`). An organization secret shared with the repository
   works the same way.
2. Commit a caller as `.github/workflows/claude-pr-review.yml`. The callers in
   [`callers/`](callers/) are the exact files lattice-tools and igvfd commit;
   start from [`callers/lattice-tools.yml`](callers/lattice-tools.yml).

```yaml
name: Claude PR Review

on:
  pull_request:
    types: [opened, synchronize]

concurrency:
  group: claude-pr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  review:
    if: github.event.pull_request.head.repo.full_name == github.repository
    permissions:
      contents: read
      pull-requests: write
      issues: write
      checks: read
      statuses: read
    uses: Lattice-Data/claude-pr-review/.github/workflows/review.yml@v1
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

The `permissions` block is the part people get wrong. It must be on the
caller's job, and it must grant at least these: a called workflow can use
what its caller grants or less, never more, and a missing permission fails
the run before it starts. `issues: write` is for collapsing old reviews, and
`checks` and `statuses` are for `gh pr checks`. No `id-token` and no Claude
GitHub App are needed: the review runs on the job's own `github.token`.

The `if:` skips PRs from forks, which get no secrets. The workflow's own job
carries the same guard as well.

The first PR that adds or changes the caller is reviewed by the caller it
contains, since GitHub runs a PR's own version of its workflow files.

Don't give the review more reach through the repository's own Claude Code
settings. claude-code-action loads the base branch's `.claude/settings.json`
into the reviewer's session, and an allow rule there widens what the reviewer
may do.

## Inputs

| Input | Default | What it is for |
| - | - | - |
| `tooling_constraints` | `""` | Lines appended to the reviewer's TOOLING CONSTRAINTS, for what is true only of this repository, written as `- ` bullets. igvfd's names two dependencies the reviewer will see imported but cannot read. |
| `checkout_filter` | `blob:none` | git partial-clone filter for the PR checkout. Blobless fetches the history without file contents, which arrive on demand; `""` fetches everything. |
| `max_turns` | `120` | The reviewer's turn limit. Running out still posts whatever `review.md` held by then. |
| `effort` | `high` | The reviewer's effort level: `low`, `medium`, `high`, `xhigh` or `max`. |

The model is fixed here (`claude-opus-5-5`), not an input: a model change is a
change to the review, and it ships as a version.

## Versions

Consumers pin a tag, never `main`. There are two kinds:

- `v1.0.0`, `v1.0.1`, `v1.1.0`, ...: immutable, one per release.
- `v1`: moves to the newest `v1.x.y`. It only ever moves for a compatible
  change: nothing a caller passes stops working, and the permissions it
  grants stay enough.

A change that makes a caller edit its file (a new required input, a new
permission, a removed input) is `v2`, and `v1` stays where it was. Pinning a
full `v1.x.y` tag, or a commit SHA, instead of `v1` is the choice for a
repository that wants to take each change deliberately.

Writing to this repository is in effect writing to every consumer's review
job, which can write to their PRs and spends their Claude subscription. Keep
its writers few, and move tags only from `main`.

## What the reviewer can and cannot do

The reviewer is Claude Code, run unattended by claude-code-action with a
token that can write to the PR. What it reads comes partly from the PR
author, and a PR can carry third-party text (fixtures, vendored files, pasted
logs), so its permissions assume the reviewer may be steered:

- It reads files and searches (`grep`, `find`, `cat` and the other read-only
  commands) inside the checkout and its own skill file, and nowhere else: not
  `.git/` (git commands still work), and not the rest of the runner, such as
  the process environment, the staged scripts or the runner's own files.
- Its shell is `gh pr view`, `gh pr diff`, `gh pr checks`, `git show`,
  `git log` and `git diff`. A git command that writes a file (`--output`),
  names a path outside the checkout, or expands a `$` variable is denied, and
  so are `gh pr view`'s comment and review fields and `echo`. It cannot run
  code, tests or package managers, and `gh api` is not allowed.
- It writes `review.md` and `findings.json`, and nothing else.
- It reads what people with a role in the repository wrote on the PR, from a
  file the workflow prepares, and never anyone else's comments.

The workflow does not take the reviewer's word for the rest. The scripts
that run after the review, and the previous round's ledger, are checked
against digests taken before the reviewer started; the ledger script
validates everything the reviewer wrote; and a review that contains either
token is not posted.

These permissions were checked with `claude -p` (Claude Code 2.1.285) before
release. Reads and writes outside the fence, `.git/config` through `Read`,
`cat` and `grep`, git's `--output` and absolute, `../` and `~` paths, `$`
expansion, `echo`, `printf`, `python3`, `pip`, `gh api` and the `gh pr view`
comment and review fields were each tried and refused, and every allowed
command ran. The comment on
`claude_args` in [`review.yml`](.github/workflows/review.yml) says why each
rule is there. Change it and the TOOLING CONSTRAINTS in the prompt together;
a test fails when they disagree.

## Decisions not to revisit without new evidence

- **Docs-only pushes are reviewed too.** No `paths-ignore`. In
  Lattice-Data/lattice-tools#375, two README-only rounds cost $0.61 together
  and found a real contradiction in the README. And `paths-ignore` on
  `pull_request` is evaluated against every file the PR changes, not the
  push, so it would not skip docs-only pushes to a PR that also changes code.
- **Reviews post as `github-actions[bot]` with the job's token.** The Claude
  GitHub App's token would carry `contents: write`, need the app installed on
  every consumer, and skip the review on any PR that edits the caller.

## Cost

Measured on Lattice-Data/lattice-tools#375, seven rounds over two days in
September 2026: $3.77 in total, 2-4 minutes per round, ten findings, all
real. Nine were settled in round 6, each with a note on how it was checked;
the tenth was raised by one of the fixes. Each run posts its own usage report on the PR.
That PR ran lattice-tools' copy of the review, before this repository.

## Layout

```
.github/workflows/review.yml          the reusable workflow
.github/workflows/tests.yml           this repository's CI
.github/scripts/review_ledger.py      carries the findings from round to round
.github/scripts/summarize_claude_usage.py
.github/scripts/tests/                ledger tests, workflow tests, the step harness
.github/otel/collector-config.yaml    the throwaway telemetry collector
skills/github-pr-review/SKILL.md      the skill the reviewer follows
callers/                              each consumer's caller, as committed there
```

## Tests

```bash
cd .github/scripts && python -m pytest
```

Beyond the ledger tests, `test_workflow_steps.py` runs the workflow's own
shell steps against a stub `gh` with real `jq` and `git`: first rounds,
follow-up rounds, a rebased branch, a merge from the base branch, a decline
from a member the token sees as a contributor, failed fetches, and every
refusal in the post step. `test_workflow.py` pins what the workflow text must
keep saying. The step tests need `jq` and GNU coreutils (on macOS,
`brew install coreutils`); the shellcheck test needs `shellcheck`. Each of
those tests skips without its tool, and CI checks that the tools are there.

## Planned

The skill as a Claude Code plugin from this repository, so an interactive
`/github-pr-review` in any checkout uses the same text and the last local
copies can go.

## Licence

MIT; see [LICENSE](LICENSE).
