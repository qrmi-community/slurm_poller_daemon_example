"""Poller daemon that syncs Slurm dynamic licenses with the status of QRMI quantum resources."""

# SPDX-License-Identifier: Apache-2.0

# pylint: disable=too-few-public-methods
# QuantumService and its implementations are intentionally single-method
# (Strategy pattern); splitting them up would not improve readability.

from __future__ import annotations

import argparse
import json
import logging
import logging.config
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# pylint: disable=no-name-in-module
from qrmi import Config as QRMIConfig
from qrmi import QuantumResource

logger = logging.getLogger(__name__)

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_RESYNC_INTERVAL = 300.0
DEFAULT_SACCTMGR_TIMEOUT = 30.0

LICENSE_FREE = 0
LICENSE_CONSUMED = 1


class ConfigError(ValueError):
    """Raised when the config file (or the logging config) is missing or malformed."""


class ServiceInitError(RuntimeError):
    """Raised when the quantum service cannot be initialized."""


class BackendStatusError(RuntimeError):
    """Raised when the status of a backend cannot be determined."""


def configure_logging(
    *, level_name: str = DEFAULT_LOG_LEVEL, log_config_path: str | None = None
) -> None:
    """Configure logging.

    If log_config_path is given, it takes full precedence: the file is loaded
    as JSON and applied via logging.config.dictConfig(), so handlers (e.g. a
    file or rotating file handler), formatters, and per-logger levels can all
    be defined externally without touching this script. See the Python docs for the
    dictConfig schema: https://docs.python.org/3/library/logging.config.html#dictconfig-format

    Otherwise, falls back to a simple root-level console logger at level_name.

    Args:
        level_name: A standard logging level name (e.g. "DEBUG", "INFO").
            Ignored if log_config_path is given.
        log_config_path: Optional path to a JSON logging config file
            (dictConfig schema).

    Raises:
        ConfigError: If level_name is invalid, or log_config_path can't be
            read/parsed/applied.
    """
    if log_config_path:
        try:
            with open(log_config_path, "r", encoding="utf-8") as f:
                log_config = json.load(f)
            logging.config.dictConfig(log_config)
        except (OSError, ValueError, TypeError, AttributeError, ImportError) as e:
            # json.JSONDecodeError is a ValueError subclass. dictConfig may
            # raise any of the others for a malformed schema.
            raise ConfigError(
                f"Failed to apply log config '{log_config_path}': {e}"
            ) from e
        return

    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        raise ConfigError(
            f"Invalid log level: {level_name!r}. "
            f"Expected one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
        )
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger().setLevel(level)  # in case basicConfig was already called


def _require_type(raw: dict[str, Any], key: str, types: tuple[type, ...]) -> Any:
    """Return raw[key] if it is an instance of types, otherwise raise ConfigError."""
    value = raw[key]
    # bool is a subclass of int; never accept it where a number is expected.
    if isinstance(value, bool) or not isinstance(value, types):
        expected = " or ".join(t.__name__ for t in types)
        raise ConfigError(
            f"Config key '{key}' must be of type {expected}, "
            f"got {type(value).__name__}: {value!r}"
        )
    return value


def _positive_number(raw: dict[str, Any], key: str, default: float) -> float:
    """Return raw[key] (or default) as a float, ensuring it is > 0."""
    if key not in raw:
        return default
    value = float(_require_type(raw, key, (int, float)))
    if value <= 0:
        raise ConfigError(f"Config key '{key}' must be > 0, got {value!r}")
    return value


