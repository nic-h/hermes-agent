from datetime import datetime, timedelta, timezone


from gateway.discord_alerts import (
    DiscordAlertDeduper,
    DiscordAlertPolicy,
    discord_alert_metadata,
    format_cron_alert,
    load_discord_alert_policy,
    prepare_cron_alert,
    prepare_gateway_alert,
    prepare_kanban_alert,
    rollback_prepared_alert,
)


CHANNELS = {
    "errors": "1492750576415412404",
    "status": "1535573027369123970",
    "kanban": "1535573028459773993",
    "article_reviews": "1535574256128106538",
    "social_reviews": "1535574257927716894",
}


def _policy():
    return DiscordAlertPolicy.from_config(
        {
            "gateway": {
                "discord_alerts": {
                    "channels": CHANNELS,
                    "projects": {
                        "article_reviews": ["cunicula"],
                        "social_reviews": ["cunicula social", "nichamilton"],
                    },
                    "cooldown_seconds": 600,
                }
            }
        }
    )


def test_policy_routes_each_alert_class_to_one_configured_channel():
    policy = _policy()

    assert policy.channel_for("gateway") == CHANNELS["status"]
    assert policy.channel_for("kanban") == CHANNELS["kanban"]
    assert policy.channel_for("failure") == CHANNELS["errors"]
    assert policy.channel_for("project", subject="Cunicula article preview") == CHANNELS["article_reviews"]
    assert policy.channel_for("project", subject="Nichamilton social queue") == CHANNELS["social_reviews"]

    # Unknown projects do not spill into a broad status, general, or home channel.
    assert policy.channel_for("project", subject="unrelated campaign") is None
    assert policy.channel_for("unknown", subject="Cunicula") is None


def test_policy_is_opt_in_and_rejects_invalid_channel_values():
    assert DiscordAlertPolicy.from_config({}).channel_for("gateway") is None
    policy = DiscordAlertPolicy.from_config(
        {"gateway": {"discord_alerts": {"channels": {"status": "not-a-discord-id"}}}}
    )
    assert policy.channel_for("gateway") is None


def test_policy_loads_from_profile_config_without_new_environment_settings(
    tmp_path, monkeypatch,
):
    hermes_home = tmp_path / "profile-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        """gateway:
  discord_alerts:
    channels:
      errors: "1492750576415412404"
      status: "1535573027369123970"
      kanban: "1535573028459773993"
      article_reviews: "1535574256128106538"
      social_reviews: "1535574257927716894"
    projects:
      article_reviews: [cunicula]
      social_reviews: ["cunicula social", nichamilton]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    policy = load_discord_alert_policy()

    assert policy.channel_for("gateway") == CHANNELS["status"]
    assert policy.channel_for("kanban") == CHANNELS["kanban"]
    assert policy.channel_for("failure") == CHANNELS["errors"]
    assert policy.channel_for(
        "project", subject="Cunicula article preview"
    ) == CHANNELS["article_reviews"]
    assert policy.channel_for(
        "project", subject="Cunicula social preview"
    ) == CHANNELS["social_reviews"]


def test_project_preview_format_is_plain_language_and_actionable():
    text = format_cron_alert(
        {"id": "cron-secret-id", "name": "Cunicula article preview"},
        "Cronjob Response: Cunicula\n(job_id: cron-secret-id)\n-------------\n"
        "Draft headline\n\nUseful article body.\nMEDIA:/tmp/preview.png\n"
        "HTTP 200 {\"huge\": \"provider body\"}\nTraceback: noisy log text",
        success=True,
        project_kind="article_reviews",
    )

    assert "What happened:" in text
    assert "Impact:" in text
    assert "Action:" in text
    assert "Approve, Request changes, or Hold" in text
    assert "cron-secret-id" not in text
    assert "Cronjob Response" not in text
    assert "HTTP 200" not in text
    assert "Traceback" not in text
    assert "-------------" not in text
    assert "Draft headline" in text
    assert "Useful article body." in text
    assert "MEDIA:/tmp/preview.png" in text


def test_failure_format_classifies_error_without_leaking_http_body_or_stack():
    text = format_cron_alert(
        {"id": "cron-42", "name": "Nichamilton social draft"},
        "HTTPStatusError: 429 Too Many Requests body={\"token\":\"secret\"}\n"
        "Traceback (most recent call last): /srv/app.py:99",
        success=False,
        project_kind="social_reviews",
    )

    assert "What happened:" in text
    assert "rate limit" in text.lower()
    assert "Impact:" in text
    assert "scheduled review was not produced" in text
    assert "Action:" in text
    assert "limit" in text.lower()
    assert "cron-42" not in text
    assert "token" not in text
    assert "Traceback" not in text
    assert "/srv/app.py" not in text


def test_social_review_keeps_publishing_paused_after_approval():
    text = format_cron_alert(
        {"name": "Cunicula social preview"},
        "draft body",
        success=True,
        project_kind="social_reviews",
    )

    assert "Approve, Request changes, or Hold" in text
    assert "publishing remains paused" in text
    assert "does not publish" in text


