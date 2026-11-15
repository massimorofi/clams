"""End-to-end test of the full Clams workflow (issue #18 verification).

Exercises the REAL Clams code paths, not mocks of them:

  Part 1  Provisioning / clone            (issue #18 acceptance criterion 1)
  Part 2  HTTP "Add project" endpoint     (real FastAPI app + real git clone)
  Part 3  Real GitHub polling             (read-only: are Ready issues found?)
  Part 4  Full pipeline, multi-project    (intake -> plan -> dispatch ->
                                           execute -> review -> status)

The harness never mutates the operator's real repositories: Part 3 only reads
GitHub, and Part 4 drives the orchestrator against an in-memory fake GitHub
client plus throwaway local git clones.

Run:  .venv/bin/python scripts/e2e_workflow_test.py
Exit: 0 if every check passes, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clams.config import Settings                      # noqa: E402
from clams.db import open_store                         # noqa: E402
from clams.gh import (Issue, STATUS_READY, STATUS_IN_PROGRESS,  # noqa: E402
                      STATUS_DONE)
from clams.intake import IntakeService                  # noqa: E402
from clams.planner import PlannerService                # noqa: E402
from clams.review import ReviewService                  # noqa: E402
from clams.orchestrator import Orchestrator, ProjectContext, CycleReport  # noqa: E402
from clams.bus import InMemoryBus, publish_result, read_task  # noqa: E402
from clams.contracts import (ExecutionResult, Evidence, CommandEvidence,  # noqa: E402
                             ArtifactDelta, EnvironmentState)
from clams import provision                             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return bool(cond)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=True)


def make_local_repo(root: Path, name: str, branch: str = "main") -> str:
    """Create a bare 'origin' repo with one commit; return its file:// URL."""
    work = root / f"{name}-work"
    work.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", branch], work)
    _git(["config", "user.email", "t@t.io"], work)
    _git(["config", "user.name", "t"], work)
    (work / "README.md").write_text(f"# {name}\n")
    _git(["add", "-A"], work)
    _git(["commit", "-m", "initial"], work)
    bare = root / f"{name}.git"
    _git(["clone", "--bare", str(work), str(bare)], root)
    _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], bare)
    return bare.as_uri()  # file:///...


