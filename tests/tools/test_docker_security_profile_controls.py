import logging
import subprocess
import time

import pytest

from tools import terminal_tool
from tools.environments import docker as docker_env


def _fake_docker(monkeypatch, *, cgroups=True):
    calls = []
    docker_env._cgroup_limits_ok = cgroups
    monkeypatch.setattr(docker_env, "find_docker", lambda: "/usr/bin/docker")
    monkeypatch.setattr(docker_env, "_image_uses_init_entrypoint", lambda *args: False)

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        if len(cmd) > 1 and cmd[1] == "version":
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        if len(cmd) > 1 and cmd[1] == "run":
            return subprocess.CompletedProcess(cmd, 0, "container-id\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(docker_env.subprocess, "run", run)
    return calls


def test_docker_logs_redact_environment_values(monkeypatch, caplog):
    _fake_docker(monkeypatch)
    sentinel = "sentinel-secret-value"

    with caplog.at_level(logging.DEBUG):
        docker_env.DockerEnvironment(
            image="python:3.11",
            task_id="security-redaction",
            env={"PRIVATE_TOKEN": sentinel, "PUBLIC_MODE": "test"},
            mount_credentials=False,
            mount_skills=False,
            mount_caches=False,
            persist_across_processes=False,
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert sentinel not in rendered
    assert "PRIVATE_TOKEN=<redacted>" in rendered
    assert "PUBLIC_MODE=<redacted>" in rendered


def test_security_mount_switches_disable_automatic_host_mounts(monkeypatch):
    calls = _fake_docker(monkeypatch)
    from tools import credential_files

    monkeypatch.setattr(
        credential_files,
        "get_credential_file_mounts",
        lambda: [{"host_path": "/host/secret", "container_path": "/run/secret"}],
    )
    monkeypatch.setattr(
        credential_files,
        "get_skills_directory_mount",
        lambda: [{"host_path": "/host/skills", "container_path": "/root/.hermes/skills"}],
    )
    monkeypatch.setattr(
        credential_files,
        "get_cache_directory_mounts",
        lambda: [{"host_path": "/host/cache", "container_path": "/root/.hermes/cache"}],
    )

    docker_env.DockerEnvironment(
        image="python:3.11",
        task_id="security-no-auto-mounts",
        mount_credentials=False,
        mount_skills=False,
        mount_caches=False,
        persist_across_processes=False,
    )

    run_call = next(cmd for cmd in calls if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    rendered = " ".join(run_call)
    assert "/host/secret" not in rendered
    assert "/host/skills" not in rendered
    assert "/host/cache" not in rendered


def test_required_resource_limits_fail_closed(monkeypatch):
    _fake_docker(monkeypatch, cgroups=False)
    with pytest.raises(RuntimeError, match="requires CPU, memory, and PID limits"):
        docker_env.DockerEnvironment(
            image="python:3.11",
            task_id="security-require-limits",
            cpu=4,
            memory=8192,
            require_resource_limits=True,
            mount_credentials=False,
            mount_skills=False,
            mount_caches=False,
            persist_across_processes=False,
        )


def test_host_user_mode_adds_no_capabilities(monkeypatch):
    calls = _fake_docker(monkeypatch)
    monkeypatch.setattr(docker_env, "_resolve_host_user_spec", lambda: "1000:1000")

    docker_env.DockerEnvironment(
        image="python:3.11",
        task_id="security-nonroot-no-caps",
        run_as_host_user=True,
        mount_credentials=False,
        mount_skills=False,
        mount_caches=False,
        persist_across_processes=False,
    )

    run_call = next(cmd for cmd in calls if len(cmd) > 2 and cmd[1:3] == ["run", "-d"])
    assert "--user" in run_call
    assert "--cap-add" not in run_call
    assert run_call.count("--cap-drop") == 1
    assert run_call[run_call.index("--cap-drop") + 1] == "ALL"


def test_hard_lifetime_expires_even_with_active_background_process(monkeypatch):
    class DummyEnvironment:
        def __init__(self):
            self.cleaned = False

        def cleanup(self):
            self.cleaned = True

    from tools.process_registry import process_registry

    task_id = "hard-lifetime-test"
    env = DummyEnvironment()
    now = time.time()
    terminal_tool._active_environments[task_id] = env
    terminal_tool._last_activity[task_id] = now
    terminal_tool._created_at[task_id] = now - 120
    monkeypatch.setattr(process_registry, "has_active_processes", lambda key: key == task_id)

    try:
        terminal_tool._cleanup_inactive_envs(
            lifetime_seconds=3600,
            max_lifetime_seconds=60,
        )
        assert env.cleaned is True
        assert task_id not in terminal_tool._active_environments
        assert task_id not in terminal_tool._last_activity
        assert task_id not in terminal_tool._created_at
    finally:
        terminal_tool._active_environments.pop(task_id, None)
        terminal_tool._last_activity.pop(task_id, None)
        terminal_tool._created_at.pop(task_id, None)
