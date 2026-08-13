from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from cron.scheduler import (
    _deliver_result,
    _filter_unrouted_discord_targets,
    _resolve_delivery_targets,
    run_one_job,
)
from gateway.discord_alerts import DiscordAlertPolicy


ERRORS_CHANNEL = "1492750576415412404"
ARTICLE_CHANNEL = "1535574256128106538"


def _policy():
    return DiscordAlertPolicy.from_config(
        {
            "gateway": {
                "discord_alerts": {
                    "channels": {
                        "errors": ERRORS_CHANNEL,
                        "article_reviews": ARTICLE_CHANNEL,
                    },
                    "projects": {"article_reviews": ["cunicula"]},
                }
            }
        }
    )


def test_run_one_job_routes_cunicula_preview_without_legacy_cron_wrapper():
    job = {
        "id": "cunicula-integration-route",
        "name": "Cunicula article preview",
        "deliver": "all",
        "schedule": {"kind": "interval", "minutes": 60},
    }

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-1"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job", return_value=(True, "saved output", "preview body", None)
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution"), patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert run_one_job(job) is True

    routed_job, content = deliver.call_args.args
    assert routed_job["deliver"] == f"all,discord:{ARTICLE_CHANNEL}"
    assert routed_job["_discord_alert_suppress_unrouted"] is True
    assert routed_job["_discord_alert_channel_id"] == ARTICLE_CHANNEL
    alert_content = routed_job["_discord_alert_content"]
    assert routed_job["_discord_alert_metadata"] == {
        "non_conversational": True,
        "discord_no_mentions": True,
    }
    assert "What happened:" in alert_content
    assert "Approve, Request changes, or Hold" in alert_content
    assert "preview body" in alert_content
    assert "Cronjob Response" not in alert_content
    assert "cunicula-integration-route" not in alert_content
    assert content == "preview body"


def test_empty_project_result_routes_as_failure_not_false_review_recovery():
    job = {
        "id": "cunicula-empty-result-route",
        "name": "Cunicula article preview",
        "deliver": "all",
        "schedule": {"kind": "interval", "minutes": 60},
    }

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-2"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job", return_value=(True, "saved output", "", None)
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution"), patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert run_one_job(job) is True

    routed_job, content = deliver.call_args.args
    assert routed_job["deliver"] == f"discord:{ERRORS_CHANNEL}"
    alert_content = routed_job["_discord_alert_content"]
    assert "scheduled review was not produced" in alert_content
    assert "result is ready" not in alert_content
    assert "recovered" not in alert_content
    assert not content


def test_routed_failure_appends_discord_without_losing_non_discord_targets():
    job = {
        "id": "mixed-failure-route",
        "name": "Nightly maintenance",
        "deliver": ["telegram:222"],
        "schedule": {"kind": "interval", "minutes": 60},
    }

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-mixed-1"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job",
        return_value=(False, "saved output", "", "connection timeout"),
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution"), patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert run_one_job(job) is True

    routed_job, content = deliver.call_args.args
    assert routed_job["deliver"] == f"telegram:222,discord:{ERRORS_CHANNEL}"
    assert routed_job["_discord_alert_suppress_unrouted"] is True
    assert routed_job["_discord_alert_channel_id"] == ERRORS_CHANNEL
    assert routed_job["_discord_alert_content"].startswith("What happened:")
    assert routed_job["_discord_alert_content"] != content
    assert content.strip()


