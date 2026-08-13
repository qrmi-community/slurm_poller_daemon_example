"""A poller daemon to update Slurm dynamic licenses based on the current status of IQP backends"""

# SPDX-License-Identifier: Apache-2.0

# pylint: disable=too-few-public-methods

import argparse
import json
import time
import logging
import subprocess
from abc import ABC, abstractmethod
from qiskit_ibm_runtime import QiskitRuntimeService, Session
from qiskit_ibm_runtime.exceptions import IBMInputValueError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

REQUIRED_KEYS = {"service_crn", "api_token", "backends", "poll_interval"}


class QuantumService(ABC):
    """Abstract base class for quantum service backends."""

    @abstractmethod
    def is_backend_busy(self, backend_name: str) -> bool | None:
        """Check if a backend has an active dedicated session.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle, None on error.
        """


class IQPService(QuantumService):
    """IBM Quantum Platform (IQP) implementation of QuantumService."""

    def __init__(self, instance: str, token: str):
        """Initialize the IQP service.

        Args:
            instance: IBM Quantum Platform CRN instance
            token: IBM Quantum Platform API token
        """
        self._service = QiskitRuntimeService(instance=instance, token=token)

    def is_backend_busy(self, backend_name: str) -> bool | None:
        """Check if a backend has an active dedicated session. Returns None on error.

        Args:
            backend_name: Quantum backend name

        Returns:
            True if busy, False if idle, None on error.
        """
        try:
            jobs = self._service.jobs(limit=1, backend_name=backend_name)
            if len(jobs) == 0:
                return False

            job = jobs[0]
            session_id = job.session_id
            if session_id is None:
                return False

            try:
                session = Session.from_id(session_id, self._service)
                details = session.details()
                if details["mode"] == "dedicated" and details["state"] == "active":
                    return True
            except IBMInputValueError:
                # Session.from_id() throws IBMInputValueError for batch mode jobs
                return False

        except Exception:  # pylint: disable=broad-except
            logger.exception("Failed to obtain active session: %s", backend_name)
            return None

        return False


def _update_slurm_license(backend_name: str, is_busy: bool) -> bool:
    """Update the Slurm dynamic license for a backend. Returns True on success.

    Args:
        backend_name: Quantum backend name(=Slurm license name)
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
        logger.exception("sacctmgr failed: %s", e.returncode)
        return False

    return True


def main():
    """Main thread"""
    parser = argparse.ArgumentParser(description="Slurm License Poller")
    parser.add_argument("--config", default="./config.json", description="config json file")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as config_file:
        config_json = json.load(config_file)

    missing = REQUIRED_KEYS - config_json.keys()
    if missing:
        raise ValueError(
            f"Missing required config keys: {', '.join(sorted(missing))}. "
            f"Please check your config file: {args.config}"
        )

    service: QuantumService = IQPService(
        instance=config_json["service_crn"],
        token=config_json["api_token"],
    )

    prev_states = {backend: None for backend in config_json["backends"]}
    try:
        while True:
            for backend in config_json["backends"]:
                is_busy = service.is_backend_busy(backend)
                if is_busy in (None, prev_states[backend]):
                    continue
                if _update_slurm_license(backend, is_busy):
                    prev_states[backend] = is_busy
            time.sleep(config_json["poll_interval"])
    except KeyboardInterrupt:
        logger.info("Shutting down.")


if __name__ == "__main__":
    main()
