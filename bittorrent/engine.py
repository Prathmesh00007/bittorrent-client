"""
engine.py - Download Engine
============================
The DownloadEngine assembles all components into a cohesive download session.

Pipeline architecture:

  [Tracker/Peers] → SwarmManager
                         ↓
                  [PeerConnection × N]
                         ↓ block_queue (asyncio.Queue)
                  [PieceVerifier]
                         ↓ write_queue (asyncio.Queue)
                  [DiskWriter]
                         ↓
                  [Output File]

All stages run concurrently as asyncio.Tasks.
The engine provides a single run() coroutine that manages the full lifecycle.

Error handling:
  - Individual peer failures are isolated (handled in PeerConnection)
  - Piece verification failures trigger re-request (in PiecePicker)
  - Disk errors are logged and propagated upward
  - Engine cancellation cleanly shuts down all stages in order
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .torrent import TorrentMetadata
from .pieces import PiecePicker, Piece, PieceState
from .tracker import PeerDiscovery, PeerAddress
from .swarm import SwarmManager
from .verifier import PieceVerifier
from .disk_writer import DiskWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Download Engine
# ---------------------------------------------------------------------------

class DownloadEngine:
    """Orchestrates the complete download pipeline.

    Wires together: Tracker → Swarm → Verifier → Disk Writer.

    Args:
        metadata:      Parsed TorrentMetadata.
        output_dir:    Directory to write the downloaded file.
        peer_id:       Our 20-byte peer identifier (generated if None).
        listen_port:   Our TCP listen port (reported to tracker).
        manual_peers:  Optional list of (host, port) tuples for direct connect.
        max_peers:     Override default MAX_PEERS limit.
    """

    BLOCK_QUEUE_SIZE = 200   # Max pending blocks in memory before back-pressure
    WRITE_QUEUE_SIZE = 50    # Max verified pieces waiting for disk write

    def __init__(
        self,
        metadata: TorrentMetadata,
        output_dir: Path,
        peer_id: Optional[bytes] = None,
        listen_port: int = 6881,
        manual_peers: Optional[list[tuple[str, int]]] = None,
        max_peers: int = 50,
    ) -> None:
        self._metadata = metadata
        self._output_dir = output_dir
        self._peer_id = peer_id or self._generate_peer_id()
        self._listen_port = listen_port
        self._manual_peers = [
            PeerAddress(host=h, port=p)
            for h, p in (manual_peers or [])
        ]
        self._max_peers = max_peers

        # Queues (created in run())
        self._block_queue: Optional[asyncio.Queue] = None
        self._write_queue: Optional[asyncio.Queue] = None

        # Components (created in run())
        self._picker: Optional[PiecePicker] = None
        self._swarm: Optional[SwarmManager] = None
        self._verifier: Optional[PieceVerifier] = None
        self._writer: Optional[DiskWriter] = None

        # Stats
        self._start_time: Optional[float] = None

    @staticmethod
    def _generate_peer_id() -> bytes:
        """Generate a random 20-byte peer ID in Azureus style.

        Format: -AG0001-<12 random bytes>
        AG = Antigravity client identifier
        """
        return b"-AG0001-" + os.urandom(12)

    async def run(self) -> bool:
        """Run the complete download session.

        Discovers peers, connects to the swarm, downloads, verifies,
        and writes to disk.

        Returns:
            True if download completed successfully, False otherwise.
        """
        self._start_time = time.monotonic()
        logger.info(
            "DownloadEngine starting: %s (%d bytes, %d pieces)",
            self._metadata.name,
            self._metadata.total_length,
            self._metadata.num_pieces,
        )
        logger.info(
            "Info hash: %s", self._metadata.info_hash.hex()
        )

        # --- Initialize queues ---
        self._block_queue = asyncio.Queue(maxsize=self.BLOCK_QUEUE_SIZE)
        self._write_queue = asyncio.Queue(maxsize=self.WRITE_QUEUE_SIZE)

        # --- Initialize piece picker ---
        pieces = [
            Piece(
                index=i,
                length=self._metadata.piece_size(i),
                hash_=self._metadata.piece_hashes[i],
            )
            for i in range(self._metadata.num_pieces)
        ]
        self._picker = PiecePicker(pieces=pieces, num_pieces=self._metadata.num_pieces)

        # --- Discover peers ---
        discovery = PeerDiscovery(
            trackers=self._metadata.trackers,
            info_hash=self._metadata.info_hash,
            peer_id=self._peer_id,
            port=self._listen_port,
            total_length=self._metadata.total_length,
            mock_peers=self._manual_peers,
        )
        peers = await discovery.discover()
        if not peers:
            logger.error("No peers discovered! Cannot download.")
            return False
        logger.info("Starting download with %d peers", len(peers))

        # --- Wire up pipeline ---
        self._swarm = SwarmManager(
            metadata=self._metadata,
            picker=self._picker,
            peer_list=peers,
            our_peer_id=self._peer_id,
            block_queue=self._block_queue,
            listen_port=self._listen_port,
        )
        self._verifier = PieceVerifier(
            metadata=self._metadata,
            picker=self._picker,
            block_queue=self._block_queue,
            write_queue=self._write_queue,
        )
        self._writer = DiskWriter(
            metadata=self._metadata,
            output_dir=self._output_dir,
            write_queue=self._write_queue,
        )

        # --- Run pipeline stages concurrently ---
        swarm_task = asyncio.create_task(self._swarm.run(), name="swarm")
        verifier_task = asyncio.create_task(self._verifier.run(), name="verifier")
        writer_task = asyncio.create_task(self._writer.run(), name="writer")

        try:
            # Wait for all stages to complete
            await asyncio.gather(swarm_task, verifier_task, writer_task)
        except Exception as exc:
            logger.error("Download pipeline error: %s", exc)
            swarm_task.cancel()
            verifier_task.cancel()
            writer_task.cancel()
            return False

        # --- Final verification ---
        elapsed = time.monotonic() - self._start_time
        speed = self._metadata.total_length / elapsed / 1024 / 1024

        logger.info(
            "Download complete in %.1fs (%.2f MB/s). Running final check...",
            elapsed, speed
        )

        ok = await self._writer.verify_final_file()
        if ok:
            logger.info(
                "✓ SUCCESS: %s downloaded to %s",
                self._metadata.name,
                self._writer.output_path,
            )
        else:
            logger.error("✗ FAILED: Final file verification failed!")

        return ok

    @property
    def progress(self) -> float:
        """Current download progress [0.0, 1.0]."""
        return self._picker.progress if self._picker else 0.0

    @property
    def peer_id(self) -> bytes:
        """Our peer identifier."""
        return self._peer_id