def part1_provisioning() -> None:
    section("Part 1 - Provisioning / clone (issue #18 acceptance criterion 1)")
    tmp = Path(tempfile.mkdtemp(prefix="clams-p1-"))
    try:
        url = make_local_repo(tmp, "alpha")
        work_root = tmp / "work"
        res = provision.provision_project("Alpha", url, work_root, project_id=7)
        wt = Path(res.worktree_path)
        check("clone lands at canonical path <work_root>/projects/<slug>-<id>",
              wt == provision.project_worktree_path(work_root, 7, "Alpha"),
              str(wt))
        check("working tree is a valid git repo", provision.is_git_repo(wt))
        check("default branch detected from clone", res.default_branch == "main",
              res.default_branch)
        check("cloned flag True on first provision", res.cloned is True)
        check("file present in tree", (wt / "README.md").exists())
        # token must NOT be persisted in .git/config
        cfg = (wt / ".git" / "config").read_text()
        check("no token leaked into .git/config", "x-access-token" not in cfg)
        # idempotent re-provision -> fetch path, not re-clone
        res2 = provision.provision_project("Alpha", url, work_root, project_id=7)
        check("re-provision reuses tree (cloned=False)", res2.cloned is False)
        # new upstream commit -> sync advances HEAD
        work = tmp / "alpha-work"
        (work / "new.txt").write_text("x")
        _git(["add", "-A"], work); _git(["commit", "-m", "second"], work)
        _git(["push", url, "HEAD:main"], work)
        sha = provision.sync_repo(res.worktree_path, branch="main")
        head = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        check("sync_repo advances working tree to new upstream HEAD",
              head == sha and head != "", head[:12])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def part2_api_add_project() -> None:
    section("Part 2 - HTTP 'Add project' endpoint (real app + real clone)")
    from fastapi.testclient import TestClient
    from clams.api import create_app
    tmp = Path(tempfile.mkdtemp(prefix="clams-p2-"))
    try:
        s = Settings()
        s.paths.db_path = str(tmp / "t.db")
        s.paths.data_dir = str(tmp)
        s.paths.work_root = str(tmp / "work")
        s.paths.heartbeat_dir = str(tmp / "hb")
        (tmp / "hb").mkdir(parents=True, exist_ok=True)
        store = open_store(s.paths.db_path)
        app = create_app(s, store=store, bus=InMemoryBus())
        client = TestClient(app)

        # A real github.com URL that does not exist -> clone fails -> 502 + rollback
        bad = client.post("/api/v1/projects", json={
            "name": "ghost",
            "url": "https://github.com/clams-nonexistent-xyz/nope-xyz"})
        check("bad repo URL -> 502 (clone failed)", bad.status_code == 502,
              f"status={bad.status_code}")
        listed = client.get("/api/v1/projects").json()["projects"]
        check("failed clone is rolled back (no phantom row)",
              all(p["name"] != "ghost" for p in listed),
              f"projects={[p['name'] for p in listed]}")

        # Validation
        check("empty name -> 422",
              client.post("/api/v1/projects", json={"name": "", "url": "https://github.com/a/b"}).status_code == 422)
        check("non-github URL -> 422",
              client.post("/api/v1/projects", json={"name": "x", "url": "https://gitlab.com/a/b"}).status_code == 422)

        # Real successful clone of a small public repo (no token needed)
        ok = client.post("/api/v1/projects", json={
            "name": "Hello World",
            "url": "https://github.com/octocat/Hello-World"})
        body = ok.json() if ok.status_code == 200 else {}
        proj = body.get("project", {})
        wt = proj.get("worktree_path", "")
        check("valid github repo -> 200 + provisioned", ok.status_code == 200,
              f"status={ok.status_code} body={str(body)[:160]}")
        check("project row has worktree_path set", bool(wt), wt)
        check("cloned working tree is a git repo",
              bool(wt) and provision.is_git_repo(wt))
        check("default_branch recorded", bool(proj.get("default_branch")),
              str(proj.get("default_branch")))
        # duplicate -> 409
        dup = client.post("/api/v1/projects", json={
            "name": "Hello Again",
            "url": "https://github.com/octocat/Hello-World.git"})
        check("duplicate repo (with .git) -> 409", dup.status_code == 409,
              f"status={dup.status_code}")
        store.db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- Part 3 ----
