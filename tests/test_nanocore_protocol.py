import random
import struct
import unittest
import zlib

import nanocore_controller.nanocore_protocol as protocol
from nanocore_controller.errors import NanocoreError, ProtocolError, ValidationError
from nanocore_controller.nanocore_protocol import (
    SAVE_PRESET_COMMAND,
    SELECT_PRESET_COMMAND,
    amp_slot_payload,
    chain_order_payload,
    decode_response,
    encode_request,
    ir_slot_payload,
    live_enabled_payload,
    live_param_payload,
    live_variant_payload,
    pack_7bit,
    parse_asset_slot,
    parse_preset_capabilities,
    parse_preset_record,
    parse_preset_summaries,
    parse_runtime_snapshot,
    preset_volume_payload,
    save_preset_payload,
    select_preset_payload,
    unpack_7bit,
)


class NanocoreProtocolTests(unittest.TestCase):
    def test_encodes_save_preset_request_byte_exactly(self):
        self.assertEqual(SAVE_PRESET_COMMAND, 0x46)
        self.assertEqual(save_preset_payload(8), bytes.fromhex("08"))
        self.assertEqual(
            encode_request(SAVE_PRESET_COMMAND, save_preset_payload(8), sequence=1),
            bytes.fromhex("f0 7d 4e 43 70 00 02 01 00 46 00 01 00 00 08 f7"),
        )

    def test_rejects_invalid_save_preset_slots(self):
        for slot in (-1, 128, True):
            with self.subTest(slot=slot), self.assertRaises(ValueError):
                save_preset_payload(slot)

    def test_encodes_select_preset_payload_byte_exactly(self):
        self.assertEqual(select_preset_payload(8), bytes.fromhex("09 08"))

    def test_rejects_invalid_select_preset_slots(self):
        for slot in (-1, 128, True):
            with self.subTest(slot=slot):
                with self.assertRaises(ValueError):
                    select_preset_payload(slot)

    def test_encodes_select_preset_request_byte_exactly(self):
        self.assertEqual(SELECT_PRESET_COMMAND, 0x76)
        self.assertEqual(
            encode_request(SELECT_PRESET_COMMAND, select_preset_payload(8), sequence=1),
            bytes.fromhex(
                "f0 7d 4e 43 70 00 02 01 00 76 00 02 00 00 09 08 f7"
            ),
        )

    def test_encodes_live_parameter_payload_byte_exactly(self):
        self.assertEqual(
            live_param_payload(7, 2, 0.5),
            bytes.fromhex("01 07 02 00 00 00 3f"),
        )

    def test_encodes_live_variant_and_all_parameters_byte_exactly(self):
        self.assertEqual(
            live_variant_payload(3, 5, [0.25, 0.75]),
            bytes.fromhex("02 03 05 02") + struct.pack("<2f", 0.25, 0.75),
        )

    def test_encodes_other_live_fields_byte_exactly(self):
        self.assertEqual(live_enabled_payload(7, False), bytes.fromhex("03 07 00"))
        self.assertEqual(preset_volume_payload(84), bytes.fromhex("04 54"))
        self.assertEqual(
            chain_order_payload([0, 1, 2, 3, 4, 5, 6, 7]),
            bytes.fromhex("05 08 00 01 02 03 04 05 06 07"),
        )
        self.assertEqual(amp_slot_payload(4), bytes.fromhex("06 04"))
        self.assertEqual(ir_slot_payload(5), bytes.fromhex("07 05"))

    def test_rejects_out_of_range_live_values(self):
        with self.assertRaisesRegex(ValueError, "effect index"):
            live_enabled_payload(256, True)
        with self.assertRaisesRegex(ValueError, "0.0 to 1.0"):
            live_param_payload(7, 0, 1.01)
        with self.assertRaisesRegex(ValueError, "permutation"):
            chain_order_payload([0] * 8)

    def test_pack_7bit_round_trip_preserves_high_bits(self):
        raw = bytes.fromhex("02 34 12 63 00 80 ff 7f 55")
        self.assertEqual(unpack_7bit(pack_7bit(raw)), raw)

    def test_encodes_runtime_snapshot_request_byte_exactly(self):
        self.assertEqual(
            encode_request(0x63, sequence=1),
            bytes.fromhex("f0 7d 4e 43 70 00 02 01 00 63 00 00 00 f7"),
        )

    def test_encodes_preset_read_request_byte_exactly(self):
        self.assertEqual(
            encode_request(0x41, bytes([9]), sequence=1),
            bytes.fromhex("f0 7d 4e 43 70 00 02 01 00 41 00 01 00 00 09 f7"),
        )

    def test_decodes_response_header_and_payload(self):
        raw = bytes.fromhex("02 01 00 63 00 00 03 00 03 08 54")
        message = bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(raw) + bytes([0xF7])
        response = decode_response(message)
        self.assertEqual(response.sequence, 1)
        self.assertEqual(response.command, 0x63)
        self.assertEqual(response.status, 0)
        self.assertEqual(response.payload, bytes.fromhex("03 08 54"))

    def test_parses_runtime_snapshot_header_and_effects(self):
        payload = bytearray([3, 8, 84])
        for variant in range(8):
            payload.extend([1, variant, 1])
            payload.extend(bytes.fromhex("00 00 00 3f"))
        snapshot = parse_runtime_snapshot(bytes(payload))
        self.assertEqual(snapshot.version, 3)
        self.assertEqual(snapshot.active_preset, 8)
        self.assertEqual(snapshot.preset_volume, 84)
        self.assertEqual(len(snapshot.effects), 8)
        self.assertAlmostEqual(snapshot.effects[0].params[0], 0.5)

    def test_parses_runtime_snapshot_chain_order(self):
        payload = bytearray([3, 8, 84])
        for _ in range(8):
            payload.extend([0, 0, 0])
        payload.extend([8, 0, 1, 2, 3, 4, 5, 6, 7])
        snapshot = parse_runtime_snapshot(bytes(payload))
        self.assertEqual(snapshot.chain_order, tuple(range(8)))
        self.assertEqual(snapshot.trailing_bytes, b"")

    def test_parses_preset_catalog_page(self):
        payload = bytes([6, 2, 6, 0]) + b"TightRhy" + bytes([7, 1]) + b"HeavyMut"
        page = parse_preset_summaries(payload)
        self.assertEqual(page.start, 6)
        self.assertEqual(page.entries[0].slot, 6)
        self.assertEqual(page.entries[0].name, "TightRhy")
        self.assertEqual(page.entries[1].flags, 1)

    def test_parses_active_asset_slot_metadata(self):
        payload = bytes.fromhex(
            "04 01 01 d2 2f 00 00 17 f8 de 31 1a 1a 00 "
            "46 64 35 39 42 6d 00 00 00 00 00 00 00 00 00 00"
        )
        asset = parse_asset_slot(payload)
        self.assertEqual(asset.slot, 4)
        self.assertTrue(asset.present)
        self.assertTrue(asset.active)
        self.assertEqual(asset.size, 12242)
        self.assertEqual(asset.name, "Fd59Bm")

    def test_parses_rsp1_capabilities(self):
        capabilities = parse_preset_capabilities(bytes.fromhex("52 53 50 31 02 00 80 00 01 32"))
        self.assertEqual(capabilities.version, 2)
        self.assertEqual(capabilities.record_size, 128)
        self.assertEqual(capabilities.preset_count, 50)

    def test_validates_rsp1_record_crc(self):
        record = bytearray(128)
        record[:8] = bytes.fromhex("52 53 50 31 02 00 80 00")
        record[12:16] = (1).to_bytes(4, "little")
        record[32:37] = bytes(range(5))
        record[8:12] = (zlib.crc32(record) & 0xFFFFFFFF).to_bytes(4, "little")
        parsed = parse_preset_record(bytes(record))
        self.assertTrue(parsed.present)
        self.assertEqual(parsed.order, (0, 1, 2, 3, 4))

    def test_reassembles_streamed_response_chunks(self):
        assembler = protocol.ResponseAssembler()
        first = protocol.NanocoreResponse(1, 0x63, 0x10, bytes.fromhex("06 00 00 00 03 08 54"))
        final = protocol.NanocoreResponse(1, 0x63, 0x11, bytes.fromhex("06 00 03 00 01 02 03"))
        self.assertIsNone(assembler.feed(first))
        response = assembler.feed(final)
        self.assertEqual(response.status, 0)
        self.assertEqual(response.payload, bytes.fromhex("03 08 54 01 02 03"))


class StrictFramingTests(unittest.TestCase):
    def test_unpack_rejects_high_bit_bytes(self):
        for data in (bytes.fromhex("ff"), bytes.fromhex("00 80 01"), bytes.fromhex("00 01 f7")):
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                unpack_7bit(data)

    def test_unpack_rejects_truncated_group(self):
        for data in (bytes.fromhex("00"), bytes.fromhex("7f 01 02"), bytes.fromhex("00 01 02 03 04 05 06 07 01")):
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                unpack_7bit(data)

    def test_unpack_keeps_short_final_group(self):
        raw = bytes.fromhex("02 34 80 ff 7f 55 01 02 99")
        self.assertEqual(unpack_7bit(pack_7bit(raw)), raw)

    def test_decode_response_raises_only_protocol_error(self):
        prefix = bytes.fromhex("f0 7d 4e 43 71")
        for message in (
            b"",
            prefix + bytes.fromhex("00 01 f7"),
            prefix + bytes.fromhex("ff 01 f7"),
            prefix + pack_7bit(bytes.fromhex("02 01 00 63 00 00 09 00")) + b"\xf7",
            bytes.fromhex("f0 7d 4e 43 70 00 f7"),
        ):
            with self.subTest(message=message), self.assertRaises(ProtocolError):
                decode_response(message)

    def test_assembler_rejects_malformed_streams(self):
        def chunk(status, total, offset, data=b""):
            payload = total.to_bytes(2, "little") + offset.to_bytes(2, "little") + data
            return protocol.NanocoreResponse(1, 0x63, status, payload)

        cases = {
            "truncated": [protocol.NanocoreResponse(1, 0x63, 0x10, b"\x01")],
            "zero total": [chunk(0x10, 0, 0)],
            "zero total final": [chunk(0x11, 0, 0)],
            "overflow": [chunk(0x10, 2, 1, b"\x01\x02")],
            "length changed": [chunk(0x10, 4, 0, b"\x01"), chunk(0x10, 5, 1, b"\x02")],
            "overlap": [chunk(0x10, 4, 0, b"\x01\x02"), chunk(0x10, 4, 1, b"\x09\x03")],
            "gap at final": [chunk(0x10, 4, 0, b"\x01"), chunk(0x11, 4, 2, b"\x02\x03")],
        }
        for name, chunks in cases.items():
            with self.subTest(name), self.assertRaises(ProtocolError):
                assembler = protocol.ResponseAssembler()
                for item in chunks:
                    assembler.feed(item)

    def test_assembler_accepts_out_of_order_chunks(self):
        assembler = protocol.ResponseAssembler()
        late = protocol.NanocoreResponse(1, 0x63, 0x10, bytes.fromhex("04 00 02 00 03 04"))
        final = protocol.NanocoreResponse(1, 0x63, 0x11, bytes.fromhex("04 00 00 00 01 02"))
        self.assertIsNone(assembler.feed(late))
        self.assertEqual(assembler.feed(final).payload, bytes.fromhex("01 02 03 04"))

    def test_assembler_discards_a_stream_after_an_error(self):
        assembler = protocol.ResponseAssembler()
        assembler.feed(protocol.NanocoreResponse(1, 0x63, 0x10, bytes.fromhex("04 00 00 00 01")))
        with self.assertRaises(ProtocolError):
            assembler.feed(protocol.NanocoreResponse(1, 0x63, 0x11, bytes.fromhex("04 00 01 00 02")))
        fresh = protocol.NanocoreResponse(1, 0x63, 0x11, bytes.fromhex("02 00 00 00 05 06"))
        self.assertEqual(assembler.feed(fresh).payload, bytes.fromhex("05 06"))


def snapshot_payload(*, preset=8, volume=84, params=(0.5,), chain=(8, 0, 1, 2, 3, 4, 5, 6, 7), effects=8):
    payload = bytearray([3, preset, volume])
    for _ in range(effects):
        payload.extend([1, 0, len(params)])
        payload.extend(struct.pack(f"<{len(params)}f", *params))
    payload.extend(chain)
    return bytes(payload)


class StrictSnapshotTests(unittest.TestCase):
    def test_accepts_a_well_formed_snapshot(self):
        snapshot = parse_runtime_snapshot(snapshot_payload())
        self.assertEqual(snapshot.chain_order, tuple(range(8)))
        self.assertEqual(snapshot.trailing_bytes, b"")

    def test_accepts_boundary_values(self):
        parse_runtime_snapshot(snapshot_payload(preset=127, volume=100, params=(0.0, 1.0)))
        parse_runtime_snapshot(snapshot_payload(preset=0, volume=0, params=()))

    def test_accepts_a_snapshot_without_chain_section(self):
        snapshot = parse_runtime_snapshot(snapshot_payload(chain=()))
        self.assertEqual(snapshot.chain_order, ())

    def test_rejects_bad_parameters(self):
        for value in (float("nan"), float("inf"), float("-inf"), -0.01, 1.01, 1e30):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                parse_runtime_snapshot(snapshot_payload(params=(0.25, value)))

    def test_rejects_bad_chain_order(self):
        chains = {
            "count 7": (7, 0, 1, 2, 3, 4, 5),
            "count 9": (9, 0, 1, 2, 3, 4, 5, 6, 7, 7),
            "count 0": (0,),
            "duplicate": (8, 0, 0, 2, 3, 4, 5, 6, 7),
            "out of range": (8, 0, 1, 2, 3, 4, 5, 6, 8),
            "truncated": (8, 0, 1, 2),
        }
        for name, chain in chains.items():
            with self.subTest(name), self.assertRaises(ProtocolError):
                parse_runtime_snapshot(snapshot_payload(chain=chain))

    def test_rejects_out_of_range_header_values(self):
        for kwargs in ({"volume": 101}, {"volume": 255}, {"preset": 128}, {"preset": 255}):
            with self.subTest(**kwargs), self.assertRaises(ProtocolError):
                parse_runtime_snapshot(snapshot_payload(**kwargs))

    def test_rejects_wrong_number_of_effects(self):
        for effects in (0, 7, 9):
            with self.subTest(effects=effects), self.assertRaises(ProtocolError):
                parse_runtime_snapshot(snapshot_payload(effects=effects))


