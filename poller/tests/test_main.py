"""Unit tests for slurm_license_poller.main."""

# SPDX-License-Identifier: Apache-2.0

# pylint: disable=redefined-outer-name,unused-argument,too-few-public-methods
# Standard pytest fixture idioms and small test doubles.

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from slurm_license_poller import main as m

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class FakeService(m.QuantumService):
    """QuantumService whose answers are scripted per backend."""

    def __init__(self, script: dict[str, list[bool | Exception]]):
        self._script = {k: list(v) for k, v in script.items()}

    def is_busy(self, backend_name: str) -> bool:
        result = self._script[backend_name].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def sacctmgr(monkeypatch):
    """Record sacctmgr calls instead of running them."""
    calls: list[tuple[str, int]] = []
    behaviour: dict[str, BaseException | None] = {"raise": None}

    def fake_run(cmd, **kwargs):
        assert kwargs["check"] is True
        assert kwargs["timeout"] > 0
        exc = behaviour["raise"]
        if exc is not None:
            raise exc
        calls.append((cmd[4], int(cmd[6].split("=")[1])))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    return SimpleNamespace(calls=calls, behaviour=behaviour)


@pytest.fixture
def clock(monkeypatch):
    """Controllable time.monotonic()."""
    now = {"t": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: now["t"])
    return now


def write_config(tmp_path, **overrides):
    raw = {"config_path": "/etc/qrmi.json", "resources": ["a"], "poll_interval": 10}
    raw.update(overrides)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    return str(path)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_config_defaults(tmp_path):
    cfg = m.Config.from_file(write_config(tmp_path))
    assert cfg.resources == ["a"]
    assert cfg.poll_interval == 10.0
    assert cfg.failure_threshold == m.DEFAULT_FAILURE_THRESHOLD
    assert cfg.resync_interval == m.DEFAULT_RESYNC_INTERVAL
    assert cfg.sacctmgr_timeout == m.DEFAULT_SACCTMGR_TIMEOUT
    assert not cfg.unknown_keys


def test_config_unknown_keys_reported(tmp_path):
    cfg = m.Config.from_file(write_config(tmp_path, backends=["x"]))
    assert cfg.unknown_keys == ["backends"]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"resources": "a"}, "must be of type list"),
        ({"resources": []}, "must not be empty"),
        ({"resources": ["a", 1]}, "non-empty strings"),
        ({"resources": ["a", "a"]}, "duplicates"),
        ({"poll_interval": "60"}, "must be of type int or float"),
        ({"poll_interval": True}, "must be of type int or float"),
        ({"poll_interval": 0}, "must be > 0"),
        ({"failure_threshold": 0}, "must be >= 1"),
        ({"failure_threshold": 1.5}, "must be of type int"),
        ({"sacctmgr_timeout": -1}, "must be > 0"),
        ({"config_path": 1}, "must be of type str"),
    ],
)
def test_config_rejects_bad_values(tmp_path, overrides, message):
    with pytest.raises(m.ConfigError, match=message):
        m.Config.from_file(write_config(tmp_path, **overrides))


def test_config_missing_keys(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"resources": ["a"]}))
    with pytest.raises(m.ConfigError, match="config_path, poll_interval"):
        m.Config.from_file(str(path))


def test_config_unreadable(tmp_path):
    with pytest.raises(m.ConfigError, match="Failed to read"):
        m.Config.from_file(str(tmp_path / "missing.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    with pytest.raises(m.ConfigError, match="Failed to read"):
        m.Config.from_file(str(bad))


def test_configure_logging_errors(tmp_path):
    with pytest.raises(m.ConfigError, match="Invalid log level"):
        m.configure_logging(level_name="LOUD")
    bad = tmp_path / "log.json"
    bad.write_text(json.dumps({"version": 1, "handlers": {"h": {"class": "no.Such"}}}))
    with pytest.raises(m.ConfigError, match="Failed to apply log config"):
        m.configure_logging(log_config_path=str(bad))


# --------------------------------------------------------------------------
# QRMI service
# --------------------------------------------------------------------------


class FakeResource:
    def __init__(self, status=None, exc=None):
        self._status, self._exc = status, exc

    def status(self):
        if self._exc:
            raise self._exc
        return SimpleNamespace(to_dict=lambda: self._status)


def make_qrmi(monkeypatch, tmp_path, resource):
    res_def = SimpleNamespace(name="a", resource_type="t", environment={})
    monkeypatch.setattr(
        m,
        "QRMIConfig",
        SimpleNamespace(load=lambda _p: SimpleNamespace(resource_map={"a": res_def})),
    )
    monkeypatch.setattr(
        m, "QuantumResource", SimpleNamespace(from_config=lambda *_: resource)
    )
    return m.QRMI(m.Config.from_file(write_config(tmp_path)))


@pytest.mark.parametrize(
    "status, busy",
    [
        ({"status": "online", "healthy": None, "busy": None}, False),
        ({"status": "online", "healthy": True, "busy": False}, False),
        ({"status": "online", "healthy": False, "busy": False}, True),
        ({"status": "online", "healthy": True, "busy": True}, True),
        ({"status": "offline", "healthy": None, "busy": None}, True),
        ({"status": "paused", "healthy": True, "busy": False}, True),
    ],
)
def test_qrmi_is_busy(monkeypatch, tmp_path, status, busy):
    svc = make_qrmi(monkeypatch, tmp_path, FakeResource(status=status))
    assert svc.is_busy("a") is busy


def test_qrmi_is_busy_wraps_errors(monkeypatch, tmp_path):
    svc = make_qrmi(monkeypatch, tmp_path, FakeResource(exc=RuntimeError("401")))
    with pytest.raises(m.BackendStatusError, match="401") as info:
        svc.is_busy("a")
    assert isinstance(info.value.__cause__, RuntimeError)


def test_qrmi_init_unknown_resource(monkeypatch, tmp_path):
    monkeypatch.setattr(
        m,
        "QRMIConfig",
        SimpleNamespace(load=lambda _p: SimpleNamespace(resource_map={"other": None})),
    )
    with pytest.raises(m.ServiceInitError, match="'a' is not defined.*other"):
        m.QRMI(m.Config.from_file(write_config(tmp_path)))


def test_qrmi_init_load_failure(monkeypatch, tmp_path):
    def boom(_p):
        raise OSError("no such file")

    monkeypatch.setattr(m, "QRMIConfig", SimpleNamespace(load=boom))
    with pytest.raises(m.ServiceInitError, match="no such file"):
        m.QRMI(m.Config.from_file(write_config(tmp_path)))


# --------------------------------------------------------------------------
# update_slurm_license
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.CalledProcessError(1, "sacctmgr", stderr="denied"),
        subprocess.TimeoutExpired("sacctmgr", 30),
        FileNotFoundError("sacctmgr"),
    ],
)
def test_update_slurm_license_failures(sacctmgr, exc):
    sacctmgr.behaviour["raise"] = exc
    assert m.update_slurm_license("a", True, timeout=30) is False