@dataclass
class Config:  # pylint: disable=too-many-instance-attributes
    """Typed representation of the poller's config file.

    A plain data holder mirroring the config.json schema, so the attribute
    count tracks the number of supported config keys rather than complexity.
    """

    config_path: str
    resources: list[str]
    poll_interval: float
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    resync_interval: float = DEFAULT_RESYNC_INTERVAL
    sacctmgr_timeout: float = DEFAULT_SACCTMGR_TIMEOUT
    log_level: str = DEFAULT_LOG_LEVEL
    log_config: str | None = None
    unknown_keys: list[str] = field(default_factory=list)

    REQUIRED_KEYS = frozenset({"config_path", "resources", "poll_interval"})
    OPTIONAL_KEYS = frozenset(
        {
            "failure_threshold",
            "resync_interval",
            "sacctmgr_timeout",
            "log_level",
            "log_config",
        }
    )

    @classmethod
    def from_file(cls, path: str) -> Config:
        """Load and validate a config file.

        Args:
            path: Path to the JSON config file.

        Returns:
            A validated Config instance.

        Raises:
            ConfigError: If the file can't be read or parsed, is missing
                required keys, or contains values of the wrong type/range.
        """
        try:
            with open(path, "r", encoding="utf-8") as config_file:
                raw = json.load(config_file)
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"Failed to read config file '{path}': {e}") from e

        if not isinstance(raw, dict):
            raise ConfigError(f"Config file '{path}' must contain a JSON object")

        missing = cls.REQUIRED_KEYS - raw.keys()
        if missing:
            raise ConfigError(
                f"Missing required config keys: {', '.join(sorted(missing))}. "
                f"Please check your config file: {path}"
            )

        config_path = _require_type(raw, "config_path", (str,))

        resources = _require_type(raw, "resources", (list,))
        if not resources:
            raise ConfigError("Config key 'resources' must not be empty")
        if not all(isinstance(r, str) and r for r in resources):
            raise ConfigError(
                f"Config key 'resources' must be a list of non-empty strings, "
                f"got {resources!r}"
            )
        if len(set(resources)) != len(resources):
            raise ConfigError(f"Config key 'resources' has duplicates: {resources!r}")

        poll_interval = _positive_number(raw, "poll_interval", 0.0)
        resync_interval = _positive_number(
            raw, "resync_interval", DEFAULT_RESYNC_INTERVAL
        )
        sacctmgr_timeout = _positive_number(
            raw, "sacctmgr_timeout", DEFAULT_SACCTMGR_TIMEOUT
        )

        failure_threshold = DEFAULT_FAILURE_THRESHOLD
        if "failure_threshold" in raw:
            failure_threshold = _require_type(raw, "failure_threshold", (int,))
            if failure_threshold < 1:
                raise ConfigError(
                    f"Config key 'failure_threshold' must be >= 1, "
                    f"got {failure_threshold!r}"
                )

        log_level = DEFAULT_LOG_LEVEL
        if "log_level" in raw:
            log_level = _require_type(raw, "log_level", (str,))

        log_config = None
        if raw.get("log_config") is not None:
            log_config = _require_type(raw, "log_config", (str,))

        return cls(
            config_path=config_path,
            resources=list(resources),
            poll_interval=poll_interval,
            failure_threshold=failure_threshold,
            resync_interval=resync_interval,
            sacctmgr_timeout=sacctmgr_timeout,
            log_level=log_level,
            log_config=log_config,
            unknown_keys=sorted(raw.keys() - cls.REQUIRED_KEYS - cls.OPTIONAL_KEYS),
        )


class QuantumService(ABC):
    """Abstract base class for quantum service backends."""

    @abstractmethod
    def is_busy(self, backend_name: str) -> bool:
        """Check whether a backend is currently unavailable for new work.

        Args:
            backend_name: Quantum backend name. Must be one of the resources
                the service was configured with.

        Returns:
            True if busy (or otherwise unavailable), False if idle.

        Raises:
            BackendStatusError: If the status could not be determined. The
                caller decides how to handle the failure (e.g. retry, or
                fail closed after repeated failures).
        """


class QRMI(QuantumService):
    """QRMI implementation of QuantumService."""

    def __init__(self, config: Config):
        """Initialize the QRMI service.

        Args:
            config: Poller configuration

        Raises:
            ServiceInitError: If the QRMI config can't be loaded, a configured
                resource is not defined in it, or a resource can't be created.
        """
        self._quantum_resource_map: dict[str, QuantumResource] = {}
        try:
            qrmi_config = QRMIConfig.load(config.config_path)
        except Exception as e:  # pylint: disable=broad-except
            # qrmi raises its own exception hierarchy (and OSError/ValueError
            # from the native layer); all of them are fatal at startup.
            raise ServiceInitError(
                f"Failed to load QRMI config '{config.config_path}': {e}"
            ) from e

        resource_map = qrmi_config.resource_map
        for resource_id in config.resources:
            if resource_id not in resource_map:
                raise ServiceInitError(
                    f"Resource '{resource_id}' is not defined in QRMI config "
                    f"'{config.config_path}'. Defined resources: "
                    f"{', '.join(sorted(resource_map)) or '(none)'}"
                )
            res_def = resource_map[resource_id]
            try:
                self._quantum_resource_map[resource_id] = QuantumResource.from_config(
                    res_def.name, res_def.resource_type, res_def.environment
                )
            except Exception as e:  # pylint: disable=broad-except
                raise ServiceInitError(
                    f"Failed to initialize QRMI resource '{resource_id}': {e}"
                ) from e

    def is_busy(self, backend_name: str) -> bool:
        """Check whether a backend is currently unavailable for new work.

        A backend is considered idle only if it is online and does not report
        itself as unhealthy or busy. Vendors that don't report "healthy" or
        "busy" leave those fields as None; None is treated as "no objection",
        so such backends are idle whenever they are online. Offline and paused
        backends are reported as busy so that Slurm keeps jobs pending.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle.

        Raises:
            KeyError: If backend_name was not configured (a programming error).
            BackendStatusError: If the status could not be retrieved.
        """
        res = self._quantum_resource_map[backend_name]
        try:
            status = res.status().to_dict()
        except Exception as e:  # pylint: disable=broad-except
            # Wrap whatever qrmi raises (network, auth, vendor API errors) so
            # the caller only has to handle one exception type.
            raise BackendStatusError(
                f"Failed to obtain status of {backend_name}: {e}"
            ) from e

        logger.debug("Status of %s: %s", backend_name, status)
        is_idle = (
            status.get("status") == "online"
            and status.get("healthy") in (True, None)
            and status.get("busy") in (False, None)
        )
        return not is_idle


