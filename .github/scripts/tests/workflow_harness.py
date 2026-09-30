"""Run review.yml's own shell steps offline, against a stub gh.

Most of the review's logic lives in run: blocks: which comments count as
previous rounds, what the round context says after a rebase or a merge from
the base branch, whose replies reach the reviewer, and what the post step
refuses to run or to post. None of it runs until a real PR does. These helpers
run it here. Each step's run: block is taken from the workflow as written, its
${{ }} expressions are filled in from a context the test builds, and it runs
under `bash -eo pipefail`, as GitHub runs it, in a scratch workspace with real
git history. gh is a stub on PATH: `gh api` answers from canned JSON piped
through the real jq with the step's own --jq filter, and everything a step
would post is recorded instead.

What this proves is the steps' shell, jq and git logic. It proves nothing
about GitHub's API, claude-code-action, or the reviewer.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "review.yml"
LEDGER_SCRIPT = REPO / ".github" / "scripts" / "review_ledger.py"

REPOSITORY = "Lattice-Data/example-consumer"
PR_NUMBER = "7"
BASE_REF = "main"
GITHUB_TOKEN = "ghs_EXAMPLEFAKETOKEN0000000000000000000"
CLAUDE_TOKEN = "sk-ant-oat01-EXAMPLE-FAKE-TOKEN-0000"
TOOLING_SHA = "0123456789abcdef0123456789abcdef01234567"

# The steps use GNU date (-d) and GNU sha256sum (--strict). The runner has
# them; on macOS they come from Homebrew's coreutils, unprefixed in gnubin.
GNU_BIN_DIRS = [
    Path("/opt/homebrew/opt/coreutils/libexec/gnubin"),
    Path("/usr/local/opt/coreutils/libexec/gnubin"),
]

GH_STUB = r'''#!/usr/bin/env python3
"""A stand-in for gh.

`gh api` answers from $GH_STUB_DIR/api.json, through the real jq when the
caller passes --jq; `gh pr comment` and every POST are recorded under posted/.
Every invocation is logged to calls.jsonl.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

