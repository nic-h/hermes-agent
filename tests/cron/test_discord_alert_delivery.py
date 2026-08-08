from unittest.mock import patch


from cron.scheduler import (
    _deliver_result,
    _filter_unrouted_discord_targets,
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
    assert routed_job["deliver"] == f"discord:{ARTICLE_CHANNEL}"
    assert routed_job["_discord_alert_metadata"] == {
        "non_conversational": True,
        "discord_no_mentions": True,
    }
    assert "What happened:" in content
    assert "Approve, Request changes, or Hold" in content
    assert "preview body" in content
    assert "Cronjob Response" not in content
    assert "cunicula-integration-route" not in content


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
    assert "did not complete" in content
    assert "result is ready" not in content
    assert "recovered" not in content


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
