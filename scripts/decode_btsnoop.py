"""Print the NANOCORE requests and responses found in an Android btsnoop_hci.log.

    uv run python scripts/decode_btsnoop.py capture.log
    uv run python scripts/decode_btsnoop.py capture.log --unknown     # leave out the commands already documented
"""

import argparse
import sys

from nanocore_controller.btsnoop import format_frames, read_frames_from_file

# Commands documented in docs/protocol.md; --unknown hides them to show what is new.
KNOWN_COMMANDS = {0x63, 0x40, 0x36, 0x56, 0x45, 0x6D, 0x46}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", help="btsnoop_hci.log from the Android device")
    parser.add_argument("--unknown", action="store_true", help="hide the commands that are already documented")
    parser.add_argument("--limit", type=int, default=48, help="payload bytes shown per line (default 48)")
    args = parser.parse_args()
    try:
        frames = read_frames_from_file(args.log)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.unknown:
        frames = [f for f in frames if f.command not in KNOWN_COMMANDS]
    print(format_frames(frames, limit=args.limit))
    print(f"\n{len(frames)} frames", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