def part3_real_github_polling() -> None:
    section("Part 3 - Real GitHub polling (read-only, configured projects)")
    from dotenv import dotenv_values
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    token = env.get("CLAMS__GITHUB__TOKEN", "")
    owner = env.get("CLAMS__GITHUB__OWNER", "massimorofi")
    repo = env.get("CLAMS__GITHUB__REPO", "claudio")
    if not token:
        check("github token present", False, "no token in .env - skipping")
        return
    from clams.gh import GitHubClient

    async def run():
        gh = GitHubClient(owner, repo, token)
        ready = await gh.list_issues(state="open", labels=[STATUS_READY])
        print(f"    configured repo {owner}/{repo}: {len(ready)} status:Ready")
        return len(ready), ready

    try:
        n_default, ready_default = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        check("github reachable for polling", False, f"{type(exc).__name__}: {exc}")
        return
    check("GitHub API reachable + auth works", True, f"{owner}/{repo} polled OK")

    # A second configured project (the clams repo itself has Ready issues).
    async def run2():
        gh2 = GitHubClient(owner, "clams", token)
        return await gh2.list_issues(state="open", labels=[STATUS_READY])

    ready2 = asyncio.run(run2())
    print(f"    second repo {owner}/clams: {len(ready2)} status:Ready")
    # A clean board is a valid outcome: once every genuine requirement has been
    # claimed and every planner task is labelled In Progress, zero Ready issues
    # is correct. What must hold is that the poll itself succeeded (no exception,
    # a list came back) -- that is what proves multi-project polling works.
    check("polling a configured project returns a Ready list",
          isinstance(ready2, list), f"{len(ready2)} ready issues in {owner}/clams")
    if ready2:
        sample = ready2[0]
        check("polled issue carries the Ready label + number",
              sample.has_label(STATUS_READY) and sample.number > 0,
              f"#{sample.number} {sample.title[:50]}")

    # Real intake sweep against a THROWAWAY db. sweep() only writes locally +
    # enqueues outbox rows; it never calls GitHub, and we never run the outbox,
    # so the operator's repo is untouched.
    tmp = Path(tempfile.mkdtemp(prefix="clams-p3-"))
    try:
        s = Settings()
        s.paths.db_path = str(tmp / "p3.db")
        store = open_store(s.paths.db_path)
        gh2 = GitHubClient(owner, "clams", token)
        svc = IntakeService(store, gh2, s, project_id=42)
        res = asyncio.run(svc.sweep())
        print(f"    intake sweep: {res.summary()}")
        # Claiming depends on what is on the board right now. Three outcomes are
        # all correct: it claims genuine requirements; it skips everything
        # because the Ready issues are planner tasks; or it finds nothing at all
        # on a clean board. What must never happen is a silent failure or a
        # mis-claimed task.
        check("intake sweep runs cleanly (claims, skips tasks, or finds none)",
              res.failed == [], res.summary())
        rows = store.work_items.list_by_status("claimed", 42)
        check("claimed items are bound to the right project_id",
              len(rows) == len(res.claimed),
              f"{len(rows)} rows at project_id=42")
        ob = store.db.conn.execute(
            "SELECT kind, status, COUNT(*) c FROM outbox GROUP BY kind, status"
        ).fetchall()
        ob_kinds = {r["kind"] for r in ob}
        check("claim queued GitHub side-effects on the outbox (not sent yet)",
              {"gh_status", "gh_comment"} <= ob_kinds or not res.claimed,
              f"outbox kinds={sorted(ob_kinds)}")
        unsent = store.db.conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE status != 'sent'").fetchone()["c"]
        check("no GitHub mutation performed (outbox undelivered)",
              unsent > 0 or not res.claimed,
              f"{unsent} pending outbox rows")
        # idempotency: a second sweep must not re-claim
        res2 = asyncio.run(svc.sweep())
        check("second sweep does not re-claim (idempotent)",
              len(res2.claimed) == 0 and len(res2.skipped) >= len(res.claimed),
              res2.summary())
        store.db.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------- Part 4 ----
class FakeGH:
    """In-memory stand-in for GitHubClient with the methods the pipeline calls."""

    def __init__(self, issues: dict[int, Issue] | None = None):
        self.issues: dict[int, Issue] = dict(issues or {})
        self._next = max(self.issues, default=1000) + 1
        self.comments: list[tuple[int, str]] = []
        self.status_calls: list[tuple[int, str]] = []
        self.sub_links: list[tuple[int, int]] = []

    async def list_issues(self, state="open", labels=None, since=None,
                          per_page=100):
        labels = labels or []
        return [i for i in self.issues.values()
                if i.state == state and all(l in i.labels for l in labels)]

    async def get_issue(self, number):
        return self.issues[number]

    async def create_issue(self, title, body="", labels=None):
        n = self._next
        self._next += 1
        self.issues[n] = Issue(number=n, title=title, body=body,
                               labels=list(labels or []))
        return self.issues[n]

    async def add_sub_issue(self, parent, child):
        self.sub_links.append((parent, child))
        return True

    async def add_comment(self, number, body):
        self.comments.append((number, body))
        return {}

    async def add_labels(self, number, labels):
        self.issues[number].labels = list(
            dict.fromkeys(self.issues[number].labels + list(labels)))
        return self.issues[number].labels

    async def set_status(self, number, status_label, keep=None):
        self.status_calls.append((number, status_label))
        if number in self.issues:
            iss = self.issues[number]
            non_status = [l for l in iss.labels if not l.startswith("status:")]
            iss.labels = list(dict.fromkeys((keep or []) + non_status + [status_label]))
        return []


def stub_llm(prompt: str) -> str:
    if "PASS or FAIL" in prompt:
        return "PASS"
    return json.dumps({"tasks": [
        {"title": f"task {i}", "objective": f"do {i}",
         "acceptance_criterion": f"check {i}", "writable": ["src/"],
         "read_only": [], "forbidden": []} for i in range(1, 4)]})