class ParserErrorTypeTests(unittest.TestCase):
    def test_malformed_device_payloads_raise_protocol_error(self):
        cases = (
            (parse_preset_summaries, b""),
            (parse_preset_summaries, bytes([0, 1, 0, 0])),
            (parse_asset_slot, b"\0" * 29),
            (parse_asset_slot, bytes([0, 2, 0]) + b"\0" * 27),
            (parse_preset_capabilities, b"RSP1"),
            (parse_preset_capabilities, b"RSP1" + bytes.fromhex("09 00 80 00 01 32")),
            (parse_preset_record, b"\0" * 128),
        )
        for parser, payload in cases:
            with self.subTest(parser=parser.__name__, payload=payload), self.assertRaises(ProtocolError):
                parser(payload)


NAN = float("nan")
INF = float("inf")


class StrictPayloadBuilderTests(unittest.TestCase):
    def assertAllRejected(self, builder, calls):
        for args in calls:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                builder(*args)

    def test_exports_range_constants(self):
        self.assertEqual(protocol.MAX_EFFECT_INDEX, 7)
        self.assertEqual(protocol.MAX_PARAM_INDEX, 23)
        self.assertEqual(protocol.MAX_VARIANT, 255)
        self.assertEqual(protocol.MAX_ASSET_SLOT, 29)
        self.assertEqual(protocol.MAX_PRESET_SLOT, 127)
        self.assertEqual(protocol.MAX_PRESET_VOLUME, 100)

    def test_boundaries_are_accepted(self):
        live_param_payload(7, 23, 1.0)
        live_param_payload(0, 0, 0)
        live_variant_payload(7, 255, [0.0] * 24)
        live_enabled_payload(0, True)
        preset_volume_payload(0)
        preset_volume_payload(100)
        amp_slot_payload(29)
        ir_slot_payload(0)
        select_preset_payload(127)
        save_preset_payload(0)

    def test_live_param_payload_rejects_bad_values(self):
        self.assertAllRejected(
            live_param_payload,
            [
                (255, 255, "0.5"),
                (8, 0, 0.5),
                (0, 24, 0.5),
                (True, 0, 0.5),
                (0, False, 0.5),
                (0.0, 0, 0.5),
                (0, 1.0, 0.5),
                ("1", 0, 0.5),
                (-1, 0, 0.5),
                (0, 0, NAN),
                (0, 0, INF),
                (0, 0, -0.1),
                (0, 0, True),
                (0, 0, None),
            ],
        )

    def test_live_variant_payload_rejects_bad_values(self):
        self.assertAllRejected(
            live_variant_payload,
            [
                (8, 0, [0.5]),
                (0, 256, [0.5]),
                (0, True, [0.5]),
                (0, 1.0, [0.5]),
                (0, 0, [NAN]),
                (0, 0, [INF]),
                (0, 0, ["0.5"]),
                (0, 0, [True]),
                (0, 0, [0.5] * 25),
                (0, 0, 0.5),
                (0, 0, "ab"),
            ],
        )

    def test_enabled_volume_and_slot_builders_reject_bad_values(self):
        self.assertAllRejected(live_enabled_payload, [(0, 1), (0, "yes"), (8, True), (True, True), (0.0, True)])
        self.assertAllRejected(
            preset_volume_payload, [(101,), (-1,), (True,), (50.0,), ("50",), (NAN,), (INF,)]
        )
        for builder in (amp_slot_payload, ir_slot_payload):
            self.assertAllRejected(builder, [(30,), (-1,), (True,), (1.0,), ("1",), (NAN,), (INF,)])
        for builder in (select_preset_payload, save_preset_payload):
            self.assertAllRejected(builder, [(128,), (-1,), (True,), (1.0,), ("1",), (NAN,), (INF,)])

    def test_chain_order_payload_rejects_bad_entries_without_type_error(self):
        orders = [
            [0, 1, 2, 3, 4, 5, 6, "x"],
            [0, 1, 2, 3, 4, 5, 6, None],
            [0.0, 1, 2, 3, 4, 5, 6, 7],
            [False, True, 2, 3, 4, 5, 6, 7],
            [0, 1, 2, 3, 4, 5, 6],
            [0, 1, 2, 3, 4, 5, 6, 7, 8],
            [0] * 8,
            [8, 1, 2, 3, 4, 5, 6, 7],
            "01234567",
            None,
            5,
            [[0], 1, 2, 3, 4, 5, 6, 7],
        ]
        for order in orders:
            with self.subTest(order=order), self.assertRaises(ValidationError):
                chain_order_payload(order)

    def test_huge_integer_parameter_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            live_param_payload(0, 0, 10**400)
        with self.assertRaises(ValidationError):
            live_variant_payload(0, 0, [10**400])

    def test_chain_order_payload_accepts_any_permutation(self):
        self.assertEqual(chain_order_payload((7, 6, 5, 4, 3, 2, 1, 0)), bytes.fromhex("05 08 07 06 05 04 03 02 01 00"))

    def test_encode_request_rejects_bad_arguments(self):
        for kwargs in ({"command": -1}, {"command": 0x10000}, {"command": True}, {"command": 1.0}):
            with self.subTest(**kwargs), self.assertRaises(ValidationError):
                encode_request(sequence=1, **kwargs)
        for sequence in (-1, 0x10000, True, 1.0, "1"):
            with self.subTest(sequence=sequence), self.assertRaises(ValidationError):
                encode_request(0x63, sequence=sequence)
        with self.assertRaises(ValidationError):
            encode_request(0x63, "abc", sequence=1)