def test_cooldown_suppresses_only_discord_and_keeps_non_discord_delivery():
    job = {
        "id": "mixed-cooldown-route",
        "name": "Nightly maintenance",
        "deliver": ["telegram:222"],
        "schedule": {"kind": "interval", "minutes": 60},
    }

    def run_once(execution_id):
        with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
            "cron.scheduler.create_execution", return_value={"id": execution_id}
        ), patch("cron.scheduler.mark_execution_running"), patch(
            "cron.scheduler.run_job",
            return_value=(False, "saved output", "", "connection timeout"),
        ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
            "cron.scheduler.mark_job_run"
        ), patch("cron.scheduler.finish_execution"), patch(
            "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
        ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
            assert run_one_job(job) is True
        return deliver.call_args.args

    first_job, _ = run_once("execution-cooldown-1")
    second_job, second_content = run_once("execution-cooldown-2")

    assert first_job["deliver"] == f"telegram:222,discord:{ERRORS_CHANNEL}"
    assert second_job["deliver"] == ["telegram:222"]
    assert second_job["_discord_alert_suppress_unrouted"] is True
    assert "_discord_alert_content" not in second_job
    assert second_content.strip()


def test_deliver_result_selects_alert_content_only_for_discord_target():
    from gateway.config import Platform

    normal_content = "normal cron failure summary"
    alert_content = (
        "What happened: Nightly maintenance timed out.\n"
        "Impact: The scheduled review was not produced.\n"
        "Action: Check provider availability."
    )
    job = {
        "id": "per-target-content-route",
        "name": "Nightly maintenance",
        "deliver": f"telegram:222,discord:{ERRORS_CHANNEL}",
        "_discord_alert_suppress_unrouted": True,
        "_discord_alert_channel_id": ERRORS_CHANNEL,
        "_discord_alert_content": alert_content,
        "_discord_alert_metadata": {
            "non_conversational": True,
            "discord_no_mentions": True,
        },
    }
    config = SimpleNamespace(
        platforms={
            Platform.TELEGRAM: SimpleNamespace(enabled=True),
            Platform.DISCORD: SimpleNamespace(enabled=True),
        }
    )

    with patch(
        "gateway.config.load_gateway_config", return_value=config
    ), patch(
        "cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}
    ), patch(
        "tools.send_message_tool._send_to_platform",
        new=AsyncMock(return_value={"success": True}),
    ) as send:
        assert _deliver_result(job, normal_content) is None

    by_platform = {
        call.args[0]: call.args[3]
        for call in send.await_args_list
    }
    assert by_platform[Platform.TELEGRAM] == normal_content
    assert by_platform[Platform.DISCORD] == alert_content


def test_silent_project_result_remains_silent_instead_of_becoming_review_alert():
    job = {
        "id": "cunicula-silent-route",
        "name": "Cunicula article preview",
        "deliver": "all",
        "schedule": {"kind": "interval", "minutes": 60},
    }

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-3"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job", return_value=(True, "saved output", "[SILENT]", None)
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution"), patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert run_one_job(job) is True

    deliver.assert_not_called()


def test_generic_success_marks_discord_for_suppression_without_losing_other_targets():
    job = {
        "id": "generic-success-route",
        "name": "Nightly maintenance",
        "deliver": ["discord:111111111111111111", "telegram:222"],
        "schedule": {"kind": "interval", "minutes": 60},
    }

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-4"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job", return_value=(True, "saved output", "all good", None)
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution"), patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", return_value=None) as deliver:
        assert run_one_job(job) is True

    routed_job, content = deliver.call_args.args
    assert routed_job["_discord_alert_suppress_unrouted"] is True
    assert routed_job["deliver"] == job["deliver"]
    assert content == "all good"


def test_unrouted_filter_keeps_explicit_discord_and_non_discord_targets():
    targets = [
        {"platform": "discord", "chat_id": "111111111111111111", "thread_id": None},
        {"platform": "telegram", "chat_id": "222", "thread_id": None},
    ]
    explicit_job = {
        "deliver": ["discord:111111111111111111", "telegram:222"],
    }
    broad_job = {"deliver": "all"}

    kept_explicit, explicit_suppressed = _filter_unrouted_discord_targets(
        explicit_job, targets,
    )
    kept_broad, broad_suppressed = _filter_unrouted_discord_targets(
        broad_job, targets,
    )

    assert kept_explicit == targets
    assert explicit_suppressed == 0
    assert kept_broad == [targets[1]]
    assert broad_suppressed == 1


def test_unrouted_filter_keeps_discord_origin_target():
    job = {
        "deliver": "origin",
        "origin": {
            "platform": "discord",
            "chat_id": "555000111",
            "thread_id": "777",
        },
    }
    targets = _resolve_delivery_targets(job)

    kept, suppressed = _filter_unrouted_discord_targets(job, targets)

    assert kept == targets
    assert suppressed == 0


def test_deliver_result_marks_all_filtered_discord_targets_as_suppressed():
    job = {
        "id": "suppressed-discord-only",
        "deliver": "all",
        "_discord_alert_suppress_unrouted": True,
    }
    target = {"platform": "discord", "chat_id": "111111111111111111"}

    with patch("cron.scheduler._resolve_delivery_targets", return_value=[target]):
        assert _deliver_result(job, "generic success") is None

    assert job["_discord_alert_delivery_suppressed"] is True
    assert job["_discord_alert_all_targets_suppressed"] is True


def test_run_one_job_records_fully_filtered_delivery_as_suppressed():
    job = {
        "id": "generic-broad-success-route",
        "name": "Nightly maintenance",
        "deliver": "all",
        "schedule": {"kind": "interval", "minutes": 60},
    }

    def suppress_all(routed_job, _content, **_kwargs):
        routed_job["_discord_alert_all_targets_suppressed"] = True
        return None

    with patch("cron.scheduler.claim_dispatch", return_value=True), patch(
        "cron.scheduler.create_execution", return_value={"id": "execution-5"}
    ), patch("cron.scheduler.mark_execution_running"), patch(
        "cron.scheduler.run_job", return_value=(True, "saved output", "all good", None)
    ), patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), patch(
        "cron.scheduler.mark_job_run"
    ), patch("cron.scheduler.finish_execution") as finish, patch(
        "gateway.discord_alerts.load_discord_alert_policy", return_value=_policy()
    ), patch("cron.scheduler._deliver_result", side_effect=suppress_all):
        assert run_one_job(job) is True

    assert finish.call_args.kwargs["delivery_outcome"] == "suppressed"