def test_deduper_suppresses_identical_failure_until_cooldown_and_recovers_once():
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    deduper = DiscordAlertDeduper(cooldown_seconds=600)

    assert deduper.should_send_failure("cron:cunicula", "rate limit", now=now)
    assert not deduper.should_send_failure(
        "cron:cunicula", "rate limit", now=now + timedelta(seconds=599)
    )
    assert deduper.should_send_failure(
        "cron:cunicula", "rate limit", now=now + timedelta(seconds=600)
    )
    assert deduper.should_send_failure(
        "cron:cunicula", "authentication", now=now + timedelta(seconds=601)
    )

    assert deduper.should_send_recovery("cron:cunicula")
    assert not deduper.should_send_recovery("cron:cunicula")


def test_deduper_persists_failure_and_recovery_across_process_instances(tmp_path):
    state_path = tmp_path / "discord-alerts.json"
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)

    first = DiscordAlertDeduper(cooldown_seconds=600, state_path=state_path)
    assert first.should_send_failure("cron:persisted", "timeout", now=now)

    restarted = DiscordAlertDeduper(cooldown_seconds=600, state_path=state_path)
    assert not restarted.should_send_failure(
        "cron:persisted", "timeout", now=now + timedelta(seconds=20)
    )
    assert restarted.should_send_recovery("cron:persisted")

    after_recovery = DiscordAlertDeduper(cooldown_seconds=600, state_path=state_path)
    assert not after_recovery.should_send_recovery("cron:persisted")


def test_alert_metadata_is_nonconversational_and_disables_mentions():
    assert discord_alert_metadata({"existing": "value"}) == {
        "existing": "value",
        "non_conversational": True,
        "discord_no_mentions": True,
    }


def test_prepared_project_alert_overrides_destination_and_recovers_after_failure():
    policy = _policy()
    job = {"id": "unique-cunicula-route", "name": "Cunicula article preview"}

    failed = prepare_cron_alert(
        job, "HTTP 429 body=do-not-send", success=False,
        error="HTTP 429 body=do-not-send", policy=policy,
    )
    assert failed is not None
    assert failed.channel_id == CHANNELS["errors"]
    assert "body=do-not-send" not in failed.content

    duplicate = prepare_cron_alert(
        job, "HTTP 429 another body", success=False,
        error="HTTP 429 another body", policy=policy,
    )
    assert duplicate is not None
    assert duplicate.content == ""
    assert duplicate.metadata == {"suppressed": True}

    recovered = prepare_cron_alert(job, "fresh preview", success=True, policy=policy)
    assert recovered is not None
    assert recovered.channel_id == CHANNELS["article_reviews"]
    assert "recovered" in recovered.content


def test_failed_delivery_rolls_back_failure_and_recovery_dedupe_transitions():
    policy = _policy()
    job = {"id": "unique-rollback-route", "name": "Cunicula article preview"}

    failed = prepare_cron_alert(
        job, "timeout", success=False, error="timeout", policy=policy,
    )
    assert failed is not None and failed.transition is not None
    rollback_prepared_alert(failed)
    retried_failure = prepare_cron_alert(
        job, "timeout", success=False, error="timeout", policy=policy,
    )
    assert retried_failure is not None and retried_failure.content

    recovery = prepare_cron_alert(job, "fresh draft", success=True, policy=policy)
    assert recovery is not None and "recovered" in recovery.content
    rollback_prepared_alert(recovery)
    retried_recovery = prepare_cron_alert(
        job, "fresh draft", success=True, policy=policy,
    )
    assert retried_recovery is not None and "recovered" in retried_recovery.content


def test_generic_failure_and_recovery_stay_in_narrow_errors_channel():
    policy = _policy()
    job = {"id": "unique-generic-route", "name": "Nightly maintenance"}

    failed = prepare_cron_alert(
        job,
        "connection timeout with raw body",
        success=False,
        error="connection timeout with raw body",
        policy=policy,
    )
    recovered = prepare_cron_alert(
        job,
        "maintenance complete",
        success=True,
        policy=policy,
    )

    assert failed is not None and failed.channel_id == CHANNELS["errors"]
    assert "raw body" not in failed.content
    assert recovered is not None and recovered.channel_id == CHANNELS["errors"]
    assert "recovered" in recovered.content


def test_gateway_status_alerts_dedupe_failure_and_emit_one_recovery():
    policy = _policy()
    key = "platform:telegram-test-unique"

    failed = prepare_gateway_alert(
        "degraded", detail="Telegram timeout", failure_key=key, policy=policy,
    )
    duplicate = prepare_gateway_alert(
        "degraded", detail="Telegram timeout", failure_key=key, policy=policy,
    )
    recovered = prepare_gateway_alert(
        "recovered", detail="Telegram", failure_key=key, policy=policy,
    )
    extra_recovery = prepare_gateway_alert(
        "recovered", detail="Telegram", failure_key=key, policy=policy,
    )

    assert failed is not None and failed.channel_id == CHANNELS["status"]
    assert duplicate is not None and duplicate.content == ""
    assert recovered is not None and "recovered" in recovered.content
    assert extra_recovery is not None and extra_recovery.content == ""


def test_prepared_kanban_alert_replaces_origin_thread_with_single_update_channel():
    alert = prepare_kanban_alert(
        "DONE: @worker Kanban t_123 — shipped\nDelivery: receipt is ready for review",
        metadata={"thread_id": "old-thread", "chat_type": "channel"},
        policy=_policy(),
    )

    assert alert is not None
    assert alert.channel_id == CHANNELS["kanban"]
    assert alert.content.startswith("What happened: DONE:")
    assert "receipt is ready for review" in alert.content
    assert alert.metadata == {
        "chat_type": "channel",
        "non_conversational": True,
        "discord_no_mentions": True,
    }
