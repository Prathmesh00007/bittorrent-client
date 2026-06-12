"""
tests/test_torrent.py - Unit Tests for Torrent Metadata Parsing
================================================================
Tests bencode encode/decode, TorrentParser, and mock torrent builder.
"""

import hashlib
import pytest

from bittorrent.torrent import (
    bdecode,
    bencode,
    BencodeDecodeError,
    TorrentParser,
    TorrentMetadata,
    FileInfo,
    create_mock_torrent,
)


# ---------------------------------------------------------------------------
# Bencode Decoding Tests
# ---------------------------------------------------------------------------

class TestBdecodeIntegers:
    def test_positive(self):
        assert bdecode(b"i42e") == 42

    def test_zero(self):
        assert bdecode(b"i0e") == 0

    def test_negative(self):
        assert bdecode(b"i-17e") == -17

    def test_negative_zero_raises(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"i-0e")

    def test_leading_zero_raises(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"i042e")

    def test_large(self):
        assert bdecode(b"i1234567890e") == 1234567890


class TestBdecodeStrings:
    def test_simple(self):
        assert bdecode(b"4:spam") == b"spam"

    def test_empty(self):
        assert bdecode(b"0:") == b""

    def test_with_special_chars(self):
        data = b"\x00\xFF\x7F"
        encoded = str(len(data)).encode() + b":" + data
        assert bdecode(encoded) == data

    def test_length_too_long_raises(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"100:short")


class TestBdecodeLists:
    def test_empty(self):
        assert bdecode(b"le") == []

    def test_integers(self):
        assert bdecode(b"li1ei2ei3ee") == [1, 2, 3]

    def test_strings(self):
        assert bdecode(b"l4:spam3:egge") == [b"spam", b"egg"]

    def test_nested(self):
        assert bdecode(b"lli1ei2eei3ee") == [[1, 2], 3]


class TestBdecodeDicts:
    def test_empty(self):
        assert bdecode(b"de") == {}

    def test_simple(self):
        result = bdecode(b"d3:foo3:bare")
        assert result == {b"foo": b"bar"}

    def test_nested(self):
        result = bdecode(b"d3:keyd5:valuei99eee")
        assert result == {b"key": {b"value": 99}}


class TestBdecodeErrors:
    def test_trailing_garbage(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"i1eXXX")

    def test_unknown_token(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"X:something")

    def test_empty_input(self):
        with pytest.raises(BencodeDecodeError):
            bdecode(b"")


# ---------------------------------------------------------------------------
# Bencode Encoding Tests
# ---------------------------------------------------------------------------

class TestBencode:
    def test_integer(self):
        assert bencode(42) == b"i42e"

    def test_negative(self):
        assert bencode(-5) == b"i-5e"

    def test_bytes(self):
        assert bencode(b"spam") == b"4:spam"

    def test_str(self):
        assert bencode("hello") == b"5:hello"

    def test_list(self):
        assert bencode([1, b"two"]) == b"li1e3:twoe"

    def test_dict_sorted_keys(self):
        # Dict keys must be sorted in bencoded output
        result = bencode({b"b": 2, b"a": 1})
        assert result == b"d1:ai1e1:bi2ee"

    def test_roundtrip(self):
        original = {b"info": {b"name": b"test", b"length": 1024}}
        assert bdecode(bencode(original)) == original

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            bencode(3.14)


# ---------------------------------------------------------------------------
# TorrentParser Tests
# ---------------------------------------------------------------------------

