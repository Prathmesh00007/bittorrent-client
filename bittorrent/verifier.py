"""
verifier.py - SHA-1 Piece Verifier (Async Pipeline Stage)
==========================================================
The verifier is a dedicated pipeline stage that sits between the network
receive queue and the disk write queue:

  [block_queue] → PieceAssembler → SHA-1 check → [write_queue]
                                                 → PiecePicker.mark_failed()

Design:
  - Runs as an asyncio background task consuming from block_queue
  - Uses run_in_executor for CPU-bound SHA-1 hashing to avoid blocking
    the event loop (Python's hashlib releases the GIL)
  - Correctly handles partial pieces (last piece may be shorter)
  - Reports progress and verification failures via logging

Thread safety:
  All state (pieces dict, queue) is accessed only from the asyncio event
  loop, so no explicit locking is needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Optional, TYPE_CHECKING

from .pieces import PiecePicker, Piece, PieceState

if TYPE_CHECKING:
    from .torrent import TorrentMetadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Piece Verifier
# ---------------------------------------------------------------------------

class PieceVerifier:
    """Assembles received blocks into pieces and verifies SHA-1 integrity.

    Args:
        metadata:    Torrent metadata (piece hashes, sizes).
        picker:      Shared PiecePicker for marking success/failure.
        block_queue: asyncio.Queue receiving (piece_index, block_offset, data).
        write_queue: asyncio.Queue where verified pieces are forwarded for disk write.
    """

    def __init__(
        self,
        metadata: "TorrentMetadata",
        picker: PiecePicker,
        block_queue: asyncio.Queue,
        write_queue: asyncio.Queue,
    ) -> None:
        self._metadata = metadata
        self._picker = picker
        self._block_queue = block_queue
        self._write_queue = write_queue
        self._verified_count = 0
        self._failed_count = 0

    async def run(self) -> None:
        """Main verifier loop.

        Consumes blocks from block_queue, assembles pieces, verifies SHA-1,
        and forwards verified pieces to write_queue.

        Terminates when None is received on the block_queue (sentinel).
        """
        logger.info("PieceVerifier started")
        loop = asyncio.get_running_loop()

        while True:
            item = await self._block_queue.get()
            if item is None:
                # Sentinel: shutdown signal
                await self._write_queue.put(None)
                logger.info(
                    "PieceVerifier done: verified=%d failed=%d",
                    self._verified_count, self._failed_count,
                )
                break

            piece_index, block_offset, data = item
            piece = self._picker.get_piece(piece_index)

            if piece.state == PieceState.VERIFIED:
                # Duplicate block for an already-verified piece (end-game)
                self._block_queue.task_done()
                continue

            complete = piece.receive_block(block_offset, data)

            if complete:
                # All blocks received — run SHA-1 in thread pool
                raw_bytes = piece.assemble()
                valid = await loop.run_in_executor(
                    None,
                    self._verify_hash,
                    raw_bytes,
                    self._metadata.piece_hashes[piece_index],
                )

                if valid:
                    piece.state = PieceState.VERIFIED
                    self._picker.mark_verified(piece_index)
                    self._verified_count += 1
                    await self._write_queue.put((piece_index, raw_bytes))
                    logger.info(
                        "✓ Piece %d verified [%d/%d] (%.1f%%)",
                        piece_index,
                        self._verified_count,
                        self._metadata.num_pieces,
                        self._picker.progress * 100,
                    )
                else:
                    self._picker.mark_failed(piece_index)
                    self._failed_count += 1
                    logger.warning(
                        "✗ Piece %d FAILED SHA-1 (fail #%d)",
                        piece_index, piece.fail_count,
                    )

            self._block_queue.task_done()

    @staticmethod
    def _verify_hash(data: bytes, expected_hash: bytes) -> bool:
        """Compute SHA-1 of data and compare to expected hash.

        This is CPU-bound and safe to run in a thread pool since hashlib
        releases the GIL during computation.

        Args:
            data:          Raw piece bytes.
            expected_hash: 20-byte expected SHA-1 digest.

        Returns:
            True if hashes match, False otherwise.
        """
        actual = hashlib.sha1(data).digest()
        return actual == expected_hash

    @property
    def verified_count(self) -> int:
        """Number of successfully verified pieces so far."""
        return self._verified_count

    @property
    def failed_count(self) -> int:
        """Number of SHA-1 verification failures so far."""
        return self._failed_count
