# review-prompts

Auto-fix review loop for codebases. Runs a set of review prompts (security, perf, docs, etc.) via `claude`, `gemini`, and/or `codex` CLI tools against the current directory, applying fixes directly rather than producing reports.

## Contents

- `*-review.md` — review prompts, one per concern (code, sec, perf, arch, test, docs, cli, ux, api, infra, o11y, deps, db).
- `review-loop.py` — runner. Iterates over selected reviews, dispatches each to a randomly chosen tool/model, repeats until stopped.

## Requirements

- Python 3.10+
- At least one of: `claude`, `gemini`, `codex` in `PATH`
- `tee` (only if `--log` is used)

## Quick start

```sh
# auto-detect installed tools (claude/gemini/codex)
./review-loop.py

# single tool
./review-loop.py --models claude

# all supported tools, default models per tool
./review-loop.py --models mixed

# pick randomly across an explicit list
./review-loop.py --models claude,gemini,codex

# pin specific models
./review-loop.py --models claude:opus-4-7,codex:gpt-5-codex,gemini

# same tool with multiple models
./review-loop.py --models claude:opus-4-7,claude:sonnet-4-6
```

Run `./review-loop.py --help` for the full option list.

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--models` | auto-detect | Comma-separated `tool` or `tool:model` entries (one is sampled per review). `mixed`/`random`/`all` expands to every supported tool. Default: every tool found in `PATH`. |
| `--dir` | cwd | `cd` here before running. |
| `--once` | off | Run a single loop and exit. |
| `--max-loops N` | 0 (infinite) | Stop after N loops. |
| `--timeout DUR` | `30m` | Per-review timeout (`90s`, `30m`, `1h`, `2d`). |
| `--log FILE` | — | Tee stdout/stderr to FILE. |
| `--prompt-dir DIR` | `~/prompts` | Where `*-review.md` files live. |
| `--reviews LIST` | all | Comma-separated subset to run. |
| `--exclude LIST` | none | Comma-separated reviews to skip. |
| `--dry-run` | off | Print planned schedule and exit. |

## Behavior

- Each loop: reviews are shuffled, each runs once with a random model from `--models`.
- Each review is hard-bounded by `--timeout`; on timeout the process group is `SIGTERM`'d, then `SIGKILL`'d after 10s.
- `Ctrl+C` once: terminates the active review and stops cleanly. Twice: force-kills.
- A `flock`-based lockfile (`.review-loop.lock`) prevents concurrent runs in the same directory.
- The injected prompt header/footer constrains each tool to: apply small fixes only, skip vendored/generated/lockfile paths, never commit, run lint/typecheck/tests if configured, and revert on failure.

## Adding a review

Drop a new `<name>-review.md` into the prompt directory and add `<name>` to the `REVIEWS` list in `review-loop.py`.

## License

No license specified. Treat as all-rights-reserved unless changed.
