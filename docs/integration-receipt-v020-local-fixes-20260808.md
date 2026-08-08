# Hermes v0.20 local-fix integration receipt

- Task: `t_ee5eb56f`
- Timestamp: `2026-08-08T09:11:49Z`
- Workspace: `/home/nic/.hermes/worktrees/hermes-v020-integration-20260808`
- Branch: `stage/hermes-v020-integration-20260808`
- Upstream base: `520a1e78128c8fe328b1e70aaea99c72e26880cc` (`origin/main`)
- Integrated code tip before this receipt: `7aa6d0cf7a8d4af7f39b7529d1b728f7a54494b1`
- Upstream relationship at verification: 0 behind, 5 code commits ahead

## Safety and scope

Work stayed in the isolated staging worktree. The live checkout, live release venv, gateway, Desktop, profiles, configuration, and user state were not modified or restarted. Nothing was pushed or deployed.

The five carried changes were rebased onto the current fetched `origin/main`. No whole-file or wholesale ours/theirs conflict choice was used. The final rebase onto `520a1e781` applied cleanly after the earlier semantic conflict resolution.

## Integrated commits and retained behavior

1. `5fd7505521b58b04eff5c272e06411c4d320f11a` — `fix(kanban): trust dispatch outcomes and reap worker trees`
   - `DispatchResult.spawn_failed` records only workspace/spawn attempts that genuinely failed after a task became claimable.
   - Dispatcher health now consumes those semantic results instead of treating any remaining ready row as a failed spawn. Capacity, dependency, profile, lock, and respawn deferrals remain healthy idle states.
   - Timeout, stale-run, manual reclaim, and crashed-worker cleanup target isolated task-owned process groups. Linux cleanup also finds detached descendants by process ancestry and inherited `HERMES_KANBAN_TASK`, while protecting the dispatcher's own group and unrelated processes.
   - Claims are not released when task-owned workers survive termination.

2. `54c01e8babcb8e6a9456de7ba5e50d881ac60394` — `test(gateway): make honcho memo mtime change deterministic`
   - The Honcho cache-busting memo test advances `mtime_ns` explicitly by one nanosecond, proving cache invalidation without depending on filesystem clock granularity.

3. `d5dc2f25a1911ba07f4bdecc0c83fd9443269780` — `fix(agent): normalize structured assistant content before stripping`
   - Shared assistant-content normalization accepts strings and visible `text`/`output_text` blocks, including compatible untyped text/content objects.
   - Reasoning, tool, media, unknown, malformed, and non-text blocks remain hidden instead of being stringified.
   - Every think-block stripping caller now receives a string, preventing `expected string or bytes-like object, got 'list'` retry loops while preserving interim Codex-response behavior.

4. `82231a6a69515de25c8ad622a63805bc617fe4c7` — `fix(kanban): complete verified coding workers autonomously`
   - Worker guidance calls `kanban_complete` once written acceptance checks pass.
   - `kanban_block` is reserved for genuine unresolved decisions, credentials, capabilities, or a task's explicit named-human gate; routine review does not create a sticky block.
   - Existing downstream review cards remain the dependency mechanism: implementation completion unlocks them.

5. `7aa6d0cf7a8d4af7f39b7529d1b728f7a54494b1` — `fix(kanban): guarantee visible worker lifecycle updates`
   - Claimed events emit `START`; noted heartbeats emit meaningful `STATUS`; automatic empty heartbeats advance cursors silently.
   - Running tasks with no visible status for 600 seconds emit one `STATUS: STILL WORKING` update without spam.
   - Per-subscription SQLite status and short reservations make the silence timer restart-safe and concurrent-notifier-safe. Failed sends roll reservations back; successful sends commit visibility time.
   - Terminal messages retain DONE/BLOCKED/handoff distinctions and existing per-profile, dispatcher-lock, topic/thread, wake, and per-subscription failure isolation behavior.

## Reconciled or dropped as redundant

- The old raw-ready-row health probe and its `any_spawned` heuristic were not retained. They misclassified legitimately held work and duplicated information now carried by `DispatchResult.spawn_failed`.
- Current upstream emergency pause and live auto-decompose behavior were preserved. Zombie reaping continues while paused; auto-decompose and dispatch remain disabled during the pause.
- Source-era test/mock adaptations whose old call shape no longer exists on current upstream were not carried as standalone hunks. Current production calls pass `task_id` into worker-tree termination and the focused behavior tests exercise the current API.
- The notifier patch was adapted to current upstream routing rather than replacing it: newer block-loop triage notifications, named-profile ownership, dispatcher-lock gating, chat metadata, wake handling, and failure isolation remain intact.
- The structured-content fix uses the current shared `agent.message_content` module rather than preserving the source commit's duplicated inline coercion.
- No already-upstream feature was reintroduced as a duplicate implementation.

## Verification evidence

### Compilation

Passed:

```text
.venv/bin/python -m compileall -q \
  agent/agent_runtime_helpers.py agent/message_content.py \
  agent/prompt_builder.py gateway/kanban_watchers.py \
  hermes_cli/kanban.py hermes_cli/kanban_db.py run_agent.py
```

### Focused canonical tests

The repository-mandated `scripts/run_tests.sh` ran these seven files in isolated per-file subprocesses:

- `tests/hermes_cli/test_kanban_core_functionality.py` — 28 passed
- `tests/gateway/test_kanban_watchers_mixin.py` — 3 passed
- `tests/gateway/test_kanban_notifier.py` — 13 passed
- `tests/tools/test_kanban_tools.py` — 28 passed
- `tests/run_agent/test_run_agent.py` — 248 passed
- `tests/run_agent/test_run_agent_codex_responses.py` — 45 passed
- `tests/gateway/test_agent_cache.py` — 34 passed

Final distinct result: **399 passed, 0 failed**.

The first combined run reported 398 passed and one environment-only failure because the repository's `dev` extra does not install the optional Anthropic SDK needed by an unrelated upstream provider test in `test_run_agent.py`. `anthropic==0.87.0`, the repository-pinned optional version, was installed into the staging worktree's `.venv` only; the failed file then passed 248/248. No dependency or lock file changed.

A final fetch advanced upstream by eight Honcho auth-recovery commits. All six local commits rebased cleanly onto that tip; the shared gateway cache file was then rerun on the final base and passed 34/34. Those 34 tests are already included in the 399 distinct-test count above.

### Repository gates

- `git diff --check`: passed before receipt creation.
- Conflict-marker scan: no merge/cherry-pick conflict markers found; matches were only intentional documentation separator lines.
- Upstream relation before receipt: `0 5` from `git rev-list --left-right --count origin/main...HEAD`.
- Working tree before receipt: clean.

## Delivery truth

- Working tree: receipt pending commit at the time this file was authored.
- Code commits: committed locally.
- Push: not performed.
- Deploy/restart/live-checkout update: not performed.
- Task-started bounded processes: dependency install and test runners exited with code 0; final process closure is recorded in the Kanban closeout.
