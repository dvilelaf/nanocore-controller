"""Typed errors for NANOCORE operations.

Every class also inherits the built-in exception the code raised before this
module existed (``ValueError``, ``RuntimeError``, ``TimeoutError``), so callers
and tests that catch the old types keep working while new callers, such as a
server, can tell the failure modes apart.
"""

from pathlib import Path


class NanocoreError(Exception):
    """Base class for every error raised by this package on purpose."""


class ValidationError(NanocoreError, ValueError):
    """Bad input from a user, a file or a caller."""


class ProtocolError(NanocoreError, ValueError):
    """A device response that is malformed or inconsistent with its request."""


class DeviceStatusError(NanocoreError, RuntimeError):
    """The device answered a command with a non-zero status."""

    def __init__(self, command: int, status: int, payload: bytes = b"") -> None:
        self.command = command
        self.status = status
        self.payload = payload
        super().__init__(f"NANOCORE command 0x{command:02x} failed with status 0x{status:02x}")


class DeviceTimeout(NanocoreError, TimeoutError):
    """The device did not answer in time.

    ``maybe_applied`` is true for any command that changes state: after a
    timeout the outcome is unknown and the caller must read the state back.
    """

    def __init__(self, command: int, *, maybe_applied: bool, timeout: float | None = None) -> None:
        self.command = command
        self.maybe_applied = maybe_applied
        self.timeout = timeout
        suffix = "; it may have been applied" if maybe_applied else ""
        super().__init__(f"NANOCORE command 0x{command:02x} timed out{suffix}")


class DeviceDisconnected(NanocoreError, RuntimeError):
    """The link to the device is down or dropped during an operation."""


class DeviceNotFound(NanocoreError, RuntimeError):
    """No device answered at the configured address or port."""


class VerificationError(NanocoreError, RuntimeError):
    """A read-back after a write did not match what was written."""

    # Set by operations that tried to put the previous state back after failing:
    # true when that worked, false when it did not, None when nothing was attempted.
    rolled_back: bool | None = None


class SlotChangedError(VerificationError):
    """The active preset changed while an operation was in progress."""

    def __init__(self, expected_slot: int, actual_slot: int) -> None:
        self.expected_slot = expected_slot
        self.actual_slot = actual_slot
        super().__init__(
            f"active preset changed from slot {expected_slot + 1} to slot {actual_slot + 1}"
        )


class PartialApplyError(VerificationError):
    """A multi-step write failed after some steps had already been applied."""

    def __init__(
        self,
        applied: int,
        total: int,
        backup_path: Path | None = None,
        detail: str | None = None,
    ) -> None:
        self.applied = applied
        self.total = total
        self.backup_path = backup_path
        self.detail = detail
        where = f"; the previous state is in {backup_path}" if backup_path is not None else ""
        why = f"; {detail}" if detail else ""
        super().__init__(f"only {applied} of {total} operations were applied{why}{where}")


class BackupError(NanocoreError, RuntimeError):
    """A backup file could not be written, read or validated."""
