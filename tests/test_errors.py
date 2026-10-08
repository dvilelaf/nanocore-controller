from pathlib import Path

import pytest

from nanocore_controller import errors


def test_every_error_is_a_nanocore_error():
    for name in (
        "ValidationError",
        "ProtocolError",
        "DeviceStatusError",
        "DeviceTimeout",
        "DeviceDisconnected",
        "DeviceNotFound",
        "VerificationError",
        "SlotChangedError",
        "PartialApplyError",
        "BackupError",
    ):
        assert issubclass(getattr(errors, name), errors.NanocoreError)


def test_errors_stay_compatible_with_the_builtins_raised_before():
    assert issubclass(errors.ValidationError, ValueError)
    assert issubclass(errors.ProtocolError, ValueError)
    assert issubclass(errors.DeviceStatusError, RuntimeError)
    assert issubclass(errors.DeviceTimeout, TimeoutError)
    assert issubclass(errors.DeviceDisconnected, RuntimeError)
    assert issubclass(errors.VerificationError, RuntimeError)
    assert issubclass(errors.BackupError, RuntimeError)


def test_device_status_error_carries_command_status_and_payload():
    error = errors.DeviceStatusError(0x6D, 0x03, b"\x01")

    assert (error.command, error.status, error.payload) == (0x6D, 0x03, b"\x01")
    assert "0x6d" in str(error) and "0x03" in str(error)


def test_timeout_reports_whether_a_write_may_have_been_applied():
    write = errors.DeviceTimeout(0x46, maybe_applied=True)
    read = errors.DeviceTimeout(0x63, maybe_applied=False)

    assert write.maybe_applied and "may have been applied" in str(write)
    assert not read.maybe_applied and "may have been applied" not in str(read)


def test_slot_changed_error_uses_display_numbers():
    error = errors.SlotChangedError(5, 8)

    assert (error.expected_slot, error.actual_slot) == (5, 8)
    assert "slot 6" in str(error) and "slot 9" in str(error)
    assert isinstance(error, errors.VerificationError)


def test_partial_apply_error_points_to_the_backup():
    error = errors.PartialApplyError(7, 20, Path("/tmp/before.json"))

    assert (error.applied, error.total) == (7, 20)
    assert "7 of 20" in str(error) and "/tmp/before.json" in str(error)
    assert "previous state" not in str(errors.PartialApplyError(1, 2))


def test_a_status_error_is_not_confused_with_a_timeout():
    with pytest.raises(errors.DeviceStatusError):
        try:
            raise errors.DeviceStatusError(0x40, 0x03)
        except errors.DeviceTimeout:  # pragma: no cover - must not match
            pytest.fail("a status error must not be caught as a timeout")
