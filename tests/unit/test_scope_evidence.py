"""WP3: writable-scope rules + evidence gate checks."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from clams.executor import scope
from clams.executor.evidence import EvidenceGate
from clams.executor.worktree import WorktreeManager


# ------------------------------------------------------------------- scope
def test_glob_prefix():
    assert scope.is_writable("src/app/main.py", ["src/**"], [])
    assert not scope.is_writable("tests/x.py", ["src/**"], [])
    assert scope.is_writable("docs/a.md", ["docs/**"], [])


def test_forbidden_wins():
    assert not scope.is_writable("src/secret.py", ["src/**"], ["src/secret.py"])
    assert not scope.is_writable("src/deep/x.py", ["src/**"], ["src/deep/**"])


def test_catchall_forbidden_yields_to_writable():
    """Regression (live E2E): plans emit forbidden ['*'] meaning 'nothing
    beyond writable' — it must not veto the explicitly writable files."""
    assert scope.is_writable("README.md", ["README.md"], ["*", ".git/**"])
    assert not scope.is_writable("other.txt", ["README.md"], ["*"])
    # specific forbidden still vetoes even when writable would allow
    assert not scope.is_writable("config.env", ["**"], ["*.env"])


def test_bare_filename_glob():
    assert scope.is_writable("README.md", ["*.md"], [])
    assert not scope.is_writable("notes.txt", ["*.md"], [])


def test_check_scope_lists_violations():
    ok, v = scope.check_scope(["a.py", "b.md"], ["*.py"], [])
    assert not ok and v == ["b.md"]


