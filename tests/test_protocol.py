import numpy as np

from c12666ma.protocol import (PKT_FRAME, PKT_TEXT, PacketParser, build_frame_payload,
                               build_packet, parse_frame)


def frame_packet(seq=0, value=300, **kw):
    return build_packet(PKT_FRAME, build_frame_payload(seq, np.full(256, value), **kw))


def test_frame_roundtrip():
    sums = np.arange(256) * 10
    payload = build_frame_payload(7, sums, t_us=123_456_789_000, integration_clocks=4000,
                                  clk_hz=200_000, n_avg=4, high_gain=0, led=1, flags=3)
    f = parse_frame(payload)
    assert (f.seq, f.t_device_us, f.integration_clocks, f.n_avg) == (7, 123_456_789_000, 4000, 4)
    assert f.integration_us == 20_000 and f.integration_s == 0.02
    assert f.high_gain is False and f.led is True and f.flags == 3
    np.testing.assert_allclose(f.counts, sums / 4)


def test_parser_splits_and_joins_chunks():
    data = frame_packet(0) + build_packet(PKT_TEXT, b'{"led":1}') + frame_packet(1)
    parser = PacketParser()
    out = []
    for i in range(0, len(data), 7):            # arbitrary chunking
        out += parser.feed(data[i:i + 7])
    assert [t for t, _ in out] == [PKT_FRAME, PKT_TEXT, PKT_FRAME]
    assert out[1][1] == b'{"led":1}'
    assert parse_frame(out[2][1]).seq == 1
    assert parser.crc_errors == 0 and parser.bytes_skipped == 0


def test_parser_skips_text_and_garbage():
    data = b"C12666MA,v1.0\n\xa5\x00junk" + frame_packet(5)
    out = PacketParser().feed(data)
    assert len(out) == 1 and parse_frame(out[0][1]).seq == 5


def test_parser_drops_corrupted_packet_and_resyncs():
    bad = bytearray(frame_packet(1))
    bad[100] ^= 0xFF
    parser = PacketParser()
    out = parser.feed(bytes(bad) + frame_packet(2))
    assert [parse_frame(p).seq for _, p in out] == [2]
    assert parser.crc_errors == 1


def test_parser_keeps_partial_magic():
    pkt = frame_packet(3)
    parser = PacketParser()
    assert parser.feed(b"xx" + pkt[:1]) == []
    out = parser.feed(pkt[1:])
    assert len(out) == 1
