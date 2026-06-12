"""
torrent.py - Torrent Metadata Parser
=====================================
Parses .torrent files (Bencoded format) and exposes structured metadata:
  - Info hash (SHA-1 of the info dictionary)
  - Piece hashes list
  - File length, piece length, file name
  - Tracker URL(s)

Bencoding spec:
  - Integers:    i<number>e  (e.g. i42e)
  - Byte strings: <len>:<data>  (e.g. 4:spam)
  - Lists:       l<items>e
  - Dicts:       d<key><value>...e  (keys must be byte strings, sorted)
"""

from __future__ import annotations

import hashlib
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bencoding Decoder
# ---------------------------------------------------------------------------

class BencodeDecodeError(ValueError):
    """Raised when bencode data is malformed."""


def bdecode(data: bytes) -> Any:
    """Decode bencoded bytes into Python objects.

    Args:
        data: Raw bencoded bytes.

    Returns:
        Decoded Python object (int, bytes, list, or dict).

    Raises:
        BencodeDecodeError: If the input is malformed.
    """
    value, idx = _decode(data, 0)
    if idx != len(data):
        raise BencodeDecodeError(
            f"Trailing garbage after decoded value at index {idx}"
        )
    return value


def _decode(data: bytes, idx: int) -> tuple[Any, int]:
    """Internal recursive decoder.

    Args:
        data: Raw bencoded bytes.
        idx:  Current read position.

    Returns:
        Tuple of (decoded_value, next_index).
    """
    if idx >= len(data):
        raise BencodeDecodeError("Unexpected end of data")

    token = chr(data[idx])

    if token == "i":
        return _decode_int(data, idx)
    elif token == "l":
        return _decode_list(data, idx)
    elif token == "d":
        return _decode_dict(data, idx)
    elif token.isdigit():
        return _decode_string(data, idx)
    else:
        raise BencodeDecodeError(
            f"Unknown token {token!r} at index {idx}"
        )


def _decode_int(data: bytes, idx: int) -> tuple[int, int]:
    """Decode a bencoded integer: i<digits>e."""
    end = data.index(b"e", idx + 1)
    raw = data[idx + 1 : end]
    if raw == b"-0":
        raise BencodeDecodeError("Negative zero is not allowed")
    if raw.startswith(b"0") and raw != b"0":
        raise BencodeDecodeError("Leading zeros are not allowed")
    return int(raw), end + 1


def _decode_string(data: bytes, idx: int) -> tuple[bytes, int]:
    """Decode a bencoded byte string: <len>:<content>."""
    colon = data.index(b":", idx)
    length = int(data[idx:colon])
    start = colon + 1
    end = start + length
    if end > len(data):
        raise BencodeDecodeError(
            f"String length {length} exceeds available data"
        )
    return data[start:end], end


def _decode_list(data: bytes, idx: int) -> tuple[list, int]:
    """Decode a bencoded list: l<items>e."""
    result: list = []
    idx += 1  # skip 'l'
    while data[idx : idx + 1] != b"e":
        item, idx = _decode(data, idx)
        result.append(item)
    return result, idx + 1  # skip 'e'


def _decode_dict(data: bytes, idx: int) -> tuple[dict, int]:
    """Decode a bencoded dictionary: d<key><value>...e."""
    result: dict = {}
    idx += 1  # skip 'd'
    while data[idx : idx + 1] != b"e":
        key, idx = _decode_string(data, idx)
        value, idx = _decode(data, idx)
        result[key] = value
    return result, idx + 1  # skip 'e'


# ---------------------------------------------------------------------------
# Bencoding Encoder
# ---------------------------------------------------------------------------

def bencode(value: Any) -> bytes:
    """Encode a Python object into bencoded bytes.

    Args:
        value: Object to encode (int, bytes, str, list, or dict).

    Returns:
        Bencoded bytes.

    Raises:
        TypeError: If value type is not supported.
    """
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    elif isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    elif isinstance(value, str):
        encoded = value.encode("utf-8")
        return str(len(encoded)).encode() + b":" + encoded
    elif isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    elif isinstance(value, dict):
        items = sorted(
            (k if isinstance(k, bytes) else k.encode(), v)
            for k, v in value.items()
        )
        body = b"".join(bencode(k) + bencode(v) for k, v in items)
        return b"d" + body + b"e"
    else:
        raise TypeError(f"Cannot bencode type {type(value)}")


# ---------------------------------------------------------------------------
# Torrent Metadata Model
# ---------------------------------------------------------------------------

