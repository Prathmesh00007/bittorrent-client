"""
protocol.py - BitTorrent Wire Protocol Codec
=============================================
Implements the full BitTorrent peer wire protocol:

  Handshake:
    <pstrlen><pstr><reserved><info_hash><peer_id>

  Message format (after handshake):
    <length prefix (4 bytes BE uint32)><message_id (1 byte)><payload>

  Keep-alive: length=0, no ID, no payload.

Message IDs:
  0  - choke
  1  - unchoke
  2  - interested
  3  - not interested
  4  - have
  5  - bitfield
  6  - request
  7  - piece
  8  - cancel
  20 - extension (simplified: logged and ignored)

All integers in the protocol are big-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# Message IDs
# ---------------------------------------------------------------------------

class MessageID(IntEnum):
    """Wire protocol message type identifiers."""

    CHOKE = 0
    UNCHOKE = 1
    INTERESTED = 2
    NOT_INTERESTED = 3
    HAVE = 4
    BITFIELD = 5
    REQUEST = 6
    PIECE = 7
    CANCEL = 8
    EXTENSION = 20  # BEP-10 extension protocol (simplified)


# ---------------------------------------------------------------------------
# Message Dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Handshake:
    """BitTorrent handshake message.

    Attributes:
        info_hash: 20-byte SHA-1 info hash.
        peer_id:   20-byte peer identifier.
        reserved:  8-byte extension flags (BEP-10: byte 5, bit 4 set for extensions).
    """

    info_hash: bytes
    peer_id: bytes
    reserved: bytes = field(default=b"\x00" * 8)

    PROTOCOL = b"BitTorrent protocol"
    PSTRLEN = len(PROTOCOL)

    def encode(self) -> bytes:
        """Serialize the handshake to bytes for transmission.

        Returns:
            68-byte handshake payload.
        """
        if len(self.info_hash) != 20:
            raise ValueError("info_hash must be exactly 20 bytes")
        if len(self.peer_id) != 20:
            raise ValueError("peer_id must be exactly 20 bytes")
        return (
            bytes([self.PSTRLEN])
            + self.PROTOCOL
            + self.reserved
            + self.info_hash
            + self.peer_id
        )

    @classmethod
    def decode(cls, data: bytes) -> "Handshake":
        """Parse a received handshake from raw bytes.

        Args:
            data: Exactly 68 bytes of handshake data.

        Returns:
            Handshake instance.

        Raises:
            ValueError: If the protocol string or length is wrong.
        """
        if len(data) < 68:
            raise ValueError(
                f"Handshake too short: {len(data)} bytes (need 68)"
            )
        pstrlen = data[0]
        pstr = data[1 : 1 + pstrlen]
        if pstr != cls.PROTOCOL:
            raise ValueError(
                f"Unknown protocol: {pstr!r} (expected {cls.PROTOCOL!r})"
            )
        reserved = data[1 + pstrlen : 1 + pstrlen + 8]
        info_hash = data[1 + pstrlen + 8 : 1 + pstrlen + 28]
        peer_id = data[1 + pstrlen + 28 : 1 + pstrlen + 48]
        return cls(info_hash=info_hash, peer_id=peer_id, reserved=reserved)


@dataclass(frozen=True)
class KeepAlive:
    """Keep-alive message (length=0, sent to prevent timeout)."""

    def encode(self) -> bytes:
        """Encode to 4-byte zero length prefix."""
        return b"\x00\x00\x00\x00"


@dataclass(frozen=True)
class Choke:
    """Peer is choking us — they will not answer our requests."""

    def encode(self) -> bytes:
        return struct.pack(">IB", 1, MessageID.CHOKE)


@dataclass(frozen=True)
class Unchoke:
    """Peer is unchoking us — they will answer our requests."""

    def encode(self) -> bytes:
        return struct.pack(">IB", 1, MessageID.UNCHOKE)


@dataclass(frozen=True)
class Interested:
    """We are interested in data the peer has."""

    def encode(self) -> bytes:
        return struct.pack(">IB", 1, MessageID.INTERESTED)


@dataclass(frozen=True)
class NotInterested:
    """We are not interested in data the peer has."""

    def encode(self) -> bytes:
        return struct.pack(">IB", 1, MessageID.NOT_INTERESTED)


@dataclass(frozen=True)
class Have:
    """Peer announces they have a specific piece.

    Attributes:
        piece_index: Zero-based index of the piece now available.
    """

    piece_index: int

    def encode(self) -> bytes:
        return struct.pack(">IBI", 5, MessageID.HAVE, self.piece_index)


@dataclass(frozen=True)
class Bitfield:
    """Peer announces which pieces they have via a bitfield.

    Attributes:
        bitfield: Bytes where bit i means piece i is available.
                  Bits are ordered MSB first within each byte.
    """

    bitfield: bytes

    def encode(self) -> bytes:
        length = 1 + len(self.bitfield)
        return struct.pack(">IB", length, MessageID.BITFIELD) + self.bitfield

    def has_piece(self, index: int) -> bool:
        """Check if the bitfield indicates piece `index` is available."""
        byte_index = index // 8
        bit_index = 7 - (index % 8)
        if byte_index >= len(self.bitfield):
            return False
        return bool(self.bitfield[byte_index] & (1 << bit_index))

    @classmethod
    def from_bool_list(cls, pieces: list[bool]) -> "Bitfield":
        """Build a Bitfield from a list of boolean piece availability flags.

        Args:
            pieces: List where pieces[i]=True means piece i is available.

        Returns:
            Bitfield instance.
        """
        num_bytes = (len(pieces) + 7) // 8
        data = bytearray(num_bytes)
        for i, have in enumerate(pieces):
            if have:
                data[i // 8] |= 1 << (7 - i % 8)
        return cls(bytes(data))


@dataclass(frozen=True)
class Request:
    """Request a block of data from a peer.

    Attributes:
        piece_index:  Zero-based piece index.
        block_offset: Byte offset within the piece.
        block_length: Number of bytes requested (typically 2^14 = 16384).
    """

    piece_index: int
    block_offset: int
    block_length: int

    STANDARD_BLOCK_SIZE = 16_384  # 2^14 bytes per block

    def encode(self) -> bytes:
        return struct.pack(
            ">IBIII",
            13, MessageID.REQUEST,
            self.piece_index, self.block_offset, self.block_length,
        )


@dataclass(frozen=True)
class Piece:
    """A block of data received from a peer.

    Attributes:
        piece_index:  Zero-based piece index.
        block_offset: Byte offset within the piece.
        data:         Raw block bytes.
    """

    piece_index: int
    block_offset: int
    data: bytes

    def encode(self) -> bytes:
        length = 9 + len(self.data)
        return (
            struct.pack(">IB", length, MessageID.PIECE)
            + struct.pack(">II", self.piece_index, self.block_offset)
            + self.data
        )


@dataclass(frozen=True)
class Cancel:
    """Cancel a previously sent Request.

    Attributes:
        piece_index:  Zero-based piece index.
        block_offset: Byte offset within the piece.
        block_length: Number of bytes in the cancelled request.
    """

    piece_index: int
    block_offset: int
    block_length: int

    def encode(self) -> bytes:
        return struct.pack(
            ">IBIII",
            13, MessageID.CANCEL,
            self.piece_index, self.block_offset, self.block_length,
        )


# Union type for all possible messages
Message = (
    Handshake
    | KeepAlive
    | Choke
    | Unchoke
    | Interested
    | NotInterested
    | Have
    | Bitfield
    | Request
    | Piece
    | Cancel
)


# ---------------------------------------------------------------------------
# Message Codec
# ---------------------------------------------------------------------------

class ProtocolError(Exception):
    """Raised when a protocol violation is detected."""


class MessageCodec:
    """Stateful streaming message parser for the BitTorrent wire protocol.

    Maintains an internal byte buffer.  Feed data with ``feed()``, then call
    ``parse_messages()`` to extract all complete messages.

    This design handles TCP stream fragmentation correctly — messages may
    arrive split across multiple recv() calls.
    """

    _MAX_MESSAGE_LENGTH = 16 * 1024 + 9 + 4  # header + largest possible piece block

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Append received bytes to the internal buffer.

        Args:
            data: Raw bytes from the TCP stream.
        """
        self._buffer.extend(data)

    def parse_messages(self) -> list[Message]:
        """Extract all complete messages from the buffer.

        Returns:
            List of decoded Message objects (may be empty if no complete
            message is available yet).

        Raises:
            ProtocolError: If a message violates the protocol spec.
        """
        messages: list[Message] = []
        while len(self._buffer) >= 4:
            length = struct.unpack_from(">I", self._buffer, 0)[0]

            if length == 0:
                # Keep-alive
                messages.append(KeepAlive())
                del self._buffer[:4]
                continue

            if length > self._MAX_MESSAGE_LENGTH:
                raise ProtocolError(
                    f"Message too large: {length} bytes"
                )

            total = 4 + length
            if len(self._buffer) < total:
                break  # Wait for more data

            payload = bytes(self._buffer[4:total])
            del self._buffer[:total]

            msg_id = payload[0]
            body = payload[1:]
            messages.append(self._decode_payload(msg_id, body))

        return messages

    @staticmethod
    def _decode_payload(msg_id: int, body: bytes) -> Message:
        """Decode a single message from its ID and payload bytes.

        Args:
            msg_id: Integer message type identifier.
            body:   Bytes after the message ID (may be empty).

        Returns:
            Decoded Message object.

        Raises:
            ProtocolError: If the payload doesn't match the expected format.
        """
        try:
            mid = MessageID(msg_id)
        except ValueError:
            raise ProtocolError(f"Unknown message ID: {msg_id}")

        if mid == MessageID.CHOKE:
            return Choke()
        elif mid == MessageID.UNCHOKE:
            return Unchoke()
        elif mid == MessageID.INTERESTED:
            return Interested()
        elif mid == MessageID.NOT_INTERESTED:
            return NotInterested()
        elif mid == MessageID.HAVE:
            if len(body) != 4:
                raise ProtocolError(f"Have message wrong size: {len(body)}")
            (piece_index,) = struct.unpack(">I", body)
            return Have(piece_index=piece_index)
        elif mid == MessageID.BITFIELD:
            return Bitfield(bitfield=body)
        elif mid == MessageID.REQUEST:
            if len(body) != 12:
                raise ProtocolError(f"Request message wrong size: {len(body)}")
            piece_index, block_offset, block_length = struct.unpack(">III", body)
            return Request(
                piece_index=piece_index,
                block_offset=block_offset,
                block_length=block_length,
            )
        elif mid == MessageID.PIECE:
            if len(body) < 8:
                raise ProtocolError(f"Piece message too short: {len(body)}")
            piece_index, block_offset = struct.unpack_from(">II", body, 0)
            data = body[8:]
            return Piece(
                piece_index=piece_index,
                block_offset=block_offset,
                data=data,
            )
        elif mid == MessageID.CANCEL:
            if len(body) != 12:
                raise ProtocolError(f"Cancel message wrong size: {len(body)}")
            piece_index, block_offset, block_length = struct.unpack(">III", body)
            return Cancel(
                piece_index=piece_index,
                block_offset=block_offset,
                block_length=block_length,
            )
        elif mid == MessageID.EXTENSION:
            # BEP-10 extension messages: log and ignore for this implementation
            return KeepAlive()  # Treat as no-op
        else:
            raise ProtocolError(f"Unhandled message ID: {msg_id}")