def test_update_slurm_license_success(sacctmgr):
    assert m.update_slurm_license("a", False, timeout=30) is True
    assert sacctmgr.calls == [("a", 0)]


# --------------------------------------------------------------------------
# Poller
# --------------------------------------------------------------------------


def test_poller_writes_only_on_change(sacctmgr, clock):
    svc = FakeService({"a": [False, False, True, True, False]})
    poller = m.Poller(svc, ["a"], 10, resync_interval=1e9)
    for _ in range(5):
        poller.poll_once()
    assert sacctmgr.calls == [("a", 0), ("a", 1), ("a", 0)]


def test_poller_resyncs_periodically(sacctmgr, clock):
    svc = FakeService({"a": [False, False, False]})
    poller = m.Poller(svc, ["a"], 10, resync_interval=100)
    poller.poll_once()
    clock["t"] += 50
    poller.poll_once()  # unchanged, not stale yet
    clock["t"] += 60
    poller.poll_once()  # unchanged but stale -> re-written
    assert sacctmgr.calls == [("a", 0), ("a", 0)]


def test_poller_fails_closed_after_threshold(sacctmgr, clock):
    err = m.BackendStatusError("down")
    svc = FakeService({"a": [False, err, err, err, err, False]})
    poller = m.Poller(svc, ["a"], 10, failure_threshold=3, resync_interval=1e9)
    for _ in range(6):
        poller.poll_once()
    # idle -> (2 tolerated failures) -> 3rd failure locks -> 4th keeps -> recovers
    assert sacctmgr.calls == [("a", 0), ("a", 1), ("a", 0)]


def test_poller_retries_failed_write(sacctmgr, clock):
    svc = FakeService({"a": [False, False]})
    poller = m.Poller(svc, ["a"], 10, resync_interval=1e9)
    sacctmgr.behaviour["raise"] = subprocess.TimeoutExpired("sacctmgr", 30)
    poller.poll_once()
    sacctmgr.behaviour["raise"] = None
    poller.poll_once()
    assert sacctmgr.calls == [("a", 0)]


def test_poller_isolates_unexpected_errors(sacctmgr, clock):
    svc = FakeService({"a": [KeyError("bug")], "b": [False]})
    poller = m.Poller(svc, ["a", "b"], 10)
    poller.poll_once()
    assert sacctmgr.calls == [("b", 0)]


def test_poller_lock_all_and_stop(sacctmgr, clock):
    svc = FakeService({"a": [False], "b": [True]})
    poller = m.Poller(svc, ["a", "b"], 10, resync_interval=1e9)
    poller.poll_once()
    poller.stop()
    poller.run()  # returns immediately once stopped
    poller.lock_all()
    assert sacctmgr.calls == [("a", 0), ("b", 1), ("a", 1), ("b", 1)]


def test_main_exits_on_service_init_error(monkeypatch, tmp_path, sacctmgr):
    def boom(_cfg):
        raise m.ServiceInitError("bad qrmi config")

    monkeypatch.setattr(m, "create_service", boom)
    monkeypatch.setattr("sys.argv", ["slp", "--config", write_config(tmp_path)])
    with pytest.raises(SystemExit) as info:
        m.main()
    assert info.value.code == 1
    assert sacctmgr.calls == []