@dataclass
class FileInfo:
    """Metadata for a single file within a torrent."""

    name: str
    """File name (may include subdirectory path for multi-file torrents)."""

    length: int
    """File size in bytes."""

    offset: int = 0
    """Byte offset within the concatenated file data (multi-file torrents)."""


@dataclass
class TorrentMetadata:
    """Structured representation of a parsed .torrent file.

    Attributes:
        info_hash:    20-byte SHA-1 hash of the bencoded info dictionary.
        piece_length: Number of bytes per piece (except possibly the last).
        piece_hashes: List of 20-byte SHA-1 hashes, one per piece.
        files:        Ordered list of FileInfo objects.
        trackers:     Ordered list of tracker announce URLs.
        total_length: Total download size in bytes.
        num_pieces:   Total number of pieces.
        name:         Top-level name from the torrent (directory or file name).
    """

    info_hash: bytes
    piece_length: int
    piece_hashes: list[bytes]
    files: list[FileInfo]
    trackers: list[str]
    total_length: int
    name: str

    @property
    def num_pieces(self) -> int:
        """Return the total number of pieces."""
        return len(self.piece_hashes)

    def piece_size(self, piece_index: int) -> int:
        """Return the size in bytes of a specific piece.

        The last piece may be smaller than piece_length.

        Args:
            piece_index: Zero-based piece index.

        Returns:
            Size of the piece in bytes.
        """
        if piece_index == self.num_pieces - 1:
            remainder = self.total_length % self.piece_length
            return remainder if remainder else self.piece_length
        return self.piece_length

    def block_offset_to_file(
        self, piece_index: int, block_offset: int
    ) -> list[tuple[FileInfo, int, int, int]]:
        """Map a block within a piece to one or more file regions.

        Args:
            piece_index:  Zero-based piece index.
            block_offset: Byte offset within the piece.

        Returns:
            List of (FileInfo, file_offset, block_start, block_length) tuples.
            For single-file torrents this always has one element.
        """
        global_offset = piece_index * self.piece_length + block_offset
        regions = []
        for fi in self.files:
            file_end = fi.offset + fi.length
            if global_offset >= file_end:
                continue
            if global_offset + 1 <= fi.offset:
                break
            start_in_file = global_offset - fi.offset
            available = fi.length - start_in_file
            regions.append((fi, fi.offset + start_in_file, 0, available))
            break  # simplified: single-file support
        return regions


# ---------------------------------------------------------------------------
# Torrent Parser
# ---------------------------------------------------------------------------

