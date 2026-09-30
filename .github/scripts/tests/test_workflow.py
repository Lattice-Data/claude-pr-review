"""What review.yml and the callers must keep saying, checked on their text.

Each of these was a defect, or the fix for one, and each is the kind a
refactor undoes without anything failing until a real PR runs: a post step
that falls back to the implicit success(), a permission rule that widens, a
deny rule that shell-quote empties, a prompt that describes last month's
allowlist.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess

import pytest
import yaml
from workflow_harness import REPO, job_steps, load_workflow

CALLERS = sorted((REPO / "callers").glob("*.yml"))
PERMISSIONS = {
    "contents": "read",
    "pull-requests": "write",
    "issues": "write",
    "checks": "read",
    "statuses": "read",
}
ALLOWED = [
    "Edit(/review.md)",
    "Edit(/findings.json)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(git show:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
]
DENIED = [
    "Bash(gh pr view *comments*)",
    "Bash(gh pr view *reviews*)",
    "Bash(gh pr view *Reviews*)",
    "Bash(gh pr view *-c*)",
    "Bash(git * --output*)",
    "Bash(git *--no-index*)",
    "Bash(git * /*)",
    "Bash(git * ../*)",
    "Bash(git * ~*)",
    "Bash(git *$*)",
    "Bash(echo *)",
    "Read(.git/**)",
]


@pytest.fixture(scope="module")
def workflow():
    return load_workflow()


@pytest.fixture(scope="module")
def steps(workflow):
    return job_steps(workflow)


def rendered(text: str) -> str:
    """text as the action receives it. GitHub fills in each ${{ expr }} before
    the action sees a character; here it becomes <expr>, which has no $ and
    no space for the checks below to trip on."""
    return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", r"<\1>", text)


@pytest.fixture(scope="module")
def claude_args(steps):
    """claude_args as the CLI receives it: flag -> value, lists comma-split."""
    tokens = shlex.split(rendered(steps["claude"]["with"]["claude_args"]))
    args = {}
    for flag, value in zip(tokens[::2], tokens[1::2]):
        assert flag.startswith("--"), tokens
        args[flag] = value.split(",") if flag.endswith("Tools") else value
    return args


def test_posting_survives_a_reviewer_that_ran_out_of_turns(steps):
    """claude-code-action fails its step on error_max_turns after the reviewer
    wrote review.md; an implicit success() here would throw that review away."""
    for name in ("post", "Collapse superseded reviews"):
        assert steps[name]["if"].startswith("${{ !cancelled() && "), name


def test_the_job_asks_for_what_the_callers_grant_and_nothing_more(workflow):
    job = workflow["jobs"]["review"]
    assert job["permissions"] == PERMISSIONS
    for caller in CALLERS:
        caller_job = yaml.safe_load(caller.read_text())["jobs"]["review"]
        assert caller_job["permissions"] == PERMISSIONS, caller.name


@pytest.mark.parametrize("caller", CALLERS, ids=lambda p: p.stem)
def test_a_caller_pins_a_tag_and_guards_against_forks(caller):
    doc = yaml.safe_load(caller.read_text())
    on = doc[True]  # PyYAML reads the key `on` as a boolean
    assert on["pull_request"]["types"] == ["opened", "synchronize"]
    assert doc["concurrency"]["cancel-in-progress"] is True
    job = doc["jobs"]["review"]
    assert (
        job["uses"] == "Lattice-Data/claude-pr-review/.github/workflows/review.yml@v1"
    )
    assert (
        job["if"]
        == "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert job["secrets"] == {
        "CLAUDE_CODE_OAUTH_TOKEN": "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}"
    }


def test_the_tooling_comes_from_the_called_workflows_own_commit(workflow, steps):
    """github.workflow_sha is the caller's commit, in the caller's repository.
    (The parsed workflow has no comments, which name it to say why not.)"""
    checkout = steps["Check out the review tooling"]["with"]
    assert checkout["repository"] == "${{ job.workflow_repository }}"
    assert checkout["ref"] == "${{ job.workflow_sha }}"
    assert not re.search(r"github\.workflow_(sha|ref)\b", json.dumps(workflow))


def test_every_file_the_staging_step_copies_exists(steps):
    for path in re.findall(r"\.review-tooling/(\S+)", steps["tooling"]["run"]):
        assert (REPO / path).is_file(), path


def test_the_reviewer_is_handed_github_token_not_the_app_token(steps):
    assert steps["claude"]["with"]["github_token"] == "${{ github.token }}"


def test_the_reviewer_has_no_bare_file_rule(claude_args):
    """A bare Read or Write reaches the whole filesystem: the staged scripts,
    the process environment, .git/config."""
    assert claude_args["--allowedTools"] == ALLOWED
    assert claude_args["--disallowedTools"] == DENIED
    for rule in claude_args["--allowedTools"]:
        assert rule not in ("Read", "Write", "Edit", "Grep", "Glob"), rule


def test_the_reviewer_reads_the_skill_from_outside_dot_claude(steps, claude_args):
    assert claude_args["--add-dir"] == "<runner.temp>/skill"
    assert "Read the file ${{ runner.temp }}/skill/SKILL.md" in steps["claude"]["with"][
        "prompt"
    ].replace("\n", " ")


def test_no_dollar_sign_in_claude_args_is_left_to_shell_quote(steps):
    """claude-code-action splits claude_args with shell-quote, which expands $
    outside single quotes: an unquoted or double-quoted "Bash(git *$*)" arrives
    as "Bash(git *", taking the next rule with it."""
    quote = None
    for char in rendered(steps["claude"]["with"]["claude_args"]):
        if quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
        elif char == "$" and quote != "'":
            pytest.fail("a $ in claude_args is outside single quotes")


def test_the_prompt_names_exactly_the_shell_commands_the_reviewer_is_allowed(
    steps, claude_args
):
    """The reviewer cannot see --allowedTools, only the prompt's account of it,
    so the two must change together."""
    prompt = " ".join(steps["claude"]["with"]["prompt"].split())
    sentence = re.search(r"The shell commands available are (.*?), plus", prompt)
    assert sentence, "the TOOLING CONSTRAINTS no longer list the shell commands"
    named = re.findall(r"`([^`]+)`", sentence.group(1))
    allowed = [
        rule[len("Bash(") : -len(":*)")]
        for rule in claude_args["--allowedTools"]
        if rule.startswith("Bash(")
    ]
    assert named == allowed


def test_python_runs_isolated_after_the_tooling_is_staged(workflow):
    """-I: no PYTHON* variable from the environment, whatever wrote to
    GITHUB_ENV before the step."""
    runs = [
        step["run"] for step in workflow["jobs"]["review"]["steps"] if "run" in step
    ]
    calls = [m for run in runs for m in re.finditer(r"\bpython3\b.{0,4}", run)]
    assert calls, "no python3 call found; the check below would pass vacuously"
    for call in calls:
        assert call.group(0).startswith("python3 -I "), call.group(0)


def test_every_run_block_passes_shellcheck(workflow, tmp_path):
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    scripts = []
    for n, step in enumerate(workflow["jobs"]["review"]["steps"], start=1):
        if "run" in step:
            script = tmp_path / f"{n:02d}.sh"
            # ${{ }} becomes a plain word, the way GitHub hands the shell a
            # plain string there.
            script.write_text(
                "#!/usr/bin/env bash\n"
                + re.sub(r"\$\{\{.*?\}\}", "GHA_EXPR", step["run"], flags=re.S)
            )
            scripts.append(str(script))
    done = subprocess.run(
        [shellcheck, "--shell=bash", "--format=gcc", *scripts],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr
