"""
peer.py - Async Peer Connection Manager
========================================
Handles a single TCP connection to one BitTorrent peer:

  1. Performs the handshake
  2. Sends/receives wire protocol messages using the MessageCodec
  3. Manages choke/unchoke and interest state
  4. Dispatches received blocks to the shared piece picker
  5. Reports download throughput statistics

State machine per connection:
  CONNECTING → HANDSHAKING → ACTIVE → [CHOKED / UNCHOKED] → CLOSED

Design decisions:
  - One asyncio Task per peer (created by PeerManager)
  - Uses asyncio.StreamReader / StreamWriter for async TCP
  - Non-blocking: all waits are via await, no thread blocking
  - Timeout on handshake (10s) and on each block request (30s)
  - Rate limiting: respects choking status before sending requests
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from .protocol import (
    Handshake, KeepAlive, Choke, Unchoke,
    Interested, NotInterested, Have, Bitfield,
    Request, Piece, Cancel, MessageCodec, ProtocolError, Message,
)
from .pieces import (
    PeerAvailability, PiecePicker, Block, BlockState, BLOCK_SIZE,
)

if TYPE_CHECKING:
    from .torrent import TorrentMetadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HANDSHAKE_TIMEOUT = 15.0     # seconds
BLOCK_REQUEST_TIMEOUT = 30.0 # seconds per block
KEEPALIVE_INTERVAL = 90.0    # seconds between keep-alives
MAX_PIPELINED_REQUESTS = 10  # max in-flight block requests per peer
CONNECT_TIMEOUT = 10.0       # seconds for TCP connect


# ---------------------------------------------------------------------------
# Peer State
# ---------------------------------------------------------------------------

class PeerState(Enum):
    """Lifecycle states of a peer connection."""

    CONNECTING = auto()
    HANDSHAKING = auto()
    ACTIVE = auto()
    CLOSING = auto()
    CLOSED = auto()


@dataclass
class PeerStats:
    """Runtime statistics for a peer connection.

    Used by the choking/unchoking algorithm to rank peers.
    """

    bytes_downloaded: int = 0
    """Total bytes of block data received from this peer."""

    bytes_uploaded: int = 0
    """Total bytes of block data sent to this peer."""

    download_rate: float = 0.0
    """Rolling average download rate (bytes/second)."""

    upload_rate: float = 0.0
    """Rolling average upload rate (bytes/second)."""

    last_rate_update: float = field(default_factory=time.monotonic)
    """Timestamp of last rate calculation."""

    blocks_received: int = 0
    """Total number of blocks successfully received."""

    connection_time: float = field(default_factory=time.monotonic)
    """Timestamp when the connection was established."""

    _bytes_since_last: int = 0
    """Bytes accumulated since last rate update."""

    def record_bytes(self, n: int) -> None:
        """Record n bytes downloaded for rate tracking."""
        self.bytes_downloaded += n
        self._bytes_since_last += n
        self.blocks_received += 1

    def update_rate(self) -> None:
        """Recalculate rolling download rate (call every few seconds)."""
        now = time.monotonic()
        elapsed = now - self.last_rate_update
        if elapsed > 0:
            self.download_rate = self._bytes_since_last / elapsed
            self._bytes_since_last = 0
            self.last_rate_update = now


# ---------------------------------------------------------------------------
# Peer Connection
# ---------------------------------------------------------------------------

class PeerConnection:
    """Manages a single async TCP connection to a BitTorrent peer.

    Args:
        host:        Remote peer IP address.
        port:        Remote peer port.
        metadata:    Torrent metadata (info_hash, num_pieces, etc.).
        our_peer_id: Our 20-byte peer identifier.
        picker:      Shared PiecePicker instance.
        piece_queue: asyncio.Queue for completed, verified pieces.
        on_block:    Callback invoked with (piece_index, block_offset, data)
                     when a block is received.
    """

    def __init__(
        self,
        host: str,
        port: int,
        metadata: "TorrentMetadata",
        our_peer_id: bytes,
        picker: PiecePicker,
        block_queue: asyncio.Queue,
    ) -> None:
        self.host = host
        self.port = port
        self.metadata = metadata
        self.our_peer_id = our_peer_id
        self.picker = picker
        self.block_queue = block_queue

        # State
        self.state = PeerState.CONNECTING
        self.peer_id: Optional[bytes] = None
        self.stats = PeerStats()
        self.availability = PeerAvailability(
            peer_id=f"{host}:{port}",
            num_pieces=metadata.num_pieces,
        )

        # Choke/interest flags
        self._am_choking = True        # We are choking the remote peer
        self._am_interested = False    # We are interested in remote peer
        self._peer_choking = True      # Remote peer is choking us
        self._peer_interested = False  # Remote peer is interested in us

        # I/O
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._codec = MessageCodec()
        self._send_lock = asyncio.Lock()
        self._in_flight: dict[tuple[int, int], float] = {}  # (piece, offset) -> timestamp
        self._closed = False
        self._peer_key = f"{host}:{port}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main coroutine: connect, handshake, then run message loop.

        This is designed to be run as an asyncio Task. It handles all
        exceptions internally and cleans up on exit.
        """
        try:
            await self._connect()
            await self._handshake()
            await asyncio.gather(
                self._read_loop(),
                self._request_loop(),
                self._keepalive_loop(),
            )
        except asyncio.CancelledError:
            logger.debug("Peer %s task cancelled", self._peer_key)
        except Exception as exc:
            logger.warning("Peer %s error: %s", self._peer_key, exc)
        finally:
            await self._close()
            self.picker.peer_disconnected(self.availability)

    async def send_message(self, message: Message) -> None:
        """Send a wire protocol message to this peer.

        Args:
            message: Any Message type with an encode() method.
        """
        if self._writer is None or self._closed:
            return
        data = message.encode()
        async with self._send_lock:
            try:
                self._writer.write(data)
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                logger.debug("Send error to %s: %s", self._peer_key, exc)
                await self._close()

    @property
    def is_unchoked(self) -> bool:
        """True if the remote peer is not choking us."""
        return not self._peer_choking

    @property
    def peer_key(self) -> str:
        """Unique string identifier for this peer."""
        return self._peer_key

    @property
    def is_active(self) -> bool:
        """True if the connection is in ACTIVE state."""
        return self.state == PeerState.ACTIVE

    # ------------------------------------------------------------------
    # Internal: Connection and Handshake
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Establish TCP connection."""
        logger.debug("Connecting to %s", self._peer_key)
        self.state = PeerState.CONNECTING
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            logger.debug("TCP connected to %s", self._peer_key)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ConnectionError(
                f"Failed to connect to {self._peer_key}: {exc}"
            )

    async def _handshake(self) -> None:
        """Perform the BitTorrent handshake exchange."""
        self.state = PeerState.HANDSHAKING
        hs = Handshake(
            info_hash=self.metadata.info_hash,
            peer_id=self.our_peer_id,
        )
        async with self._send_lock:
            self._writer.write(hs.encode())
            await self._writer.drain()

        # Read exactly 68 bytes for the response handshake
        try:
            raw = await asyncio.wait_for(
                self._reader.readexactly(68),
                timeout=HANDSHAKE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise ConnectionError(f"Handshake timeout from {self._peer_key}")
        except asyncio.IncompleteReadError:
            raise ConnectionError(
                f"Peer {self._peer_key} closed connection during handshake"
            )

        remote_hs = Handshake.decode(raw)
        if remote_hs.info_hash != self.metadata.info_hash:
            raise ProtocolError(
                f"Info hash mismatch from {self._peer_key}"
            )
        self.peer_id = remote_hs.peer_id
        self.state = PeerState.ACTIVE
        logger.info("Handshake complete with %s", self._peer_key)

        # Send our bitfield (empty — we have nothing yet as a downloader)
        # A seeding peer would send a full bitfield here

    # ------------------------------------------------------------------
    # Internal: Read Loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """Continuously read data from the peer and dispatch messages."""
        while not self._closed:
            try:
                raw = await asyncio.wait_for(
                    self._reader.read(4096),
                    timeout=BLOCK_REQUEST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.debug("Read timeout from %s", self._peer_key)
                continue
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                break

            if not raw:
                logger.debug("Peer %s closed connection", self._peer_key)
                break

            self._codec.feed(raw)
            try:
                messages = self._codec.parse_messages()
            except ProtocolError as exc:
                logger.warning(
                    "Protocol error from %s: %s", self._peer_key, exc
                )
                break

            for msg in messages:
                await self._handle_message(msg)

        self._closed = True

    async def _handle_message(self, msg: Message) -> None:
        """Dispatch a received message to the appropriate handler.

        Args:
            msg: Decoded message object.
        """
        if isinstance(msg, KeepAlive):
            pass  # No action needed

        elif isinstance(msg, Choke):
            self._peer_choking = True
            logger.debug("Peer %s choked us", self._peer_key)
            # Free all pending in-flight requests (they won't be answered)
            self._in_flight.clear()

        elif isinstance(msg, Unchoke):
            self._peer_choking = False
            logger.debug("Peer %s unchoked us", self._peer_key)

        elif isinstance(msg, Interested):
            self._peer_interested = True

        elif isinstance(msg, NotInterested):
            self._peer_interested = False

        elif isinstance(msg, Have):
            self.availability.set_have(msg.piece_index)
            self.picker.peer_has_piece(msg.piece_index)
            if not self._am_interested and not self.picker.is_complete:
                await self.send_message(Interested())
                self._am_interested = True

        elif isinstance(msg, Bitfield):
            self.availability.apply_bitfield(msg.bitfield)
            for i in range(self.metadata.num_pieces):
                if self.availability.has_piece[i]:
                    self.picker.peer_has_piece(i)
            if not self.picker.is_complete:
                await self.send_message(Interested())
                self._am_interested = True
            logger.debug(
                "Peer %s has %d/%d pieces",
                self._peer_key,
                sum(self.availability.has_piece),
                self.metadata.num_pieces,
            )

        elif isinstance(msg, Piece):
            await self._handle_piece(msg)

        elif isinstance(msg, Request):
            # Simplified: we don't upload in this implementation
            # A full client would serve blocks to unchoked peers here
            pass

        elif isinstance(msg, Cancel):
            pass  # Cancel handling for a seeder role

    async def _handle_piece(self, msg: Piece) -> None:
        """Process a received piece (block) from a peer.

        Args:
            msg: Piece message containing index, offset, and data.
        """
        key = (msg.piece_index, msg.block_offset)
        self._in_flight.pop(key, None)

        self.stats.record_bytes(len(msg.data))

        # Forward to the block assembly queue
        await self.block_queue.put((msg.piece_index, msg.block_offset, msg.data))
        logger.debug(
            "Received block piece=%d offset=%d size=%d from %s",
            msg.piece_index, msg.block_offset, len(msg.data), self._peer_key,
        )

    # ------------------------------------------------------------------
    # Internal: Request Loop
    # ------------------------------------------------------------------

    async def _request_loop(self) -> None:
        """Periodically request new blocks from an unchoked peer."""
        while not self._closed:
            await asyncio.sleep(0.05)  # 50ms polling interval

            if self._peer_choking or not self._am_interested:
                continue
            if self.picker.is_complete:
                break

            # Refill pipeline up to MAX_PIPELINED_REQUESTS
            slots_available = MAX_PIPELINED_REQUESTS - len(self._in_flight)
            if slots_available <= 0:
                continue

            blocks = self.picker.pick_blocks(
                self.availability,
                max_blocks=slots_available,
            )

            for block in blocks:
                req = Request(
                    piece_index=block.piece_index,
                    block_offset=block.block_offset,
                    block_length=block.length,
                )
                key = (block.piece_index, block.block_offset)
                self._in_flight[key] = time.monotonic()
                await self.send_message(req)

    # ------------------------------------------------------------------
    # Internal: Keep-alive Loop
    # ------------------------------------------------------------------

    async def _keepalive_loop(self) -> None:
        """Send periodic keep-alive messages to prevent disconnection."""
        while not self._closed:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not self._closed:
                await self.send_message(KeepAlive())
                self.stats.update_rate()
                logger.debug(
                    "Peer %s: rate=%.1f KB/s blocks=%d",
                    self._peer_key,
                    self.stats.download_rate / 1024,
                    self.stats.blocks_received,
                )

    # ------------------------------------------------------------------
    # Internal: Cleanup
    # ------------------------------------------------------------------

    async def _close(self) -> None:
        """Close the TCP connection and update state."""
        if self._closed:
            return
        self._closed = True
        self.state = PeerState.CLOSED
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        logger.debug("Closed connection to %s", self._peer_key)