stub = Path(os.environ["GH_STUB_DIR"])
args = sys.argv[1:]
with (stub / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
posted = stub / "posted"
posted.mkdir(exist_ok=True)


def record(kind, text):
    count = len(list(posted.iterdir()))
    (posted / f"{count:02d}-{kind}").write_text(text)


if args[:2] == ["pr", "comment"]:
    record("pr-comment.md", Path(args[args.index("--body-file") + 1]).read_text())
    sys.exit(0)
if not args or args[0] != "api":
    print(f"gh stub: unsupported command {args}", file=sys.stderr)
    sys.exit(2)

endpoint, jq_filter, method, fields, paginate = None, None, "GET", [], False
rest = iter(args[1:])
for arg in rest:
    if arg == "--paginate":
        paginate = True
    elif arg == "--silent":
        pass
    elif arg in ("--jq", "-q"):
        jq_filter = next(rest)
    elif arg in ("-X", "--method"):
        method = next(rest)
    elif arg in ("-f", "-F", "--field", "--raw-field"):
        fields.append(next(rest))
    elif endpoint is None:
        endpoint = arg
    else:
        print(f"gh stub: unexpected argument {arg!r}", file=sys.stderr)
        sys.exit(2)

api = json.loads((stub / "api.json").read_text())
answer = api.get(f"{method} {endpoint}", api.get(endpoint) if method == "GET" else None)
if endpoint == "graphql" and answer is None:
    answer = {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": True}}}}
if isinstance(answer, dict) and "__fail__" in answer:
    print(answer["__fail__"], file=sys.stderr)
    sys.exit(1)
if method == "POST" or endpoint == "graphql":
    # -F name=@file sends the file's content, so that is what is recorded.
    sent = {}
    for field in fields:
        name, _, value = field.partition("=")
        sent[name] = Path(value[1:]).read_text() if value.startswith("@") else value
    record(f"{method.lower()}-{endpoint.replace('/', '_')}.json", json.dumps(sent))
    if method == "POST":
        sys.exit(0)
if answer is None:
    print(f"HTTP 404: Not Found ({endpoint})", file=sys.stderr)
    sys.exit(1)
pages = answer["__pages__"] if isinstance(answer, dict) and "__pages__" in answer else [answer]
if not paginate:
    pages = pages[:1]
for page in pages:
    if jq_filter is None:
        print(json.dumps(page))
        continue
    # gh prints strings raw and everything else as JSON, like jq -r.
    done = subprocess.run(["jq", "-r", jq_filter], input=json.dumps(page), text=True)
    if done.returncode != 0:
        sys.exit(done.returncode)
'''


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def job_steps(workflow: dict | None = None) -> dict[str, dict]:
    """The review job's steps, by id and by name."""
    workflow = workflow or load_workflow()
    steps = {}
    for step in workflow["jobs"]["review"]["steps"]:
        steps[step["name"]] = step
        if "id" in step:
            steps[step["id"]] = step
    return steps


def load_ledger_module():
    spec = importlib.util.spec_from_file_location("review_ledger", LEDGER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta) -> str:
    return stamp(datetime.now(timezone.utc) - timedelta(**delta))


def parse_outputs(text: str) -> dict[str, str]:
    """GITHUB_OUTPUT's two forms: name=value, and name<<DELIM ... DELIM."""
    outputs: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        heredoc = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)<<(.+)$", line)
        if heredoc:
            name, delimiter = heredoc.groups()
            end = lines.index(delimiter, i + 1)
            outputs[name] = "\n".join(lines[i + 1 : end])
            i = end + 1
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            outputs[name] = value
        i += 1
    return outputs


@dataclass
class StepResult:
    returncode: int
    outputs: dict[str, str]
    stdout: str
    stderr: str

    @property
    def log(self) -> str:
        return self.stdout + self.stderr


class Harness:
    """One PR's workspace, runner temp directory, API fixtures and step context."""

    def __init__(self, root: Path):
        self.root = root
        self.workspace = root / "workspace"
        self.runner_temp = root / "runner-temp"
        self.stub_dir = root / "gh-stub"
        self.bin_dir = root / "bin"
        for directory in (
            self.workspace,
            self.runner_temp,
            self.stub_dir,
            self.bin_dir,
        ):
            directory.mkdir(parents=True)
        gh = self.bin_dir / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.api: dict = {}
        self.workflow = load_workflow()
        self.steps = job_steps(self.workflow)
        self.ctx: dict[str, str] = {
            "github.repository": REPOSITORY,
            "github.event.pull_request.number": PR_NUMBER,
            "github.event.pull_request.base.ref": BASE_REF,
            "github.token": GITHUB_TOKEN,
            "github.run_id": "1",
            "github.run_attempt": "1",
            "secrets.CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_TOKEN,
            "runner.temp": str(self.runner_temp),
            "inputs.effort": "high",
            "inputs.max_turns": "120",
            "inputs.checkout_filter": "blob:none",
            "inputs.tooling_constraints": "",
            "job.workflow_repository": "Lattice-Data/claude-pr-review",
            "job.workflow_sha": TOOLING_SHA,
            "job.workflow_ref": "Lattice-Data/claude-pr-review/.github/workflows/review.yml@refs/tags/v1",
        }
        self.git("init", "-q", "-b", BASE_REF)

    # -- git --------------------------------------------------------------

    def git_env(self) -> dict[str, str]:
        # Nothing from the developer's own git config (signing, hooks, a
        # default branch) may leak into the scratch repository.
        return {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Example Author",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_COMMITTER_NAME": "Example Author",
            "GIT_COMMITTER_EMAIL": "author@example.com",
        }

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args],
            cwd=self.workspace,
            env=self.git_env(),
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, name: str) -> str:
        """A commit that adds one file, returning its SHA."""
        (self.workspace / f"{name}.txt").write_text(f"{name}\n")
        self.git("add", f"{name}.txt")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def head(self) -> str:
        sha = self.git("rev-parse", "HEAD")
        self.ctx["github.event.pull_request.head.sha"] = sha
        return sha

    # -- API fixtures -------------------------------------------------------

    def endpoint(self, kind: str) -> str:
        return {
            "issue_comments": f"repos/{REPOSITORY}/issues/{PR_NUMBER}/comments",
            "reviews": f"repos/{REPOSITORY}/pulls/{PR_NUMBER}/reviews",
            "review_comments": f"repos/{REPOSITORY}/pulls/{PR_NUMBER}/comments",
            "pull": f"repos/{REPOSITORY}/pulls/{PR_NUMBER}",
        }[kind]

    def set_api(self, kind: str, value) -> None:
        self.api[self.endpoint(kind)] = value

    def set_permission(self, login: str, role: str | None) -> None:
        """What the collaborator permission API says of login; None is a 404."""
        key = f"repos/{REPOSITORY}/collaborators/{login}/permission"
        if role is None:
            self.api.pop(key, None)
        else:
            self.api[key] = {"permission": role, "role_name": role}

    def previous_review(
        self,
        *,
        round_no: int,
        head_sha: str,
        findings: list[dict],
        created_at: str,
        comments_fetched_at: str | None = None,
        node_id: str = "IC_previous",
        comment_id: int = 100,
    ) -> dict:
        """A review comment as an earlier round posted it: embed's own output."""
        ledger = load_ledger_module()
        record = {
            "schema": ledger.SCHEMA,
            "round": round_no,
            "head_sha": head_sha,
            "findings": findings,
        }
        if comments_fetched_at:
            record["comments_fetched_at"] = comments_fetched_at
        body = ledger.assemble(
            self.workflow["env"]["REVIEW_MARKER"],
            record,
            f"## Review, round {round_no}\n",
            ledger.footer_lines(round_no, head_sha, findings, [], []),
        )
        return comment(
            body,
            login="github-actions[bot]",
            association="NONE",
            created_at=created_at,
            bot=True,
            node_id=node_id,
            comment_id=comment_id,
        )

    # -- running steps --------------------------------------------------------

    def render(self, text: str) -> str:
        def value(match: re.Match) -> str:
            expr = match.group(1).strip()
            if expr in self.ctx:
                return self.ctx[expr]
            # GitHub renders an output that was never set as "".
            if re.fullmatch(r"steps\.[a-z_]+\.outputs\.[a-z_0-9]+", expr):
                return ""
            raise KeyError(f"the harness has no value for ${{{{ {expr} }}}}")

        return re.sub(r"\$\{\{(.*?)\}\}", value, text, flags=re.S)

    def env(self, step: dict) -> dict[str, str]:
        path = [str(self.bin_dir)]
        path += [str(d) for d in GNU_BIN_DIRS if d.is_dir()]
        path.append(os.environ.get("PATH", "/usr/bin:/bin"))
        env = {
            **self.git_env(),
            "PATH": os.pathsep.join(path),
            "GH_STUB_DIR": str(self.stub_dir),
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "GITHUB_ENV": str(self.root / "github-env"),
            "GITHUB_STEP_SUMMARY": str(self.root / "step-summary"),
        }
        for key, value in self.workflow.get("env", {}).items():
            env[key] = self.render(str(value))
        for key, value in (step.get("env") or {}).items():
            env[key] = self.render(str(value))
        return env

    def shell(self, step: dict) -> list[str]:
        """The command GitHub runs the step with, from the workflow's own
        shell setting, so that dropping `shell: bash` loses pipefail here too."""
        defaults = self.workflow["jobs"]["review"].get("defaults") or {}
        shell = step.get("shell") or (defaults.get("run") or {}).get("shell")
        if shell == "bash":
            return ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
        if shell is None:
            # GitHub's default on Linux when bash is present.
            return ["bash", "-e"]
        raise ValueError(f"the harness does not know the shell {shell!r}")

    def run(self, key: str) -> StepResult:
        step = self.steps[key]
        (self.stub_dir / "api.json").write_text(json.dumps(self.api))
        output_file = self.root / "github-output"
        output_file.write_text("")
        script = self.root / "step.sh"
        script.write_text(self.render(step["run"]))
        done = subprocess.run(
            [*self.shell(step), str(script)],
            cwd=self.workspace,
            env=self.env(step),
            capture_output=True,
            text=True,
        )
        outputs = parse_outputs(output_file.read_text())
        if "id" in step:
            for name, value in outputs.items():
                self.ctx[f"steps.{step['id']}.outputs.{name}"] = value
        return StepResult(done.returncode, outputs, done.stdout, done.stderr)

    def stage_tooling(self) -> StepResult:
        """The tooling checkout, as actions/checkout would leave it, then staging."""
        tooling = self.workspace / ".review-tooling"
        for relative in (
            ".github/scripts/review_ledger.py",
            ".github/scripts/summarize_claude_usage.py",
            ".github/otel/collector-config.yaml",
            "skills/github-pr-review/SKILL.md",
        ):
            target = tooling / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / relative, target)
        return self.run("tooling")

    def write_review(self, review: str, findings: list | None = None) -> None:
        """What the reviewer leaves behind: review.md, and findings.json if given."""
        (self.workspace / "review.md").write_text(review)
        if findings is not None:
            (self.workspace / "findings.json").write_text(json.dumps(findings))

    # -- what the steps did ---------------------------------------------------

    def posted(self, kind: str = "pr-comment.md") -> list[str]:
        directory = self.stub_dir / "posted"
        if not directory.is_dir():
            return []
        return [
            p.read_text() for p in sorted(directory.iterdir()) if p.name.endswith(kind)
        ]

    def calls(self) -> list[list[str]]:
        log = self.stub_dir / "calls.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


def comment(
    body: str,
    *,
    login: str,
    association: str,
    created_at: str,
    bot: bool = False,
    node_id: str | None = None,
    comment_id: int = 1,
) -> dict:
    """An issue comment as GitHub's API returns it, with the fields the steps read."""
    return {
        "id": comment_id,
        "node_id": node_id or f"IC_{comment_id}",
        "created_at": created_at,
        "body": body,
        "author_association": association,
        "user": {"login": login, "type": "Bot" if bot else "User"},
    }


def finding(fid: str, **overrides) -> dict:
    base = {
        "id": fid,
        "severity": "should-fix",
        "status": "open",
        "path": "src/example.py",
        "line": 10,
        "title": f"title of {fid}",
        "note": None,
        "first_round": 1,
    }
    base.update(overrides)
    return base


def ledger_in(body: str) -> dict:
    ledger = load_ledger_module()
    found, reason = ledger.find_ledger(body)
    assert found is not None, reason
    return found


def gnu_tools_available() -> bool:
    """Whether `date -d` works on the PATH the steps get."""
    path = [str(d) for d in GNU_BIN_DIRS if d.is_dir()] + [os.environ.get("PATH", "")]
    done = subprocess.run(
        ["date", "-u", "-d", "1 minute ago", "+%s"],
        env={**os.environ, "PATH": os.pathsep.join(path)},
        capture_output=True,
    )
    return done.returncode == 0 and shutil.which("jq") is not None
