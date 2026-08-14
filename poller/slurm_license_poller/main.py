"""A poller daemon to update Slurm dynamic license counts based on the current status of IBM Quantum backends"""

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
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import requests
from ibm_cloud_sdk_core.authenticators import IAMAuthenticator
from qiskit_ibm_runtime import QiskitRuntimeService, Session
from qiskit_ibm_runtime.exceptions import IBMInputValueError

logger = logging.getLogger(__name__)

DEFAULT_LOG_LEVEL = "INFO"


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
        except (OSError, json.JSONDecodeError, ValueError) as e:
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


# Keys required regardless of service type.
COMMON_REQUIRED_KEYS = {"type", "service_crn", "api_token", "backends", "poll_interval"}

# Extra keys required for specific service types.
TYPE_SPECIFIC_REQUIRED_KEYS: dict[str, set[str]] = {
    "ibm-quantum-compute": set(),
    "ibm-quantum-system": {"endpoint_url"},
}

# Default IAM endpoint used when a config doesn't specify iam_endpoint_url.
DEFAULT_IAM_ENDPOINT_URL = "https://iam.cloud.ibm.com"


class ConfigError(ValueError):
    """Raised when the config file is missing or malformed."""


@dataclass
class Config:  # pylint: disable=too-many-instance-attributes
    """Typed representation of the poller's config file.

    A plain data holder mirroring the config.json schema, so the attribute
    count tracks the number of supported config keys rather than complexity.
    """

    type: str
    service_crn: str
    api_token: str
    backends: list[str]
    poll_interval: float
    endpoint_url: str | None = None
    iam_endpoint_url: str = DEFAULT_IAM_ENDPOINT_URL
    log_level: str = DEFAULT_LOG_LEVEL
    log_config: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str) -> "Config":
        """Load and validate a config file.

        Args:
            path: Path to the JSON config file.

        Returns:
            A validated Config instance.

        Raises:
            ConfigError: If the file is missing required keys or has an
                unsupported service type.
        """
        with open(path, "r", encoding="utf-8") as config_file:
            raw: dict[str, Any] = json.load(config_file)

        missing = COMMON_REQUIRED_KEYS - raw.keys()
        if missing:
            raise ConfigError(
                f"Missing required config keys: {', '.join(sorted(missing))}. "
                f"Please check your config file: {path}"
            )

        service_type = raw["type"]
        if service_type not in TYPE_SPECIFIC_REQUIRED_KEYS:
            supported = ", ".join(sorted(TYPE_SPECIFIC_REQUIRED_KEYS))
            raise ConfigError(
                f"Unsupported service type: {service_type}. Supported types: {supported}. "
                f"Please check your config file: {path}"
            )

        type_missing = TYPE_SPECIFIC_REQUIRED_KEYS[service_type] - raw.keys()
        if type_missing:
            raise ConfigError(
                f"Missing config keys required for type '{service_type}': "
                f"{', '.join(sorted(type_missing))}. Please check your config file: {path}"
            )

        known_keys = COMMON_REQUIRED_KEYS | {
            "endpoint_url",
            "iam_endpoint_url",
            "log_level",
            "log_config",
        }
        return cls(
            type=service_type,
            service_crn=raw["service_crn"],
            api_token=raw["api_token"],
            backends=raw["backends"],
            poll_interval=raw["poll_interval"],
            endpoint_url=raw.get("endpoint_url"),
            iam_endpoint_url=raw.get("iam_endpoint_url", DEFAULT_IAM_ENDPOINT_URL),
            log_level=raw.get("log_level", DEFAULT_LOG_LEVEL),
            log_config=raw.get("log_config"),
            extra={k: v for k, v in raw.items() if k not in known_keys},
        )


class QuantumService(ABC):
    """Abstract base class for quantum service backends."""

    @abstractmethod
    def is_busy(self, backend_name: str) -> bool | None:
        """Check if a backend has an active dedicated session.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle, None on error.
        """


class IBMQuantumComputeService(QuantumService):
    """IBM Quantum Platform (IQP) implementation of QuantumService."""

    def __init__(self, instance: str, token: str):
        """Initialize the IQP service.

        Args:
            instance: IBM Quantum Platform CRN instance
            token: IBM Quantum Platform API token
        """
        self._service = QiskitRuntimeService(instance=instance, token=token)

    def _is_dedicated_active_session(self, session_id: str) -> bool:
        """Return True if the given session is an active dedicated session.

        Args:
            session_id: The Qiskit Runtime session id to check.

        Returns:
            True if the session is dedicated and active, otherwise False.
        """
        try:
            session = Session.from_id(session_id, self._service)
            details = session.details()
            return details["mode"] == "dedicated" and details["state"] == "active"
        except IBMInputValueError:
            # Session.from_id() throws IBMInputValueError for batch mode jobs
            return False

    def is_busy(self, backend_name: str) -> bool | None:
        """Check if a backend has an active dedicated session. Returns None on error.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle, None on error.
        """
        try:
            backend = self._service.backend(backend_name)
            if backend.status().operational is False:
                return True

            jobs = self._service.jobs(limit=1, backend_name=backend_name)
            if len(jobs) == 0:
                return False

            session_id = jobs[0].session_id
            if session_id is None:
                return False

            return self._is_dedicated_active_session(session_id)

        except Exception:  # pylint: disable=broad-except
            # Broad catch is intentional: this runs in a long-lived poll loop and
            # a single backend's transient failure must not crash the daemon.
            logger.exception("Failed to obtain active session: %s", backend_name)
            return None