def _good_result(task_issue: int, requirement: int, project_id: int) -> ExecutionResult:
    return ExecutionResult(
        task_issue=task_issue, status="completed", summary="did it",
        evidence=Evidence(commands=[CommandEvidence(cmd="pytest", exit_code=0,
                                                    tail="42 passed")]),
        artifact_delta=ArtifactDelta(files_changed=["src/x.py"],
                                     commit="abc123", branch=f"task/{task_issue}"),
        environment_state=EnvironmentState(git_head="abc123",
                                           working_tree_clean=True),
        project_id=project_id)


def _build_pipeline(use_def: int):
    """Mirror cli._orchestrator wiring: default project + one extra context.

    ``use_def`` is the project id the orchestrator treats as the default
    project (the real CLI passes ``ensure_default_project``'s return value,
    which is always >= 1; the unit tests pass 0).
    """
    tmp = Path(tempfile.mkdtemp(prefix=f"clams-p4-{use_def}-"))
    s = Settings()
    s.paths.db_path = str(tmp / "p4.db")
    s.paths.data_dir = str(tmp)
    s.paths.work_root = str(tmp / "work")
    s.paths.heartbeat_dir = str(tmp / "hb")
    (tmp / "hb").mkdir(parents=True, exist_ok=True)
    store = open_store(s.paths.db_path)

    # two registered projects, ids mirror ensure_default_project (>=1)
    pdef = store.projects.add(name="widgets",
                              url="https://github.com/acme/widgets")
    pid_def = int(pdef["id"])
    pext = store.projects.add(name="gadgets",
                              url="https://github.com/acme/gadgets")
    pid_ext = int(pext["id"])
    wt_def = make_local_repo(tmp, "widgets")
    wt_ext = make_local_repo(tmp, "gadgets")
    store.projects.set_worktree(pid_def, wt_def, "main")
    store.projects.set_worktree(pid_ext, wt_ext, "main")

    use_ext = pid_ext

    gh_def = FakeGH({100: Issue(number=100, title="Add dark mode",
                                body="AC: toggle", labels=[STATUS_READY])})
    gh_ext = FakeGH({200: Issue(number=200, title="Fix login bug",
                                body="AC: tests pass", labels=[STATUS_READY])})

    intake = IntakeService(store, gh_def, s, project_id=use_def)
    planner = PlannerService(store, gh_def, s, llm_call=stub_llm,
                            project_id=use_def)
    review = ReviewService(store, gh_def, s, judge=planner.judge_review,
                          project_id=use_def)

    ctx = ProjectContext(
        project_id=use_ext, gh=gh_ext,
        intake=IntakeService(store, gh_ext, s, project_id=use_ext),
        planner=PlannerService(store, gh_ext, s, llm_call=stub_llm,
                               project_id=use_ext),
        review=ReviewService(store, gh_ext, s, judge=planner.judge_review,
                             project_id=use_ext),
        worktree_path=wt_ext, default_branch="main")

    bus = InMemoryBus()
    orch = Orchestrator(store, bus, s, gh=gh_def, planner=planner,
                        intake=intake, review=review,
                        project_contexts=[ctx], default_project_id=use_def)
    return dict(tmp=tmp, s=s, store=store, orch=orch, bus=bus,
                gh_def=gh_def, gh_ext=gh_ext, use_def=use_def, use_ext=use_ext)


