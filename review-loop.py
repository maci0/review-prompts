#!/usr/bin/env python3
"""Run review prompts via claude/gemini/codex against current dir."""

from __future__ import annotations

import argparse
import fcntl
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

REVIEWS = [
    "code-review", "doc-review", "cli-review", "perf-review", "arch-review",
    "sec-review", "test-review", "ux-review", "api-review", "infra-review",
    "o11y-review", "deps-review", "db-review",
]

VALID_TOOLS = {"claude", "gemini", "codex"}

PROMPT_HEADER = "MODE: AUTO_FIX — apply fixes directly to files. Do NOT write a report."

PROMPT_SUFFIX = """

---

OVERRIDE: Ignore any 'Output format', 'Executive Summary', or report-style sections in the instructions above. Do NOT write a report.

Operating mode: apply fixes directly to files, autonomously.

Rules:
- First check if this review makes sense for this codebase. If not, exit immediately without changes.
- Make the smallest reversible diff possible.
- Fix at most ~10 highest-value issues this pass. Stop after that.
- Do not delete tests.
- Do not change public APIs or exported symbols.
- Skip anything uncertain. Never ask questions.
- Never modify files in: node_modules, vendor, dist, build, .next, target, .git, generated/auto-generated files, lockfiles (package-lock.json, yarn.lock, pnpm-lock.yaml, Cargo.lock, go.sum, etc.).
- If a single fix would change more than 300 lines in one file, skip it.
- Do not commit anything to git. Do not run git commit, git push, or git tag.
- After edits, if the project has a lint, typecheck, or test command configured (package.json scripts, Makefile, justfile, pyproject.toml, etc.), run the relevant one. If it fails due to your changes, revert them.
- At the end, print a brief 1-line summary per changed file in the form: 'PATH — what was done'. If nothing changed, print 'No changes.'"""


@dataclass(frozen=True)
class ToolSpec:
    tool: str
    model: str | None = None

    def label(self) -> str:
        return f"{self.tool}:{self.model}" if self.model else self.tool


@dataclass
class Stats:
    runs: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    fails: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    timeouts: dict[str, int] = field(default_factory=lambda: defaultdict(int))


def parse_duration(s: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd]?)", s)
    if not m:
        raise argparse.ArgumentTypeError(f"invalid duration: {s} (e.g. 30m, 1h, 90s)")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


MIXED_KEYWORDS = {"mixed", "random", "all"}


def parse_models(s: str) -> list[ToolSpec]:
    specs: list[ToolSpec] = []
    seen: set[ToolSpec] = set()
    for entry in s.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower() in MIXED_KEYWORDS:
            for t in sorted(VALID_TOOLS):
                spec = ToolSpec(t)
                if spec not in seen:
                    specs.append(spec)
                    seen.add(spec)
            continue
        tool, _, model = entry.partition(":")
        tool = tool.strip()
        model = model.strip() or None
        if tool not in VALID_TOOLS:
            raise argparse.ArgumentTypeError(
                f"unknown tool: {tool!r} "
                f"(valid: {', '.join(sorted(VALID_TOOLS))}, "
                f"or {'/'.join(sorted(MIXED_KEYWORDS))} for all)"
            )
        spec = ToolSpec(tool, model)
        if spec not in seen:
            specs.append(spec)
            seen.add(spec)
    if not specs:
        raise argparse.ArgumentTypeError("no models specified")
    return specs


def fmt_duration(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 60}m{secs % 60:02d}s"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"Required tool not found in PATH: {name}")


def build_cmd(spec: ToolSpec, prompt: str) -> list[str]:
    if spec.tool == "claude":
        cmd = ["claude", "--dangerously-skip-permissions"]
        if spec.model:
            cmd += ["--model", spec.model]
        cmd += ["-p", prompt]
    elif spec.tool == "gemini":
        cmd = ["gemini", "-y"]
        if spec.model:
            cmd += ["-m", spec.model]
        cmd += ["-p", prompt]
    elif spec.tool == "codex":
        cmd = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if spec.model:
            cmd += ["-m", spec.model]
        cmd.append(prompt)
    else:
        raise ValueError(f"unknown tool: {spec.tool}")
    return cmd


