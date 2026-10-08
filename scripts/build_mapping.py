"""Turn raw calibration measurements into the mapping table the web editor uses.

Input : docs/calibration/measurements.json (from scripts/calibrate_mapping.py)
        docs/calibration/editor-catalog.json (the editor's blocks and types)
Output: docs/calibration/mapping.json and web/src/data/measuredMapping.json

For every block and type it records the pedal's snapshot entry, the `variant`
the pedal reports for the type, and for each parameter of the snapshot the
controller number that changes it (or null when no documented controller does).

    uv run python scripts/build_mapping.py
"""

import argparse
import json
from pathlib import Path
from typing import Any

ALL_TYPES = "*"
FIXED_TYPE_BLOCKS = frozenset({"amp", "cab"})


class MappingError(ValueError):
    pass


def parameter_controllers(record: dict[str, Any], runtime_index: int) -> dict[int, int]:
    """Snapshot parameter index -> the one controller number that changed it."""

    found: dict[int, set[int]] = {}
    for cc, probes in record["cc_map"].items():
        for probe in probes:
            for change in probe["changes"]:
                if change["field"] == "param" and change["effect"] == runtime_index:
                    found.setdefault(change["index"], set()).add(int(cc))
    clashes = {index: sorted(ccs) for index, ccs in found.items() if len(ccs) > 1}
    if clashes:
        raise MappingError(f"several controllers change the same parameter: {clashes}")
    return {index: next(iter(ccs)) for index, ccs in found.items()}


def build(measurements: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    runtime_order: list[str | None] = [None] * 8
    blocks: dict[str, Any] = {}
    names = {b["id"]: {t["id"]: t["name"] for t in b["types"]} for b in catalog["blocks"]}
    for block_id, block in measurements["blocks"].items():
        index = block.get("runtime_index")
        if index is None:
            raise MappingError(f"{block_id}: its on/off controller changed no snapshot entry")
        if runtime_order[index] is not None:
            raise MappingError(f"{block_id} and {runtime_order[index]} share snapshot entry {index}")
        runtime_order[index] = block_id
        types: dict[str, Any] = {}
        for type_id, record in block["types"].items():
            count = record["param_count"]
            by_index = parameter_controllers(record, index)
            if by_index and max(by_index) >= count:
                raise MappingError(f"{block_id} type {type_id}: parameter index beyond {count}")
            key = ALL_TYPES if block_id in FIXED_TYPE_BLOCKS else type_id
            types[key] = {
                "name": names[block_id].get(int(type_id), record["name"]),
                "variant": record["variant"],
                "param_count": count,
                "params": [{"index": j, "cc": by_index.get(j)} for j in range(count)],
            }
        blocks[block_id] = {"runtime_index": index, "types": types}
    if None in runtime_order:
        raise MappingError(f"snapshot entries without a block: {runtime_order}")
    order = chain_order_semantics(measurements)
    return {
        "format": "nanocore-runtime-mapping",
        "version": 1,
        "source": "docs/calibration/measurements.json",
        "captured_at": measurements.get("captured_at"),
        "runtime_blocks": runtime_order,
        "chain_order_values": order,
        "blocks": blocks,
    }


def chain_order_semantics(measurements: dict[str, Any]) -> dict[str, Any]:
    check = measurements["chain_order"]
    if check["read_back"] != check["sent"] or check["restored"] != check["original"]:
        raise MappingError(f"the chain order was not read back as written: {check}")
    return {"verified_round_trip": True, "values": "as sent; their relation to blocks is unverified"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--measurements", default="docs/calibration/measurements.json")
    parser.add_argument("--catalog", default="docs/calibration/editor-catalog.json")
    parser.add_argument("--output", default="docs/calibration/mapping.json")
    parser.add_argument("--web-output", default="web/src/data/measuredMapping.json")
    args = parser.parse_args(argv)
    result = build(
        json.loads(Path(args.measurements).read_text(encoding="utf-8")),
        json.loads(Path(args.catalog).read_text(encoding="utf-8")),
    )
    text = json.dumps(result, indent=1) + "\n"
    for target in (args.output, args.web_output):
        Path(target).write_text(text, encoding="utf-8")
    print(f"wrote {args.output} and {args.web_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