def create_service(config: Config) -> QuantumService:
    """Instantiate the QuantumService for the given config.

    Args:
        config: Validated poller configuration.

    Returns:
        A QuantumService implementation.

    Raises:
        ServiceInitError: If the service can't be initialized.
    """
    return QRMI(config=config)


def update_slurm_license(backend_name: str, is_busy: bool, timeout: float) -> bool:
    """Set the consumed count of a Slurm dynamic license.

    Args:
        backend_name: Quantum backend name (= Slurm license name)
        is_busy: True to mark the license as consumed, False to release it.
        timeout: Seconds to wait for sacctmgr before giving up.

    Returns:
        True if succeeded, otherwise False
    """
    last_consumed = LICENSE_CONSUMED if is_busy else LICENSE_FREE
    cmd = [
        "sacctmgr",
        "-i",
        "update",
        "resource",
        backend_name,
        "set",
        f"lastconsumed={last_consumed}",
    ]
    logger.info("%s", cmd)
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        logger.error(
            "sacctmgr failed for %s (exit code %d): %s",
            backend_name,
            e.returncode,
            (e.stderr or e.stdout or "").strip(),
        )
        return False
    except subprocess.TimeoutExpired:
        logger.error("sacctmgr timed out after %.0fs for %s", timeout, backend_name)
        return False
    except OSError as e:
        # e.g. sacctmgr not found in PATH, or not executable.
        logger.error("Failed to run sacctmgr for %s: %s", backend_name, e)
        return False

    return True


@dataclass
class _BackendState:
    """Per-backend bookkeeping for the poller."""

    # Last value successfully written to Slurm (None = never written).
    synced_busy: bool | None = None
    # time.monotonic() of the last successful write.
    synced_at: float = 0.0
    consecutive_failures: int = 0


class Poller:  # pylint: disable=too-many-instance-attributes
    """Polls backend status and syncs Slurm dynamic licenses.

    Policy:
      * A license is written whenever the observed state changes, and
        re-written every resync_interval seconds even if it hasn't, so that
        manual edits or a restored slurmdbd are corrected.
      * If a backend's status can't be determined failure_threshold times in
        a row, the license is marked consumed (fail closed), so jobs don't run
        against a backend whose state is unknown.
      * On shutdown, every license is marked consumed, since nothing will
        maintain it afterwards.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        service: QuantumService,
        backends: list[str],
        poll_interval: float,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        resync_interval: float = DEFAULT_RESYNC_INTERVAL,
        sacctmgr_timeout: float = DEFAULT_SACCTMGR_TIMEOUT,
    ):
        self._service = service
        self._backends = backends
        self._poll_interval = poll_interval
        self._failure_threshold = failure_threshold
        self._resync_interval = resync_interval
        self._sacctmgr_timeout = sacctmgr_timeout
        self._states = {backend: _BackendState() for backend in backends}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Request the poll loop to stop. Safe to call from a signal handler."""
        self._stop_event.set()

    @property
    def stopping(self) -> bool:
        """True once stop() has been called."""
        return self._stop_event.is_set()

    def _sync(self, backend: str, is_busy: bool, *, force: bool = False) -> None:
        """Write the license for backend if needed, and record the result."""
        state = self._states[backend]
        now = time.monotonic()
        changed = is_busy != state.synced_busy
        stale = now - state.synced_at >= self._resync_interval
        if not (force or changed or stale):
            return

        if changed:
            logger.info("Backend %s is %s", backend, "busy" if is_busy else "idle")
        else:
            logger.debug(
                "Re-syncing license of %s (%s)", backend, "busy" if is_busy else "idle"
            )
        if update_slurm_license(backend, is_busy, self._sacctmgr_timeout):
            state.synced_busy = is_busy
            state.synced_at = now

    def _poll_backend(self, backend: str) -> None:
        state = self._states[backend]
        try:
            is_busy = self._service.is_busy(backend)
        except BackendStatusError as e:
            state.consecutive_failures += 1
            if state.consecutive_failures < self._failure_threshold:
                logger.warning(
                    "%s (failure %d/%d)",
                    e,
                    state.consecutive_failures,
                    self._failure_threshold,
                )
                return
            if state.consecutive_failures == self._failure_threshold:
                logger.error(
                    "%s; %d consecutive failures, marking license as consumed",
                    e,
                    state.consecutive_failures,
                )
            else:
                logger.warning(
                    "%s (failure %d, license kept consumed)",
                    e,
                    state.consecutive_failures,
                )
            is_busy = True
        else:
            if state.consecutive_failures >= self._failure_threshold:
                logger.info(
                    "Status of %s is available again after %d failures",
                    backend,
                    state.consecutive_failures,
                )
            state.consecutive_failures = 0

        self._sync(backend, is_busy)

    def poll_once(self) -> None:
        """Check every configured backend once and update licenses as needed."""
        for backend in self._backends:
            if self.stopping:
                return
            try:
                self._poll_backend(backend)
            except Exception:  # pylint: disable=broad-except
                # Last line of defense: an unexpected bug for one backend must
                # neither crash the daemon nor starve the other backends.
                logger.exception("Unexpected error while polling %s", backend)

    def lock_all(self) -> None:
        """Mark every license as consumed. Used on shutdown."""
        for backend in self._backends:
            self._sync(backend, True, force=True)

    def run(self) -> None:
        """Run the poll loop until stop() is called."""
        while not self.stopping:
            self.poll_once()
            self._stop_event.wait(self._poll_interval)