class TorrentParser:
    """Parses a .torrent file and returns a TorrentMetadata object."""

    PIECE_HASH_SIZE = 20  # SHA-1 digest length

    def parse_file(self, path: Union[str, Path]) -> TorrentMetadata:
        """Parse a .torrent file from disk.

        Args:
            path: Path to the .torrent file.

        Returns:
            Populated TorrentMetadata instance.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            BencodeDecodeError: If the file is not valid bencode.
            ValueError: If required fields are missing.
        """
        path = Path(path)
        logger.info("Parsing torrent file: %s", path)
        raw = path.read_bytes()
        return self.parse_bytes(raw)

    def parse_bytes(self, raw: bytes) -> TorrentMetadata:
        """Parse raw bencoded torrent bytes.

        Args:
            raw: Raw bytes from a .torrent file.

        Returns:
            Populated TorrentMetadata instance.

        Raises:
            BencodeDecodeError: If raw bytes are not valid bencode.
            ValueError: If required metadata fields are missing.
        """
        torrent_dict = bdecode(raw)
        if not isinstance(torrent_dict, dict):
            raise ValueError("Torrent file must be a bencoded dictionary")

        info = torrent_dict.get(b"info")
        if info is None:
            raise ValueError("Missing 'info' key in torrent")

        info_hash = self._compute_info_hash(raw)
        piece_hashes = self._extract_piece_hashes(info)
        files, total_length = self._extract_files(info)
        trackers = self._extract_trackers(torrent_dict)
        piece_length = info.get(b"piece length")
        if not isinstance(piece_length, int):
            raise ValueError("Missing or invalid 'piece length'")

        name_raw = info.get(b"name", b"unknown")
        name = name_raw.decode("utf-8", errors="replace")

        logger.info(
            "Parsed torrent: name=%s pieces=%d total_length=%d trackers=%d",
            name, len(piece_hashes), total_length, len(trackers),
        )

        return TorrentMetadata(
            info_hash=info_hash,
            piece_length=piece_length,
            piece_hashes=piece_hashes,
            files=files,
            trackers=trackers,
            total_length=total_length,
            name=name,
        )

    def _compute_info_hash(self, raw: bytes) -> bytes:
        """Extract and SHA-1 hash the raw bencoded info dict."""
        # Find the info value in the raw bytes by re-encoding
        # For correctness, we locate 'd4:info' and extract the value
        torrent_dict = bdecode(raw)
        info_dict = torrent_dict[b"info"]
        info_encoded = bencode(info_dict)
        return hashlib.sha1(info_encoded).digest()

    def _extract_piece_hashes(self, info: dict) -> list[bytes]:
        """Extract individual 20-byte piece hashes from concatenated string."""
        raw_pieces = info.get(b"pieces", b"")
        if len(raw_pieces) % self.PIECE_HASH_SIZE != 0:
            raise ValueError(
                f"'pieces' length {len(raw_pieces)} is not a multiple of {self.PIECE_HASH_SIZE}"
            )
        n = len(raw_pieces) // self.PIECE_HASH_SIZE
        return [
            raw_pieces[i * self.PIECE_HASH_SIZE : (i + 1) * self.PIECE_HASH_SIZE]
            for i in range(n)
        ]

    def _extract_files(self, info: dict) -> tuple[list[FileInfo], int]:
        """Extract file list and total download size from info dict."""
        files: list[FileInfo] = []
        total = 0

        if b"files" in info:
            # Multi-file torrent
            base_name = info.get(b"name", b"").decode("utf-8", errors="replace")
            for file_dict in info[b"files"]:
                path_parts = [
                    p.decode("utf-8", errors="replace")
                    for p in file_dict.get(b"path", [])
                ]
                file_name = "/".join(path_parts) if path_parts else "unknown"
                length = file_dict.get(b"length", 0)
                files.append(FileInfo(
                    name=f"{base_name}/{file_name}",
                    length=length,
                    offset=total,
                ))
                total += length
        else:
            # Single-file torrent
            length = info.get(b"length", 0)
            name = info.get(b"name", b"download").decode("utf-8", errors="replace")
            files.append(FileInfo(name=name, length=length, offset=0))
            total = length

        return files, total

    def _extract_trackers(self, torrent_dict: dict) -> list[str]:
        """Extract all tracker URLs from announce and announce-list."""
        trackers: list[str] = []
        announce = torrent_dict.get(b"announce")
        if isinstance(announce, bytes):
            trackers.append(announce.decode("utf-8", errors="replace"))

        announce_list = torrent_dict.get(b"announce-list", [])
        for tier in announce_list:
            for url in tier:
                decoded = url.decode("utf-8", errors="replace") if isinstance(url, bytes) else str(url)
                if decoded not in trackers:
                    trackers.append(decoded)

        return trackers


# ---------------------------------------------------------------------------
# Mock Torrent Builder (for testing without a real .torrent file)
# ---------------------------------------------------------------------------

def create_mock_torrent(
    file_name: str = "test_file.bin",
    file_size: int = 10 * 1024 * 1024,  # 10 MB
    piece_length: int = 512 * 1024,      # 512 KB
    tracker_url: str = "http://localhost:6969/announce",
    content: Optional[bytes] = None,
) -> tuple["TorrentMetadata", bytes]:
    """Create a mock TorrentMetadata for local testing.

    Generates random piece hashes based on provided or random content.
    This is an educational helper — in real BitTorrent, hashes come from
    the actual file content.

    Args:
        file_name:    Name of the (virtual) file.
        file_size:    Total file size in bytes.
        piece_length: Bytes per piece.
        tracker_url:  Announce URL.
        content:      Optional file bytes to hash; random bytes used if None.

    Returns:
        A TorrentMetadata object ready for use with the download engine.
    """
    import os

    if content is None:
        content = os.urandom(file_size)

    num_pieces = (file_size + piece_length - 1) // piece_length
    piece_hashes: list[bytes] = []
    for i in range(num_pieces):
        chunk = content[i * piece_length : (i + 1) * piece_length]
        piece_hashes.append(hashlib.sha1(chunk).digest())

    # Build a mock info dict and compute its hash
    pieces_raw = b"".join(piece_hashes)
    info_dict: dict[bytes, Any] = {
        b"name": file_name.encode(),
        b"length": file_size,
        b"piece length": piece_length,
        b"pieces": pieces_raw,
    }
    info_encoded = bencode(info_dict)
    info_hash = hashlib.sha1(info_encoded).digest()

    files = [FileInfo(name=file_name, length=file_size, offset=0)]

    return TorrentMetadata(
        info_hash=info_hash,
        piece_length=piece_length,
        piece_hashes=piece_hashes,
        files=files,
        trackers=[tracker_url],
        total_length=file_size,
        name=file_name,
    ), content  # also return content so tests can seed peers with it
