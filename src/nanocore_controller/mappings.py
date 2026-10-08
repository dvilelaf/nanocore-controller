"""Documented LIVTRA NANOCORE MIDI mappings."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    name: str
    on_cc: int
    type_cc: int


@dataclass(frozen=True)
class Parameter:
    name: str
    cc: int
    blocks: tuple[str, ...]
    types: tuple[str, ...] = ()


BLOCKS = {
    "fx1": Block("fx1", 20, 40),
    "fx2": Block("fx2", 21, 41),
    "amp": Block("amp", 23, 43),
    "cab": Block("cab", 24, 44),
    "mod": Block("mod", 25, 45),
    "delay": Block("delay", 26, 46),
    "reverb": Block("reverb", 27, 47),
    "eq": Block("eq", 28, 48),
}

BLOCK_ALIASES = {
    "fx-1": "fx1",
    "fx-2": "fx2",
    "effect1": "fx1",
    "effect2": "fx2",
    "modulation": "mod",
}

TYPE_IDS = {
    "fx1": {
        "gate": 0,
        "auto_gate": 1,
        "gate_compressor": 2,
        "compressor": 3,
    },
    "fx2": {
        **{f"drive_{index}": index - 1 for index in range(1, 6)},
        **{f"boost_{index}": index + 4 for index in range(1, 4)},
        "pitch": 8,
        "envelope_wah": 9,
        "wah": 10,
    },
    "mod": {"chorus": 0, "phaser": 1, "flanger": 2, "tremolo": 3, "vibrato": 4},
    "delay": {
        "bbd": 0,
        "digital": 1,
        "duck": 2,
        "ice": 3,
        "reverse": 4,
        "lofi": 5,
        "smear": 6,
        "mod_delay": 7,
    },
    "reverb": {
        "room": 0,
        "plate": 1,
        "hall": 2,
        "concert": 3,
        "shimmer": 4,
        "cloud": 5,
        "spring": 6,
    },
    "eq": {"eq3": 0, "eq6": 1, "eq8": 2},
}

TYPE_ALIASES = {
    "fx1": {"gate+compressor": "gate_compressor", "gate+comp": "gate_compressor", "auto gate": "auto_gate"},
    "fx2": {"drive1": "drive_1", "drive2": "drive_2", "drive3": "drive_3", "drive4": "drive_4", "drive5": "drive_5"},
    "delay": {"lo-fi": "lofi", "mod delay": "mod_delay"},
    "eq": {"3-band": "eq3", "6-band": "eq6", "8-band": "eq8"},
}

PARAMETERS = {
    "gate_threshold": Parameter("gate_threshold", 50, ("fx1",), ("gate", "gate_compressor")),
    "compressor_threshold": Parameter("compressor_threshold", 51, ("fx1",), ("compressor", "gate_compressor")),
    "ratio": Parameter("ratio", 52, ("fx1",), ("compressor", "gate_compressor")),
    "knee": Parameter("knee", 53, ("fx1",), ("compressor", "gate_compressor")),
    "attack": Parameter("attack", 54, ("fx1",), ("compressor", "gate_compressor")),
    "release": Parameter("release", 55, ("fx1",), ("compressor", "gate_compressor")),
    "makeup": Parameter("makeup", 56, ("fx1",), ("compressor", "gate_compressor")),
    "drive_gain": Parameter("drive_gain", 57, ("fx2",), tuple(f"drive_{i}" for i in range(1, 6)) + tuple(f"boost_{i}" for i in range(1, 4))),
    "tone": Parameter("tone", 58, ("fx2",), tuple(f"drive_{i}" for i in range(1, 6))),
    "fx2_level": Parameter("fx2_level", 59, ("fx2",), tuple(f"drive_{i}" for i in range(1, 6))),
    "amp_gain": Parameter("amp_gain", 60, ("amp",)),
    "bass": Parameter("bass", 61, ("amp",)),
    "mid": Parameter("mid", 62, ("amp",)),
    "treble": Parameter("treble", 63, ("amp",)),
    "amp_level": Parameter("amp_level", 64, ("amp",)),
    "cab_low_cut": Parameter("cab_low_cut", 65, ("cab",)),
    "cab_high_cut": Parameter("cab_high_cut", 66, ("cab",)),
    "cab_level": Parameter("cab_level", 67, ("cab",)),
    "mod_rate": Parameter("mod_rate", 68, ("mod",)),
    "mod_depth": Parameter("mod_depth", 69, ("mod",)),
    "mod_mix": Parameter("mod_mix", 70, ("mod",)),
    "mod_level": Parameter("mod_level", 73, ("mod",)),
    "delay_time": Parameter("delay_time", 74, ("delay",)),
    "delay_feedback": Parameter("delay_feedback", 75, ("delay",)),
    "delay_mix": Parameter("delay_mix", 77, ("delay",)),
    "delay_level": Parameter("delay_level", 78, ("delay",)),
    "reverb_decay": Parameter("reverb_decay", 85, ("reverb",)),
    "reverb_pre_delay": Parameter("reverb_pre_delay", 86, ("reverb",)),
    "reverb_mix": Parameter("reverb_mix", 89, ("reverb",)),
    "reverb_level": Parameter("reverb_level", 90, ("reverb",)),
    "eq_low": Parameter("eq_low", 91, ("eq",)),
    "eq_mid": Parameter("eq_mid", 92, ("eq",)),
    "eq_high": Parameter("eq_high", 93, ("eq",)),
}

PARAMETER_ALIASES = {
    "gain": "amp_gain",
    "volume": "amp_level",
    "cab_lowcut": "cab_low_cut",
    "cab_highcut": "cab_high_cut",
    "reverb_predelay": "reverb_pre_delay",
}


def normalize_block(block: str) -> str:
    key = block.strip().lower()
    key = BLOCK_ALIASES.get(key, key)
    if key not in BLOCKS:
        raise ValueError(f"unknown block: {block}")
    return key


def normalize_parameter(name: str) -> str:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    key = PARAMETER_ALIASES.get(key, key)
    if key not in PARAMETERS:
        raise ValueError(f"unknown parameter: {name}")
    return key


def parameter_cc(name: str) -> int:
    return PARAMETERS[normalize_parameter(name)].cc


def resolve_type(block: str, type_name: str | int) -> int:
    block_key = normalize_block(block)
    if isinstance(type_name, int):
        if not 0 <= type_name <= 127:
            raise ValueError("type ID must be from 0 to 127")
        return type_name
    raw_token = str(type_name).strip()
    try:
        numeric_type = int(raw_token, 10)
    except ValueError:
        pass
    else:
        if not 0 <= numeric_type <= 127:
            raise ValueError("type ID must be from 0 to 127")
        return numeric_type
    token = raw_token.lower().replace(" ", "_").replace("-", "_")
    token = TYPE_ALIASES.get(block_key, {}).get(token, token)
    try:
        return TYPE_IDS[block_key][token]
    except KeyError as exc:
        raise ValueError(f"unknown {block_key} type: {type_name}") from exc


def validate_parameter(name: str, block: str, effect_type: str | None = None) -> bool:
    try:
        parameter = PARAMETERS[normalize_parameter(name)]
        block_key = normalize_block(block)
    except ValueError:
        return False
    if block_key not in parameter.blocks:
        return False
    if effect_type is None or not parameter.types:
        return True
    try:
        type_key = str(effect_type).strip().lower().replace(" ", "_").replace("-", "_")
        type_key = TYPE_ALIASES.get(block_key, {}).get(type_key, type_key)
    except AttributeError:
        return False
    return type_key in parameter.types


def percent_to_midi(percent: float) -> int:
    if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        raise ValueError("percent must be a number from 0 to 100")
    return int(round(percent * 127 / 100))