# --------------------------------------------------------------- evidence gate
@pytest.fixture()
def git_repo(tmp_path):
    """A bare remote + a worktree manager rooted at tmp."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=seed, check=True)
    (seed / "hello.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=seed, check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=seed,
                   check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=seed, check=True,
                   capture_output=True)
    wt = WorktreeManager(tmp_path / "work",
                         remote_url_template=str(remote))
    return wt, remote


def _commit_something(wt_dir: Path, name: str = "new.py"):
    (wt_dir / name).write_text("print('ok')\n")
    subprocess.run(["git", "add", "."], cwd=wt_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "work"], cwd=wt_dir, check=True,
                   capture_output=True)


def test_gate_all_pass(git_repo):
    wt, _ = git_repo
    path, _branch = wt.add("o/r", "t1", 1, "main")
    _commit_something(path)
    brief = {"run": ["echo verified"], "writable": ["*.py"], "forbidden": []}
    ev = EvidenceGate(wt).run(path, brief, "main")
    assert ev["passed"], ev["reason"]
    assert ev["changed_files"] == ["new.py"]


def test_gate_fail_no_commit(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t2", 1, "main")
    ev = EvidenceGate(wt).run(path, {"run": ["echo x"], "writable": ["*.py"]},
                             "main")
    assert not ev["passed"]
    assert any(c["check"] == "commits" and not c["ok"] for c in ev["checks"])


def test_gate_fail_command_nonzero(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t3", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"run": ["exit 3"], "writable": ["*.py"]},
                             "main")
    assert not ev["passed"]


def test_gate_passes_silent_success(git_repo):
    """claudio#133: exit 0 with no output is a PASS, flagged not fatal.

    The old rule failed any silent command, which made `-q` / `|| true` run
    commands unsatisfiable — T4 burned 3 attempts on a `git commit -q` that
    succeeded, printed nothing, and was therefore scored "failed".
    """
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t4", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"run": ["true"], "writable": ["*.py"]},
                             "main")
    assert ev["passed"], ev["reason"]
    assert ev["checks"][0]["empty_output"] is True


def test_gate_silent_commit_passes(git_repo):
    """The exact T4 trap: a run command that commits artifacts with -q."""
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t4b", 1, "main")
    _commit_something(path)
    cmd = ("echo 'x = 2' > gen.py && git add -A && git -c user.email=t@t "
           "-c user.name=t commit -q -m artifacts || true")
    ev = EvidenceGate(wt).run(path, {"run": [cmd], "writable": ["*.py"]},
                             "main")
    assert ev["passed"], ev["reason"]


def test_gate_fail_forbidden_edit(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t5", 1, "main")
    (path / "secret.env").write_text("leak\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "bad"], cwd=path, check=True,
                   capture_output=True)
    ev = EvidenceGate(wt).run(
        path, {"run": ["echo ok"], "writable": ["*.py"],
               "forbidden": ["*.env"]}, "main")
    assert not ev["passed"]
    assert any(c["check"] == "scope" and not c["ok"] for c in ev["checks"])


def test_gate_fail_dirty_tree(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t6", 1, "main")
    _commit_something(path)
    (path / "dirty.txt").write_text("uncommitted\n")
    ev = EvidenceGate(wt).run(path, {"run": ["echo ok"], "writable": ["*.py",
                                                                     "*.txt"]},
                             "main")
    assert not ev["passed"]
    assert any(c["check"] == "clean_tree" and not c["ok"] for c in ev["checks"])


def test_gate_missing_run_fails_closed(git_repo):
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t7", 1, "main")
    _commit_something(path)
    ev = EvidenceGate(wt).run(path, {"writable": ["*.py"]}, "main")
    assert not ev["passed"]


# ------------------------------------------------------------------ worktree
def test_worktree_add_remove_push(git_repo):
    wt, remote = git_repo
    path, branch = wt.add("o/r", "t8", 1, "main")
    assert path.exists() and branch == "clams/t8-a1"
    _commit_something(path)
    wt.push(path, branch)
    refs = subprocess.run(["git", "ls-remote", "--heads", str(remote)],
                          capture_output=True, text=True).stdout
    assert branch in refs
    wt.remove("o/r", "t8", 1)
    assert not path.exists()


def test_worktree_token_scrubbed(tmp_path, git_repo):
    wt, remote = git_repo
    # simulate a clone whose remote carries a token
    d = wt.ensure_clone("o/r")
    subprocess.run(["git", "remote", "set-url", "origin",
                    f"https://ghp_secret@{remote}"], cwd=d, check=True,
                   capture_output=True)
    wt._scrub_remote(d)
    url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=d,
                         capture_output=True, text=True).stdout
    assert "ghp_secret" not in url


def test_mid_path_doublestar_glob():
    """`dir/**/file` must span directories. PurePosixPath.match treats "**" as
    "*", so it matched one level only and charged a task its own writable
    files (claudio#133 T5: 172 of 176 "violations")."""
    p = "benchmark/results/claude_code/fix_csv/trial_1/judge.json"
    assert scope.is_writable(p, ["benchmark/results/**/judge.json"], [])
    assert scope.is_writable(p, ["**/judge.json"], [])
    assert scope.is_writable("benchmark/results/judge.json",
                             ["benchmark/results/**/judge.json"], [])
    assert not scope.is_writable(p, ["benchmark/results/**/run.json"], [])
    assert not scope.is_writable("other/judge.json",
                                 ["benchmark/results/**/judge.json"], [])


def test_gate_scope_measured_from_seed_not_workflow_base(git_repo):
    """Retry work reuse seeds attempt N from attempt N-1's branch, so a
    predecessor's committed files must not be charged to this task — they are
    already on the branch it was handed and no agent edit can remove them."""
    wt, _ = git_repo
    path, _ = wt.add("o/r", "t6", 1, "main")
    (path / "artifacts.bin").write_text("data\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "predecessor"], cwd=path, check=True,
                   capture_output=True)
    seed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()
    _commit_something(path)                       # this task's own, writable
    brief = {"run": ["echo ok"], "writable": ["*.py"]}
    against_base = EvidenceGate(wt).run(path, brief, "main")
    assert not against_base["passed"] and "scope" in against_base["reason"]
    against_seed = EvidenceGate(wt).run(path, brief, "main", scope_base=seed)
    assert against_seed["passed"], against_seed["reason"]