class CommandConstantTests(unittest.TestCase):
    def test_command_ids(self):
        self.assertEqual(protocol.RUNTIME_SNAPSHOT_COMMAND, 0x63)
        self.assertEqual(protocol.PRESET_CATALOG_COMMAND, 0x40)
        self.assertEqual(protocol.AMP_SLOT_COMMAND, 0x36)
        self.assertEqual(protocol.IR_SLOT_COMMAND, 0x56)
        self.assertEqual(protocol.PRESET_CAPABILITIES_COMMAND, 0x45)

    def test_validate_display_number(self):
        self.assertEqual(protocol.validate_display_number(1), 1)
        self.assertEqual(protocol.validate_display_number(128), 128)
        for value in (0, 129, -1, True, 1.0, "1", None, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValidationError, "preset display number must be an integer from 1 to 128"
            ):
                protocol.validate_display_number(value)


def mutate_bytes(rng, data):
    data = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        choice = rng.randrange(4)
        if choice == 0 and data:
            data[rng.randrange(len(data))] = rng.randrange(256)
        elif choice == 1 and data:
            del data[rng.randrange(len(data)) :]
        elif choice == 2:
            data.insert(rng.randint(0, len(data)), rng.randrange(256))
        elif data:
            del data[rng.randrange(len(data))]
    return bytes(data)