class IBMQuantumSystemService(QuantumService):
    """IBM Quantum System implementation of QuantumService."""

    def __init__(
        self, instance: str, token: str, iam_endpoint_url: str, endpoint_url: str
    ):
        """Initialize the IQP service.

        Args:
            instance: IBM Quantum Platform CRN instance
            token: IBM Quantum Platform API token
            iam_endpoint_url: IAM authentication endpoint URL
            endpoint_url: Backend status API endpoint URL
        """
        self._instance = instance
        self._token = token
        self._iam_endpoint_url = iam_endpoint_url
        self._endpoint_url = endpoint_url
        self._authenticator = IAMAuthenticator(token, url=iam_endpoint_url)

    def is_busy(self, backend_name: str) -> bool | None:
        """Check if a backend has an active dedicated session. Returns None on error.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle, None on error.
        """
        try:
            access_token = self._authenticator.token_manager.get_token()
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Service-CRN": self._instance,
            }
            backend_details_url = f"{self._endpoint_url}/v1/backends/{backend_name}"
            resp = requests.get(backend_details_url, headers=headers, timeout=10)

            if resp.status_code != 200:
                logger.error(
                    "Backend status request failed for %s: HTTP %d %s",
                    backend_name,
                    resp.status_code,
                    resp.text,
                )
                return None

            resp_json = resp.json()
            logger.debug(
                "Backend status for %s: %s", backend_name, json.dumps(resp_json)
            )
            if resp_json["status"] != "online":
                return True
            if resp_json["locked"] is True:
                return True

        except Exception:  # pylint: disable=broad-except
            # Broad catch is intentional: this runs in a long-lived poll loop and
            # a single backend's transient failure must not crash the daemon.
            logger.exception("Failed to obtain active session: %s", backend_name)
            return None

        return False


def create_service(config: Config) -> QuantumService:
    """Instantiate the appropriate QuantumService for the given config.

    Args:
        config: Validated poller configuration.

    Returns:
        A QuantumService implementation matching config.type.
    """
    if config.type == "ibm-quantum-compute":
        return IBMQuantumComputeService(
            instance=config.service_crn,
            token=config.api_token,
        )
    if config.type == "ibm-quantum-system":
        return IBMQuantumSystemService(
            instance=config.service_crn,
            token=config.api_token,
            endpoint_url=config.endpoint_url,
            iam_endpoint_url=config.iam_endpoint_url,
        )
    # Config.from_file already validates config.type, so this should be unreachable.
    raise ConfigError(f"Unsupported service type: {config.type}.")


def update_slurm_license(backend_name: str, is_busy: bool) -> bool:
    """Update the Slurm dynamic license for a backend. Returns True on success.

    Args:
        backend_name: Quantum backend name (= Slurm license name)
        is_busy: True if busy, otherwise False

    Returns:
        True if succeeded, otherwise False
    """
    logger.info("Backend %s is %s", backend_name, "busy" if is_busy else "idle")
    last_consumed = 1 if is_busy else 0
    try:
        subprocess.run(
            [
                "sacctmgr",
                "-i",
                "update",
                "resource",
                backend_name,
                "set",
                f"lastconsumed={last_consumed}",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        logger.exception(
            "sacctmgr failed for %s: exit code %d", backend_name, e.returncode
        )
        return False

    return True


class ShutdownRequested(BaseException):
    """Raised on SIGTERM so it unwinds blocking calls the same way KeyboardInterrupt does.

    Inherits from BaseException (not Exception) so it is not accidentally
    swallowed by the services' broad `except Exception` handlers.
    """


def _raise_shutdown_requested(_signum, _frame) -> None:
    raise ShutdownRequested()


class Poller:
    """Polls backend status and syncs Slurm dynamic licenses."""

    def __init__(
        self, service: QuantumService, backends: list[str], poll_interval: float
    ):
        self._service = service
        self._backends = backends
        self._poll_interval = poll_interval
        self._prev_states: dict[str, bool | None] = dict.fromkeys(backends)

    def poll_once(self) -> None:
        """Check every configured backend once and update licenses on change."""
        for backend in self._backends:
            is_busy = self._service.is_busy(backend)
            if is_busy is None or is_busy == self._prev_states[backend]:
                continue
            if update_slurm_license(backend, is_busy):
                self._prev_states[backend] = is_busy

    def run(self) -> None:
        """Run the poll loop until interrupted by SIGINT/SIGTERM."""
        while True:
            self.poll_once()
            time.sleep(self._poll_interval)


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

    # Configure logging as early as possible using just the CLI, so config
    # load failures are still logged at a sensible level. Reconfigured below
    # once the config file's log_level/log_config (if any) is known.
    if args.log_config:
        configure_logging(log_config_path=args.log_config)
    else:
        configure_logging(level_name=args.log_level or DEFAULT_LOG_LEVEL)

    try:
        config = Config.from_file(args.config)
    except (ConfigError, OSError, json.JSONDecodeError) as e:
        logger.error("Failed to load config: %s", e)
        raise SystemExit(1) from e

    if args.log_config:
        pass  # CLI --log-config already applied and takes full precedence.
    elif config.log_config:
        configure_logging(log_config_path=config.log_config)
    elif args.log_level is None and config.log_level != DEFAULT_LOG_LEVEL:
        configure_logging(level_name=config.log_level)

    service = create_service(config)
    poller = Poller(service, config.backends, config.poll_interval)

    # Leave SIGINT on its default handler (raises KeyboardInterrupt) so Ctrl+C
    # interrupts blocking calls (e.g. requests.get) immediately, same as before.
    # Give SIGTERM the same behavior so systemd/slurm stop signals work too.
    signal.signal(signal.SIGTERM, _raise_shutdown_requested)

    try:
        poller.run()
    except (KeyboardInterrupt, ShutdownRequested):
        pass
    finally:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
