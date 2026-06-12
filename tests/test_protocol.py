"""
tests/test_protocol.py - Unit Tests for Wire Protocol Codec
=============================================================
Tests all message types: encode → decode roundtrips, edge cases,
malformed packet handling, and streaming buffer behavior.
"""

import struct
import pytest

from bittorrent.protocol import (
    Handshake,
    KeepAlive,
    Choke,
    Unchoke,
    Interested,
    NotInterested,
    Have,
    Bitfield,
    Request,
    Piece,
    Cancel,
    MessageCodec,
    ProtocolError,
    MessageID,
)


# ---------------------------------------------------------------------------
# Handshake Tests
# ---------------------------------------------------------------------------

class TestHandshake:
    INFO_HASH = b"\x01" * 20
    PEER_ID = b"\x02" * 20

    def test_encode_length(self):
        hs = Handshake(info_hash=self.INFO_HASH, peer_id=self.PEER_ID)
        encoded = hs.encode()
        assert len(encoded) == 68

    def test_encode_decode_roundtrip(self):
        hs = Handshake(info_hash=self.INFO_HASH, peer_id=self.PEER_ID)
        encoded = hs.encode()
        decoded = Handshake.decode(encoded)
        assert decoded.info_hash == self.INFO_HASH
        assert decoded.peer_id == self.PEER_ID

    def test_protocol_string(self):
        hs = Handshake(info_hash=self.INFO_HASH, peer_id=self.PEER_ID)
        encoded = hs.encode()
        assert encoded[0] == 19  # pstrlen
        assert encoded[1:20] == b"BitTorrent protocol"

    def test_reserved_bytes_default_zero(self):
        hs = Handshake(info_hash=self.INFO_HASH, peer_id=self.PEER_ID)
        encoded = hs.encode()
        assert encoded[20:28] == b"\x00" * 8

    def test_wrong_info_hash_length_raises(self):
        with pytest.raises(ValueError, match="20 bytes"):
            Handshake(info_hash=b"short", peer_id=self.PEER_ID).encode()

    def test_wrong_peer_id_length_raises(self):
        with pytest.raises(ValueError, match="20 bytes"):
            Handshake(info_hash=self.INFO_HASH, peer_id=b"short").encode()

    def test_decode_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            Handshake.decode(b"\x00" * 10)

    def test_decode_wrong_protocol_raises(self):
        bad_hs = bytes([19]) + b"WrongProtocolXXXXXX" + b"\x00" * 48
        with pytest.raises(ValueError, match="Unknown protocol"):
            Handshake.decode(bad_hs)


# ---------------------------------------------------------------------------
# Simple Message Encode Tests
# ---------------------------------------------------------------------------

class TestSimpleMessages:
    def test_keepalive(self):
        msg = KeepAlive()
        assert msg.encode() == b"\x00\x00\x00\x00"

    def test_choke(self):
        data = Choke().encode()
        assert len(data) == 5
        assert data[4] == MessageID.CHOKE

    def test_unchoke(self):
        data = Unchoke().encode()
        assert data[4] == MessageID.UNCHOKE

    def test_interested(self):
        data = Interested().encode()
        assert data[4] == MessageID.INTERESTED

    def test_not_interested(self):
        data = NotInterested().encode()
        assert data[4] == MessageID.NOT_INTERESTED

    def test_have(self):
        msg = Have(piece_index=42)
        data = msg.encode()
        assert len(data) == 9
        assert data[4] == MessageID.HAVE
        (index,) = struct.unpack_from(">I", data, 5)
        assert index == 42


# ---------------------------------------------------------------------------
# Bitfield Tests
# ---------------------------------------------------------------------------

class TestBitfield:
    def test_encode_decode(self):
        bf_bytes = bytes([0b10110001, 0b01101110])
        msg = Bitfield(bitfield=bf_bytes)
        data = msg.encode()
        assert data[4] == MessageID.BITFIELD
        assert data[5:] == bf_bytes

    def test_has_piece_true(self):
        # byte 0 = 0b10000000 → piece 0 is set
        bf = Bitfield(bitfield=bytes([0b10000000]))
        assert bf.has_piece(0) is True

    def test_has_piece_false(self):
        bf = Bitfield(bitfield=bytes([0b10000000]))
        assert bf.has_piece(1) is False

    def test_from_bool_list(self):
        pieces = [True, False, True, True, False, False, False, False]
        bf = Bitfield.from_bool_list(pieces)
        assert bf.has_piece(0) is True
        assert bf.has_piece(1) is False
        assert bf.has_piece(2) is True
        assert bf.has_piece(3) is True
        assert bf.has_piece(4) is False

    def test_from_bool_list_multi_byte(self):
        pieces = [False] * 8 + [True, False, False, False, False, False, False, False]
        bf = Bitfield.from_bool_list(pieces)
        assert bf.has_piece(8) is True
        assert bf.has_piece(0) is False

    def test_out_of_range_piece(self):
        bf = Bitfield(bitfield=bytes([0xFF]))
        assert bf.has_piece(100) is False


# ---------------------------------------------------------------------------
# Request / Piece / Cancel Tests
# ---------------------------------------------------------------------------