def install_signal_handlers(poller: Poller) -> None:
    """Make SIGINT/SIGTERM stop the poller gracefully.

    The first signal asks the loop to stop after the current backend; the
    handler only sets an event, so an in-flight QRMI call or sacctmgr update
    is never torn down halfway. A second signal aborts whatever is running
    (including the shutdown lock_all()) by raising SystemExit.

    The handler deliberately does not log: logging from a signal handler can
    interleave with, or re-enter, a log call already in progress.
    """

    def _handler(signum: int, _frame: Any) -> None:
        if poller.stopping:
            raise SystemExit(128 + signum)
        poller.stop()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Slurm License Poller")
    parser.add_argument("--config", default="./config.json", help="config json file")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help=(
            f"Overrides the config file's log_level (default: {DEFAULT_LOG_LEVEL}). "
            "Ignored if --log-config or the config's log_config is set."
        ),
    )
    parser.add_argument(
        "--log-config",
        default=None,
        help="Path to a JSON logging.config.dictConfig file. Overrides "
        "--log-level and the config file's log_level entirely.",
    )
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    args = parse_args()

    try:
        # Configure logging as early as possible using just the CLI, so config
        # load failures are still logged at a sensible level. Reconfigured
        # below once the config file's log_level/log_config (if any) is known.
        if args.log_config:
            configure_logging(log_config_path=args.log_config)
        else:
            configure_logging(level_name=args.log_level or DEFAULT_LOG_LEVEL)

        config = Config.from_file(args.config)

        if args.log_config:
            pass  # CLI --log-config already applied and takes full precedence.
        elif config.log_config:
            configure_logging(log_config_path=config.log_config)
        elif args.log_level is None:
            configure_logging(level_name=config.log_level)
    except ConfigError as e:
        # Logging may not be configured if --log-config itself was bad;
        # logging's last-resort handler still prints ERROR records to stderr.
        logger.error("%s", e)
        raise SystemExit(1) from e

    if config.unknown_keys:
        logger.warning(
            "Ignoring unknown config keys: %s", ", ".join(config.unknown_keys)
        )

    try:
        service = create_service(config)
    except ServiceInitError as e:
        logger.error("%s", e)
        raise SystemExit(1) from e

    poller = Poller(
        service,
        config.resources,
        config.poll_interval,
        failure_threshold=config.failure_threshold,
        resync_interval=config.resync_interval,
        sacctmgr_timeout=config.sacctmgr_timeout,
    )
    install_signal_handlers(poller)

    logger.info(
        "Starting: resources=%s, poll_interval=%ss",
        ", ".join(config.resources),
        config.poll_interval,
    )
    try:
        poller.run()
        logger.info("Shutdown requested.")
    finally:
        logger.info("Marking all licenses as consumed before exit.")
        poller.lock_all()
        logger.info("Shut down.")


if __name__ == "__main__":
    main()
