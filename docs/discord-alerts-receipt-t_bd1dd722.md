# Discord alert routing receipt — t_bd1dd722

- Recorded: 2026-08-08T10:24:16Z
- Branch: `stage/hermes-v020-integration-20260808`
- Base before this task: `d0018b9074bacb40e1502dd88264d5a6a5513387`
- Scope: staged source implementation only. The live checkout, live profile config, and live gateway were not modified or restarted.
- Commit: this receipt is included in the task's single implementation commit.

## Delivered

- Added opt-in `gateway.discord_alerts` policy loading with no behavior change when the section is absent.
- Routed Discord gateway online/restart/shutdown/degraded/recovered messages only to the configured status channel.
- Routed Discord Kanban lifecycle messages only to the configured work channel while retaining blocker, review, and handoff detail.
- Routed actionable scheduled-task failures only to the configured errors channel.
- Routed Cunicula article and social previews to their configured review channels with the actual sanitized review body and `MEDIA:` references retained.
- Suppressed generic broad/home Discord cron noise while retaining explicitly configured `discord:<channel>` targets and non-Discord targets.
- Replaced staged alert wrappers, job IDs, separators, HTTP bodies, traces, and log lines with `What happened / Impact / Action` copy.
- Added durable profile-scoped cooldown state, equivalent-failure deduplication, one-shot recovery, and rollback when delivery is not confirmed.
- Added explicit Discord no-mention metadata and adapter-level `AllowedMentions.none()` handling for text, retry, and forum sends.
- Added Approve, Request changes, and Hold review choices. No publishing action was added. Social copy explicitly states that approval does not publish and publishing remains paused.

## Authoritative staged configuration

The source does not hardcode guild-specific IDs. Deployment must add this profile-scoped config before enabling the staged behavior:

```yaml
gateway:
  discord_alerts:
    cooldown_seconds: 600
    channels:
      errors: "1492750576415412404"
      status: "1535573027369123970"
      kanban: "1535573028459773993"
      article_reviews: "1535574256128106538"
      social_reviews: "1535574257927716894"
    projects:
      article_reviews:
        - "cunicula article"
      social_reviews:
        - "cunicula social"
```

`1492750576415412404` remains the Discord home channel and should be renamed to `hermes-errors` during the authorized deployment/configuration step. This task did not perform Discord administration.

## Verification evidence

- RED: `scripts/run_tests.sh tests/gateway/test_discord_alerts.py` failed on missing `gateway.discord_alerts` before implementation.
- Final focused regression: `HERMES_PYTHON=/home/nic/.hermes/hermes-agent/.venv/bin/python scripts/run_tests.sh tests/gateway/test_discord_alerts.py tests/cron/test_discord_alert_delivery.py tests/gateway/test_kanban_notifier.py tests/gateway/test_restart_notification.py tests/gateway/test_discord_free_response.py tests/cron/test_scheduler.py tests/gateway/test_platform_reconnect.py tests/gateway/test_platform_reconnect_fd_leak.py tests/gateway/test_runner_fatal_adapter.py` → 9 files, 168 tests passed, 0 failed.
- Post-review formatter check: `scripts/run_tests.sh tests/gateway/test_discord_alerts.py` → 14 passed, 0 failed.
- Compile smoke: `python -m compileall -q` over every changed Python source/test file → exit 0.
- Diff hygiene: `git diff --check` → exit 0.
- Process closeout: no tracked background processes remained.

## Review record

The approved `app-reviewer` route returned five concrete findings on the first exact-diff pass: preview payload loss, dedupe state consumed before confirmed send, Kanban detail loss, explicit Discord targets being suppressed, and suppressed delivery recorded as delivered. All five were repaired and covered by focused tests. A bounded second reviewer verdict attempt timed out and is not represented as PASS; objective post-fix tests and exact-diff inspection are recorded above. Read-only review cards `t_0283ad83` and `t_64ac4739` remain queued because the board dispatcher did not start them during this run.

## Truth and residual risk

- Working tree at receipt creation: implementation and receipt uncommitted; the next and only write is the requested staging-branch commit.
- Push/deploy: not pushed and not deployed.
- Live Discord proof: intentionally not run because the task forbids modifying or restarting the live gateway. The deployment step must apply the config above, rename the home channel, restart under separate authorization, and verify one event per route with no mentions or cross-channel duplicates.