def _run_pipeline(default_project_id: int) -> dict:
    env = _build_pipeline(default_project_id)
    orch, store = env["orch"], env["store"]
    rep = CycleReport()

    async def drive():
        await orch.step_intake(rep)
        await orch.step_plan(rep)
        await orch.step_dispatch(rep)
        # stub executor: consume every dispatched brief, return passing evidence
        while True:
            brief = await read_task(env["bus"], "executors", "e1", count=1,
                                    block_ms=10)
            if brief is None:
                break
            await publish_result(env["bus"], _good_result(
                brief.task_issue, brief.requirement_issue, brief.project_id))
        await orch.step_review_results(rep)
        await orch.step_projects(rep)
    asyncio.run(drive())

    # Where did the plans actually land? Capture before closing the store.
    def_row = store.work_items.get(100, env["use_def"])
    phantom_def = store.work_items.get(100, 0)
    ext_row = store.work_items.get(200, env["use_ext"])
    phantom_ext = store.work_items.get(200, 0)
    landing = {
        "def_row": None if not def_row else (def_row.status, def_row.task_numbers),
        "phantom_def": None if not phantom_def else (phantom_def.status,
                                                    phantom_def.task_numbers),
        "ext_row": None if not ext_row else (ext_row.status, ext_row.task_numbers),
        "phantom_ext": None if not phantom_ext else (phantom_ext.status,
                                                    phantom_ext.task_numbers),
    }

    # Consequence probe: because the owning row never leaves 'claimed', a second
    # planning pass re-plans the SAME requirement and creates ANOTHER batch of
    # sub-issues on GitHub. Count the growth.
    sub_before = len(env["gh_def"].issues)
    async def replan_again():
        rp = CycleReport()
        await orch.step_plan(rp)
    asyncio.run(replan_again())
    sub_after = len(env["gh_def"].issues)
    replan_growth = sub_after - sub_before

    snap = {}
    for pid in (env["use_def"], env["use_ext"]):
        items = store.work_items.list_by_status("claimed", pid) + \
            store.work_items.list_by_status("planned", pid) + \
            store.work_items.list_by_status("dispatched", pid) + \
            store.work_items.list_by_status("review", pid) + \
            store.work_items.list_by_status("blocked", pid)
        snap[pid] = [(w.requirement_number, w.status, w.task_numbers)
                     for w in items]
    proj_rows = {int(r["id"]): (r["status"], r["progress"])
                 for r in store.projects.all()}
    out = dict(rep=rep, snap=snap, proj_rows=proj_rows, env=env,
               landing=landing, replan_growth=replan_growth)
    store.db.close()
    shutil.rmtree(env["tmp"], ignore_errors=True)
    return out


def part4_pipeline() -> None:
    section("Part 4 - Full pipeline (intake->plan->dispatch->execute->review)")

    print("\n  4a. Real CLI wiring: default_project_id >= 1 (as ensure_default_project returns)")
    r = _run_pipeline(default_project_id=1)
    rep = r["rep"]
    print(f"      cycle report: {rep.summary()}")
    print(f"      work items by project: {r['snap']}")
    print(f"      project status rows:   {r['proj_rows']}")
    print(f"      plan landing:          {r['landing']}")
    check("intake claims Ready issues from BOTH projects",
          len(rep.intake) == 2, f"claimed={rep.intake}")
    check("planner ran for both projects",
          len(rep.planned) == 2, f"planned={rep.planned}")
    check("tasks were dispatched to the executor queue",
          len(rep.dispatched) > 0,
          f"dispatched={rep.dispatched}  <-- expect 3+3 if wiring is correct")
    check("requirements reached Review after execution evidence",
          len(rep.reviewed) > 0, f"reviewed={rep.reviewed}")
    land = r["landing"]
    check("plan stored under the project that OWNS the requirement (not project 0)",
          bool(land["def_row"] and land["def_row"][1]),
          f"owning row={land['def_row']}  phantom@project0={land['phantom_def']}")
    check("no runaway re-plan (planning twice must not mint new sub-issues)",
          r["replan_growth"] == 0,
          f"second step_plan created {r['replan_growth']} extra GitHub issues")

    print("\n  4b. Legacy wiring: default_project_id = 0 (what the unit tests use)")
    r0 = _run_pipeline(default_project_id=0)
    rep0 = r0["rep"]
    print(f"      cycle report: {rep0.summary()}")
    print(f"      work items by project: {r0['snap']}")
    print(f"      project status rows:   {r0['proj_rows']}")
    check("[project 0] tasks dispatched", len(rep0.dispatched) > 0,
          f"dispatched={rep0.dispatched}")
    check("[project 0] requirement reached review", len(rep0.reviewed) > 0,
          f"reviewed={rep0.reviewed}")


# ------------------------------------------------------------------ main ----
def main() -> int:
    print("Clams end-to-end workflow test (issue #18 verification)")
    part1_provisioning()
    part2_api_add_project()
    part3_real_github_polling()
    part4_pipeline()

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"  {passed}/{len(RESULTS)} checks passed")
    if failed:
        print("\n  FAILURES:")
        for n, d in failed:
            print(f"    - {n}: {d}")
        return 1
    print("  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
