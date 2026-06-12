"""
swarm.py - Swarm Manager
=========================
The SwarmManager is the central coordinator of the BitTorrent client.

Responsibilities:
  1. Maintain a pool of up to MAX_PEERS concurrent PeerConnection tasks
  2. Monitor task health and restart failed connections
  3. Feed the ChokingManager with peer references
  4. Expire timed-out pending blocks
  5. Emit progress reports at regular intervals
  6. Signal completion to downstream pipeline stages

Concurrency model:
  - Each peer gets one asyncio.Task running PeerConnection.run()
  - SwarmManager itself runs as an asyncio.Task
  - All communication with pipeline stages is via asyncio.Queue
  - No thread synchronization needed (single event loop)

Connection management:
  - Maintains a max of MAX_PEERS simultaneous connections
  - Drains cancelled tasks and replaces with new peers
  - Skips peers that have been attempted recently (backoff list)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional, TYPE_CHECKING

from .peer import PeerConnection
from .pieces import PiecePicker, Piece, PieceState
from .choking import ChokingManager
from .tracker import PeerAddress

if TYPE_CHECKING:
    from .torrent import TorrentMetadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_PEERS = 50                  # Maximum simultaneous connections
PROGRESS_INTERVAL = 5.0         # Seconds between progress log lines
BLOCK_EXPIRE_INTERVAL = 30.0    # Seconds between pending block expiry sweeps
PEER_BACKOFF_SECONDS = 60.0     # Don't retry a failed peer within this window
MIN_PEERS_THRESHOLD = 5         # Try to maintain at least this many peers


# ---------------------------------------------------------------------------
# Swarm Manager
# ---------------------------------------------------------------------------

class SwarmManager:
    """Manages the full swarm of peer connections.

    Orchestrates peer lifecycle, choking, and progress reporting.

    Args:
        metadata:     Torrent metadata.
        picker:       Shared PiecePicker instance.
        peer_list:    Initial list of PeerAddress to connect to.
        our_peer_id:  Our 20-byte peer identifier.
        block_queue:  Queue for block data → PieceVerifier.
        listen_port:  Our local listen port (reported to tracker).
    """

    def __init__(
        self,
        metadata: "TorrentMetadata",
        picker: PiecePicker,
        peer_list: list[PeerAddress],
        our_peer_id: bytes,
        block_queue: asyncio.Queue,
        listen_port: int = 6881,
    ) -> None:
        self._metadata = metadata
        self._picker = picker
        self._peer_list = list(peer_list)
        self._our_peer_id = our_peer_id
        self._block_queue = block_queue
        self._listen_port = listen_port

        # Active peer tasks: peer_key → (Task, PeerConnection)
        self._active: dict[str, tuple[asyncio.Task, PeerConnection]] = {}

        # Backoff tracker: peer_key → timestamp of last failure
        self._backoff: dict[str, float] = {}

        # Choking manager
        self._choking = ChokingManager()

        # Completion event
        self._done_event = asyncio.Event()
        self._start_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main swarm management loop.

        Starts peer connections, runs the choking manager, monitors
        progress, and expires pending blocks. Returns when all pieces
        are verified.
        """
        self._start_time = time.monotonic()
        logger.info(
            "SwarmManager started: %d peers available, %d pieces needed",
            len(self._peer_list),
            self._metadata.num_pieces,
        )

        # Start background tasks
        choking_task = asyncio.create_task(self._choking.run(), name="choking")
        expire_task = asyncio.create_task(self._expire_loop(), name="expire")
        progress_task = asyncio.create_task(self._progress_loop(), name="progress")

        try:
            while not self._picker.is_complete:
                # Fill peer slots
                await self._fill_peer_slots()

                # Update choking manager with current peers
                self._choking.set_peers(
                    [conn for _, conn in self._active.values()]
                )

                # Prune dead tasks
                self._prune_dead_tasks()

                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            logger.info("SwarmManager cancelled")
        finally:
            # Cancel all peer tasks
            for task, conn in list(self._active.values()):
                task.cancel()
            await asyncio.gather(
                *[t for t, _ in self._active.values()],
                return_exceptions=True,
            )

            # Cancel background tasks
            choking_task.cancel()
            expire_task.cancel()
            progress_task.cancel()
            await asyncio.gather(
                choking_task, expire_task, progress_task,
                return_exceptions=True,
            )

            # Signal pipeline completion
            await self._block_queue.put(None)
            self._done_event.set()
            logger.info("SwarmManager shutdown complete")

    async def wait_complete(self) -> None:
        """Wait until all pieces have been verified."""
        await self._done_event.wait()

    # ------------------------------------------------------------------
    # Internal: Peer slot management
    # ------------------------------------------------------------------

    async def _fill_peer_slots(self) -> None:
        """Connect to new peers until MAX_PEERS slots are filled."""
        now = time.monotonic()
        slots_available = MAX_PEERS - len(self._active)

        if slots_available <= 0:
            return

        candidates = [
            addr for addr in self._peer_list
            if (
                str(addr) not in self._active
                and (
                    str(addr) not in self._backoff
                    or now - self._backoff[str(addr)] > PEER_BACKOFF_SECONDS
                )
            )
        ]

        for addr in candidates[:slots_available]:
            if len(self._active) >= MAX_PEERS:
                break
            await self._connect_peer(addr)

    async def _connect_peer(self, addr: PeerAddress) -> None:
        """Spawn an asyncio.Task for a new PeerConnection.

        Args:
            addr: Peer address to connect to.
        """
        conn = PeerConnection(
            host=addr.host,
            port=addr.port,
            metadata=self._metadata,
            our_peer_id=self._our_peer_id,
            picker=self._picker,
            block_queue=self._block_queue,
        )
        key = str(addr)
        task = asyncio.create_task(conn.run(), name=f"peer-{key}")
        self._active[key] = (task, conn)
        logger.debug("Started peer task for %s", key)

    def _prune_dead_tasks(self) -> None:
        """Remove completed or cancelled peer tasks from active dict."""
        dead_keys = [
            key for key, (task, _) in self._active.items()
            if task.done()
        ]
        for key in dead_keys:
            task, _ = self._active.pop(key)
            exc = task.exception() if not task.cancelled() else None
            if exc:
                logger.debug("Peer %s failed: %s", key, exc)
                self._backoff[key] = time.monotonic()
            else:
                logger.debug("Peer %s task complete", key)

    # ------------------------------------------------------------------
    # Internal: Background loops
    # ------------------------------------------------------------------

    async def _expire_loop(self) -> None:
        """Periodically expire timed-out pending blocks."""
        while True:
            await asyncio.sleep(BLOCK_EXPIRE_INTERVAL)
            expired = self._picker.expire_pending_blocks()
            if expired:
                logger.debug(
                    "Expired %d timed-out pending blocks", len(expired)
                )

    async def _progress_loop(self) -> None:
        """Log download progress every PROGRESS_INTERVAL seconds."""
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            elapsed = time.monotonic() - (self._start_time or time.monotonic())
            completed = self._picker.num_completed
            total = self._metadata.num_pieces
            percent = self._picker.progress * 100

            # Estimate download speed from bytes verified
            bytes_done = completed * self._metadata.piece_length
            speed_mbps = (bytes_done / elapsed / 1024 / 1024) if elapsed > 0 else 0

            logger.info(
                "Progress: %d/%d pieces (%.1f%%) | Speed: %.2f MB/s | Peers: %d | Time: %.0fs",
                completed, total, percent,
                speed_mbps,
                len(self._active),
                elapsed,
            )

    @property
    def active_peer_count(self) -> int:
        """Number of currently active peer connections."""
        return len(self._active)