class TestRequestMessage:
    def test_encode(self):
        req = Request(piece_index=3, block_offset=16384, block_length=16384)
        data = req.encode()
        assert len(data) == 17  # 4 (length) + 1 (ID) + 12 (payload)
        assert data[4] == MessageID.REQUEST

    def test_encode_values(self):
        req = Request(piece_index=1, block_offset=0, block_length=16384)
        data = req.encode()
        pi, bo, bl = struct.unpack_from(">III", data, 5)
        assert pi == 1
        assert bo == 0
        assert bl == 16384


class TestPieceMessage:
    def test_encode(self):
        payload = b"\xAB" * 100
        msg = Piece(piece_index=5, block_offset=0, data=payload)
        data = msg.encode()
        assert data[4] == MessageID.PIECE
        pi, bo = struct.unpack_from(">II", data, 5)
        assert pi == 5
        assert bo == 0
        assert data[13:] == payload

    def test_encode_length_prefix(self):
        payload = b"\x00" * 50
        msg = Piece(piece_index=0, block_offset=0, data=payload)
        data = msg.encode()
        (length,) = struct.unpack_from(">I", data, 0)
        assert length == 9 + 50  # 1 (ID) + 8 (index+offset) + payload


class TestCancelMessage:
    def test_encode(self):
        msg = Cancel(piece_index=2, block_offset=32768, block_length=16384)
        data = msg.encode()
        assert len(data) == 17
        assert data[4] == MessageID.CANCEL


# ---------------------------------------------------------------------------
# MessageCodec Tests (streaming parser)
# ---------------------------------------------------------------------------

class TestMessageCodec:
    def _make_message(self, msg) -> bytes:
        return msg.encode()

    def test_parse_single_unchoke(self):
        codec = MessageCodec()
        codec.feed(Unchoke().encode())
        messages = codec.parse_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], Unchoke)

    def test_parse_keepalive(self):
        codec = MessageCodec()
        codec.feed(b"\x00\x00\x00\x00")
        messages = codec.parse_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], KeepAlive)

    def test_parse_multiple_messages(self):
        codec = MessageCodec()
        codec.feed(Choke().encode() + Unchoke().encode() + Interested().encode())
        messages = codec.parse_messages()
        assert len(messages) == 3
        assert isinstance(messages[0], Choke)
        assert isinstance(messages[1], Unchoke)
        assert isinstance(messages[2], Interested)

    def test_parse_fragmented_message(self):
        """Simulate TCP fragmentation — message arrives in multiple chunks."""
        codec = MessageCodec()
        full = Have(piece_index=7).encode()
        # Feed first 3 bytes, then the rest
        codec.feed(full[:3])
        assert codec.parse_messages() == []
        codec.feed(full[3:])
        messages = codec.parse_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], Have)
        assert messages[0].piece_index == 7

    def test_parse_piece_message(self):
        codec = MessageCodec()
        data = b"\xFF" * 16384
        msg = Piece(piece_index=4, block_offset=32768, data=data)
        codec.feed(msg.encode())
        messages = codec.parse_messages()
        assert len(messages) == 1
        parsed = messages[0]
        assert isinstance(parsed, Piece)
        assert parsed.piece_index == 4
        assert parsed.block_offset == 32768
        assert parsed.data == data

    def test_parse_request_message(self):
        codec = MessageCodec()
        req = Request(piece_index=10, block_offset=0, block_length=16384)
        codec.feed(req.encode())
        messages = codec.parse_messages()
        parsed = messages[0]
        assert isinstance(parsed, Request)
        assert parsed.piece_index == 10
        assert parsed.block_length == 16384

    def test_parse_bitfield(self):
        codec = MessageCodec()
        bf = Bitfield(bitfield=bytes([0xFF, 0xAB]))
        codec.feed(bf.encode())
        messages = codec.parse_messages()
        parsed = messages[0]
        assert isinstance(parsed, Bitfield)
        assert parsed.bitfield == bytes([0xFF, 0xAB])

    def test_unknown_message_id_raises(self):
        codec = MessageCodec()
        # Construct a message with unknown ID 99
        bad_msg = struct.pack(">IB", 1, 99)
        codec.feed(bad_msg)
        with pytest.raises(ProtocolError, match="Unknown message ID"):
            codec.parse_messages()

    def test_message_too_large_raises(self):
        codec = MessageCodec()
        # 32 MB message — clearly too large
        too_large = struct.pack(">I", 32 * 1024 * 1024)
        codec.feed(too_large)
        with pytest.raises(ProtocolError, match="too large"):
            codec.parse_messages()

    def test_partial_then_full_then_partial(self):
        """Simulate real TCP behavior with mixed fragment sizes."""
        codec = MessageCodec()
        msg1 = Have(piece_index=1).encode()
        msg2 = Have(piece_index=2).encode()
        combined = msg1 + msg2

        # Feed in 3 chunks
        codec.feed(combined[:5])
        r1 = codec.parse_messages()

        codec.feed(combined[5:11])
        r2 = codec.parse_messages()

        codec.feed(combined[11:])
        r3 = codec.parse_messages()

        all_messages = r1 + r2 + r3
        # We may get 0, 1, or 2 messages per chunk depending on framing
        assert len(all_messages) == 2
        indices = {m.piece_index for m in all_messages}
        assert indices == {1, 2}