def random_bytes(rng, high_bit=True):
    limit = 256 if high_bit else 128
    return bytes(rng.randrange(limit) for _ in range(rng.randint(0, 60)))


class FuzzTests(unittest.TestCase):
    ITERATIONS = 1500

    def assertOnlyNanocoreErrors(self, function, samples):
        for sample in samples:
            try:
                function(sample)
            except NanocoreError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{function.__name__}({sample!r}) raised {type(exc).__name__}: {exc}")

    def samples(self, seed, valid):
        rng = random.Random(seed)
        for index in range(self.ITERATIONS):
            if index % 3 == 0:
                yield random_bytes(rng)
            elif index % 3 == 1:
                yield random_bytes(rng, high_bit=False)
            else:
                yield mutate_bytes(rng, rng.choice(valid))

    def test_unpack_7bit(self):
        valid = [pack_7bit(bytes(range(40))), pack_7bit(b"\xff" * 9)]
        self.assertOnlyNanocoreErrors(unpack_7bit, self.samples(1, valid))

    def test_unpack_7bit_round_trips_random_data(self):
        rng = random.Random(2)
        for _ in range(300):
            raw = random_bytes(rng)
            self.assertEqual(unpack_7bit(pack_7bit(raw)), raw)

    def test_decode_response(self):
        frame = bytes.fromhex("f0 7d 4e 43 71") + pack_7bit(bytes.fromhex("02 01 00 63 00 00 03 00 03 08 54")) + b"\xf7"
        self.assertOnlyNanocoreErrors(decode_response, self.samples(3, [frame]))

    def test_response_assembler(self):
        rng = random.Random(4)
        assembler = protocol.ResponseAssembler()

        def feed(response):
            return assembler.feed(response)

        responses = [
            protocol.NanocoreResponse(
                rng.randrange(3), rng.choice((0x63, 0x40)), rng.choice((0, 0x10, 0x11, 0x10, 0x11)), random_bytes(rng)
            )
            for _ in range(self.ITERATIONS)
        ]
        structured = [
            protocol.NanocoreResponse(
                rng.randrange(2),
                0x63,
                rng.choice((0x10, 0x11)),
                rng.randrange(0, 9).to_bytes(2, "little") + rng.randrange(0, 9).to_bytes(2, "little") + random_bytes(rng)[:6],
            )
            for _ in range(self.ITERATIONS)
        ]
        self.assertOnlyNanocoreErrors(feed, responses + structured)

    def test_device_payload_parsers(self):
        valid = {
            parse_runtime_snapshot: [snapshot_payload(), snapshot_payload(chain=(), params=())],
            parse_preset_summaries: [bytes([6, 2, 6, 0]) + b"TightRhy" + bytes([7, 1]) + b"HeavyMut"],
            parse_asset_slot: [bytes.fromhex("04 01 01 d2 2f 00 00 17 f8 de 31 1a 1a 00") + b"Fd59Bm".ljust(16, b"\0")],
            parse_preset_capabilities: [bytes.fromhex("52 53 50 31 02 00 80 00 01 32")],
            parse_preset_record: [bytes(128)],
        }
        for seed, (parser, samples) in enumerate(valid.items()):
            self.assertOnlyNanocoreErrors(parser, self.samples(10 + seed, samples))


if __name__ == "__main__":
    unittest.main()