class Runner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tools: list[ToolSpec] = args.models
        self.reviews: list[str] = self._filter_reviews()
        self.timeout_secs = parse_duration(args.timeout)
        self.stats = Stats()
        self.failures: list[str] = []
        self.loop_count = 0
        self.review_count = 0
        self.fail_count = 0
        self.stopping = False
        self.interrupt_count = 0
        self.current_proc: subprocess.Popen | None = None
        self.script_start = time.monotonic()

    def _filter_reviews(self) -> list[str]:
        reviews = list(REVIEWS)
        valid = set(REVIEWS)

        def split(arg: str) -> set[str]:
            return {x.strip() for x in arg.split(",") if x.strip()}

        if self.args.reviews:
            inc = split(self.args.reviews)
            unknown = inc - valid
            if unknown:
                sys.exit(f"Unknown review in --reviews: {', '.join(sorted(unknown))}")
            reviews = [r for r in reviews if r in inc]
        if self.args.exclude:
            exc = split(self.args.exclude)
            unknown = exc - valid
            if unknown:
                sys.exit(f"Unknown review in --exclude: {', '.join(sorted(unknown))}")
            reviews = [r for r in reviews if r not in exc]
        if not reviews:
            sys.exit("No reviews remain after filtering.")
        return reviews

    def pick_tool(self) -> ToolSpec:
        return random.choice(self.tools)

    def run_review(self, review: str) -> None:
        spec = self.pick_tool()
        prompt_file = self.args.prompt_dir / f"{review}.md"
        if not prompt_file.is_file():
            log(f"Missing prompt file: {prompt_file} — skipping")
            return

        prompt = f"{PROMPT_HEADER}\n\n{prompt_file.read_text()}{PROMPT_SUFFIX}"

        start = time.monotonic()
        log(f"Running {review} with {spec.label()} (timeout {self.args.timeout})")
        self.stats.runs[spec.tool] += 1

        cmd = build_cmd(spec, prompt)
        proc = subprocess.Popen(cmd, start_new_session=True)
        self.current_proc = proc
        timed_out = False
        try:
            rc = proc.wait(timeout=self.timeout_secs)
        except subprocess.TimeoutExpired:
            timed_out = True
            log(f"TIMEOUT: {review} ({spec.label()}) after {self.args.timeout}")
            self._kill_proc(proc, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill_proc(proc, signal.SIGKILL)
                rc = proc.wait()
        finally:
            self.current_proc = None

        elapsed = time.monotonic() - start
        self.interrupt_count = 0

        if timed_out:
            self.fail_count += 1
            self.stats.timeouts[spec.tool] += 1
            self.failures.append(
                f"loop {self.loop_count + 1}: {review} ({spec.label()}) — timeout"
            )
            return

        if rc in (130, 143, -signal.SIGINT, -signal.SIGTERM):
            log(f"Interrupted: {review} ({spec.label()}) after {fmt_duration(elapsed)}")
            self.stopping = True
        elif rc != 0:
            log(f"FAILED: {review} ({spec.label()}) after {fmt_duration(elapsed)} — exit code {rc}")
            self.fail_count += 1
            self.stats.fails[spec.tool] += 1
            self.failures.append(
                f"loop {self.loop_count + 1}: {review} ({spec.label()}) — exit {rc}"
            )
        else:
            log(f"Done: {review} ({spec.label()}) in {fmt_duration(elapsed)}")

    def _kill_proc(self, proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    def handle_signal(self, signum, frame) -> None:
        self.interrupt_count += 1
        self.stopping = True
        proc = self.current_proc
        print()
        if proc and proc.poll() is None:
            if self.interrupt_count == 1:
                log(f"Signal received — terminating current review (pid {proc.pid}). Ctrl+C again for KILL.")
                self._kill_proc(proc, signal.SIGTERM)
            else:
                log(f"Force-killing pid {proc.pid}...")
                self._kill_proc(proc, signal.SIGKILL)
        else:
            log("Signal received — stopping...")

    def print_stats(self) -> None:
        total = time.monotonic() - self.script_start
        print()
        print("=== Review loop stopped ===")
        print(f"Completed loops: {self.loop_count}")
        print(f"Total reviews run: {self.review_count}")
        print(f"Failures: {self.fail_count}")
        print(f"Total time: {fmt_duration(total)}")

        unique_tools = sorted({s.tool for s in self.tools})
        if len(self.tools) > 1:
            print()
            print("Per-tool stats:")
            for t in unique_tools:
                print(
                    f"  {t:<7} runs={self.stats.runs[t]}  "
                    f"fails={self.stats.fails[t]}  timeouts={self.stats.timeouts[t]}"
                )

        if self.failures:
            print()
            print("Failed reviews:")
            for f in self.failures:
                print(f"  - {f}")

    def dry_run(self) -> None:
        print("DRY RUN — planned schedule for one loop:")
        order = list(self.reviews)
        random.shuffle(order)
        for r in order:
            print(f"  {r:<15} → {self.pick_tool().label()}")
        print()
        print(f"Reviews per loop: {len(self.reviews)}")
        models_str = ",".join(s.label() for s in self.tools)
        print(
            f"Models: {models_str}  |  timeout: {self.args.timeout}  |  "
            f"prompt-dir: {self.args.prompt_dir}"
        )
        if self.args.once:
            limit = "1"
        elif self.args.max_loops:
            limit = str(self.args.max_loops)
        else:
            limit = "infinite"
        print(f"Loop limit: {limit}")

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

        if self.args.dry_run:
            self.dry_run()
            return

        try:
            while True:
                order = list(self.reviews)
                random.shuffle(order)
                loop_start = time.monotonic()
                for r in order:
                    if self.stopping:
                        return
                    self.run_review(r)
                    self.review_count += 1
                self.loop_count += 1
                loop_elapsed = time.monotonic() - loop_start
                print()
                log(
                    f"=== Loop {self.loop_count} complete in {fmt_duration(loop_elapsed)} "
                    f"({self.review_count} reviews, {self.fail_count} failures) ==="
                )
                print()

                if self.args.once:
                    break
                if self.args.max_loops and self.loop_count >= self.args.max_loops:
                    break
        finally:
            self.print_stats()


def acquire_lock(path: Path) -> int:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another review-loop appears to be running (lock: {path})")
    return fd


def setup_log_tee(log_path: Path) -> None:
    tee = subprocess.Popen(["tee", "-a", str(log_path)], stdin=subprocess.PIPE)
    assert tee.stdin is not None
    os.dup2(tee.stdin.fileno(), 1)
    os.dup2(tee.stdin.fileno(), 2)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run review prompts via claude/gemini/codex.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  review-loop.py --models claude\n"
            "  review-loop.py --models mixed                 # all tools, default models\n"
            "  review-loop.py --models claude,gemini,codex\n"
            "  review-loop.py --models claude:opus-4-7,codex:gpt-5-codex\n"
            "  review-loop.py --models mixed,claude:opus-4-7 # all + extra pinned model\n"
        ),
    )
    p.add_argument(
        "--models", type=parse_models, default=None,
        help="comma-separated tools, optionally tool:model. "
             "Use 'mixed' (or 'random'/'all') as shorthand for every supported tool. "
             "Examples: 'claude', 'mixed', 'claude:opus-4-7,codex:gpt-5-codex'. "
             "Default: auto-detect installed tools.",
    )
    p.add_argument("--dir", type=Path, default=None, help="cd into DIR before running")
    p.add_argument("--once", action="store_true", help="run a single loop and exit")
    p.add_argument("--max-loops", type=int, default=0, help="stop after N loops")
    p.add_argument(
        "--timeout", default="30m",
        type=lambda s: (parse_duration(s), s)[1],
        help="per-review timeout (default 30m)",
    )
    p.add_argument("--log", type=Path, default=None, help="tee output to FILE")
    p.add_argument(
        "--prompt-dir", type=Path,
        default=Path(__file__).resolve().parent,
        help="directory of *-review.md files (default: script directory)",
    )
    p.add_argument("--reviews", default="", help="comma-separated subset to run")
    p.add_argument("--exclude", default="", help="comma-separated reviews to skip")
    p.add_argument("--dry-run", action="store_true", help="print planned schedule and exit")
    args = p.parse_args()
    if args.max_loops < 0:
        p.error("--max-loops must be >= 0")
    return args


def autodetect_models() -> list[ToolSpec]:
    found = [ToolSpec(t) for t in sorted(VALID_TOOLS) if shutil.which(t)]
    if not found:
        sys.exit(
            f"No supported tools found in PATH. Install one of: "
            f"{', '.join(sorted(VALID_TOOLS))}, or pass --models explicitly."
        )
    return found


def main() -> None:
    args = parse_args()

    if args.dir:
        try:
            os.chdir(args.dir)
        except OSError as e:
            sys.exit(f"Cannot cd to {args.dir}: {e}")

    if not args.prompt_dir.is_dir():
        sys.exit(f"Prompt directory not found: {args.prompt_dir}")

    if args.models is None:
        args.models = autodetect_models()
        log(f"Auto-detected models: {','.join(s.label() for s in args.models)}")

    for tool in {s.tool for s in args.models}:
        check_tool(tool)
    check_tool("tee")

    acquire_lock(Path.cwd() / ".review-loop.lock")

    if args.log:
        setup_log_tee(args.log)

    Runner(args).run()


if __name__ == "__main__":
    main()
