"""Pure MIDI message encoding helpers."""


def _validate_7_bit(value: int, field: str) -> None:
    if not isinstance(value, int) or not 0 <= value <= 127:
        raise ValueError(f"{field} must be an integer from 0 to 127")


def _status_channel(channel: int, base: int) -> int:
    if not isinstance(channel, int) or not 1 <= channel <= 16:
        raise ValueError("channel must be an integer from 1 to 16")
    return base | (channel - 1)


def control_change(channel: int, controller: int, value: int) -> bytes:
    """Encode one MIDI Control Change message."""

    _validate_7_bit(controller, "controller")
    _validate_7_bit(value, "value")
    return bytes((_status_channel(channel, 0xB0), controller, value))


def program_change(channel: int, program: int) -> bytes:
    """Encode one MIDI Program Change message."""

    _validate_7_bit(program, "program")
    return bytes((_status_channel(channel, 0xC0), program))