class TestTorrentParser:
    def _build_torrent_bytes(
        self,
        name=b"testfile.bin",
        length=1024 * 512,
        piece_length=256 * 1024,
        num_pieces=None,
        tracker=b"http://tracker.example.com/announce",
    ):
        """Helper: build minimal valid torrent bytes."""
        if num_pieces is None:
            num_pieces = (length + piece_length - 1) // piece_length

        pieces_raw = b"\x00" * (20 * num_pieces)
        info = {
            b"name": name,
            b"length": length,
            b"piece length": piece_length,
            b"pieces": pieces_raw,
        }
        torrent = {
            b"announce": tracker,
            b"info": info,
        }
        return bencode(torrent)

    def test_parse_minimal(self):
        raw = self._build_torrent_bytes()
        parser = TorrentParser()
        meta = parser.parse_bytes(raw)

        assert meta.name == "testfile.bin"
        assert meta.total_length == 1024 * 512
        assert meta.piece_length == 256 * 1024
        assert meta.num_pieces == 2
        assert len(meta.piece_hashes) == 2
        assert meta.trackers == ["http://tracker.example.com/announce"]

    def test_info_hash_is_20_bytes(self):
        raw = self._build_torrent_bytes()
        meta = TorrentParser().parse_bytes(raw)
        assert len(meta.info_hash) == 20

    def test_info_hash_is_sha1_of_info(self):
        raw = self._build_torrent_bytes(name=b"unique_name.bin")
        parsed = bdecode(raw)
        expected_hash = hashlib.sha1(bencode(parsed[b"info"])).digest()
        meta = TorrentParser().parse_bytes(raw)
        assert meta.info_hash == expected_hash

    def test_announce_list(self):
        info = {
            b"name": b"f.bin",
            b"length": 100,
            b"piece length": 100,
            b"pieces": b"\x00" * 20,
        }
        torrent = {
            b"announce": b"http://t1.example.com/announce",
            b"announce-list": [
                [b"http://t2.example.com/announce"],
                [b"http://t3.example.com/announce"],
            ],
            b"info": info,
        }
        meta = TorrentParser().parse_bytes(bencode(torrent))
        assert "http://t1.example.com/announce" in meta.trackers
        assert "http://t2.example.com/announce" in meta.trackers
        assert "http://t3.example.com/announce" in meta.trackers

    def test_missing_info_raises(self):
        raw = bencode({b"announce": b"http://example.com"})
        with pytest.raises(ValueError, match="Missing 'info'"):
            TorrentParser().parse_bytes(raw)

    def test_missing_piece_length_raises(self):
        info = {
            b"name": b"f.bin",
            b"length": 100,
            b"pieces": b"\x00" * 20,
        }
        raw = bencode({b"info": info})
        with pytest.raises(ValueError, match="piece length"):
            TorrentParser().parse_bytes(raw)

    def test_piece_size_last_piece(self):
        # File = 700 bytes, piece_length = 512 → last piece = 188 bytes
        raw = self._build_torrent_bytes(length=700, piece_length=512, num_pieces=2)
        meta = TorrentParser().parse_bytes(raw)
        assert meta.piece_size(0) == 512
        assert meta.piece_size(1) == 188  # 700 - 512

    def test_piece_size_exact_multiple(self):
        # File = 1024 bytes, piece_length = 512 → all pieces same size
        raw = self._build_torrent_bytes(length=1024, piece_length=512, num_pieces=2)
        meta = TorrentParser().parse_bytes(raw)
        assert meta.piece_size(0) == 512
        assert meta.piece_size(1) == 512


# ---------------------------------------------------------------------------
# Mock Torrent Tests
# ---------------------------------------------------------------------------

class TestMockTorrent:
    def test_creates_valid_metadata(self):
        meta, content = create_mock_torrent(
            file_name="test.bin",
            file_size=1024,
            piece_length=512,
        )
        assert meta.num_pieces == 2
        assert meta.total_length == 1024
        assert meta.piece_length == 512
        assert len(meta.info_hash) == 20

    def test_piece_hashes_correct(self):
        meta, content = create_mock_torrent(
            file_size=1024,
            piece_length=512,
        )
        # Verify hash of first piece
        expected = hashlib.sha1(content[:512]).digest()
        assert meta.piece_hashes[0] == expected
        # Verify hash of second piece
        expected2 = hashlib.sha1(content[512:1024]).digest()
        assert meta.piece_hashes[1] == expected2

    def test_deterministic_with_provided_content(self):
        content = b"\xAB" * 2048
        meta1, _ = create_mock_torrent(content=content, file_size=2048, piece_length=1024)
        meta2, _ = create_mock_torrent(content=content, file_size=2048, piece_length=1024)
        assert meta1.info_hash == meta2.info_hash
