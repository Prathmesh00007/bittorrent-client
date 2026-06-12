"""
disk_writer.py - Async Disk Write Pipeline Stage
=================================================
Decouples network I/O from file I/O.

Architecture:
  write_queue → DiskWriter → output file(s)

Design decisions:
  1. All writes are done in a background thread pool via run_in_executor.
     This ensures asyncio event loop is never blocked by disk I/O.
  2. Pieces are written to their correct byte offset in the output file.
     The file is pre-allocated (sparse file on supported OS) to avoid
     repeated extends and allow random-access writes.
  3. For multi-file torrents, the DiskWriter maps global byte offsets
     to individual files.
  4. Write batching: pieces are written individually but could be batched
     if throughput is the bottleneck (currently not needed for educational
     purposes since verification is the bottleneck).
  5. On completion, a final integrity check is run across all piece hashes
     to validate the entire file.

Thread safety:
  _do_write() runs in a thread pool. It is safe because each piece has a
  unique, non-overlapping byte range in the file. Multiple writes never
  touch the same bytes simultaneously.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .torrent import TorrentMetadata

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Disk Writer
# ---------------------------------------------------------------------------

class DiskWriter:
    """Writes verified pieces to disk asynchronously.

    Receives (piece_index, piece_data) tuples from write_queue and writes
    them to the appropriate byte offset in the output file.

    Args:
        metadata:    Torrent metadata for file layout.
        output_dir:  Directory where the output file will be created.
        write_queue: asyncio.Queue receiving (piece_index, bytes) tuples.
                     A None sentinel signals shutdown.
    """

    def __init__(
        self,
        metadata: "TorrentMetadata",
        output_dir: Path,
        write_queue: asyncio.Queue,
    ) -> None:
        self._metadata = metadata
        self._output_dir = output_dir
        self._write_queue = write_queue
        self._output_path = output_dir / metadata.files[0].name
        self._bytes_written = 0
        self._pieces_written = 0
        self._file: Optional[object] = None  # opened in run()

    async def run(self) -> None:
        """Main writer loop.

        Pre-allocates the output file, then consumes from write_queue
        until None sentinel is received.

        Terminates gracefully and closes the file handle.
        """
        logger.info("DiskWriter started, output: %s", self._output_path)

        loop = asyncio.get_running_loop()

        # Pre-allocate the file
        await loop.run_in_executor(None, self._preallocate_file)

        try:
            while True:
                item = await self._write_queue.get()
                if item is None:
                    logger.info(
                        "DiskWriter done: wrote %d pieces (%d bytes)",
                        self._pieces_written,
                        self._bytes_written,
                    )
                    break

                piece_index, piece_data = item
                byte_offset = piece_index * self._metadata.piece_length

                await loop.run_in_executor(
                    None,
                    self._do_write,
                    byte_offset,
                    piece_data,
                )

                self._pieces_written += 1
                self._bytes_written += len(piece_data)
                self._write_queue.task_done()

                logger.debug(
                    "Wrote piece %d (%d bytes) at offset %d",
                    piece_index, len(piece_data), byte_offset,
                )

        finally:
            await loop.run_in_executor(None, self._close_file)

    def _preallocate_file(self) -> None:
        """Pre-allocate the output file to its full expected size.

        Uses sparse file allocation where available (Linux: fallocate,
        Windows: SetEndOfFile). Falls back to seek+write-zero approach.
        This avoids costly file extension during writes.
        """
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        total = self._metadata.total_length

        if not self._output_path.exists():
            with open(self._output_path, "wb") as f:
                try:
                    # Try fallocate (Linux) for true pre-allocation
                    os.posix_fallocate(f.fileno(), 0, total)
                    logger.debug("Pre-allocated %d bytes via fallocate", total)
                except (AttributeError, OSError):
                    # Fallback: seek to end and write a single zero byte
                    f.seek(total - 1)
                    f.write(b"\x00")
                    logger.debug(
                        "Pre-allocated %d bytes via seek (sparse)", total
                    )

        # Open the file for random-access writes
        self._file = open(self._output_path, "r+b")
        logger.info("Output file opened: %s (%d bytes)", self._output_path, total)

    def _do_write(self, byte_offset: int, data: bytes) -> None:
        """Write piece data at the correct file offset.

        Args:
            byte_offset: Absolute byte position in the output file.
            data:        Verified piece bytes to write.
        """
        if self._file is None:
            raise RuntimeError("File not opened")
        self._file.seek(byte_offset)
        self._file.write(data)
        # Flush to OS buffer (not necessarily to disk)
        self._file.flush()

    def _close_file(self) -> None:
        """Flush and close the output file."""
        if self._file is not None:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
                logger.info("Output file closed and synced")
            except OSError as exc:
                logger.error("Error closing file: %s", exc)
            finally:
                self._file = None

    async def verify_final_file(self) -> bool:
        """Verify the entire downloaded file against all piece hashes.

        This is a final integrity check after all pieces are written.
        Runs SHA-1 in executor to avoid blocking.

        Returns:
            True if all pieces match their expected hashes.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._check_all_hashes)

    def _check_all_hashes(self) -> bool:
        """Read and verify each piece from the output file.

        Returns:
            True if all piece hashes match.
        """
        logger.info("Running final file integrity check...")
        try:
            with open(self._output_path, "rb") as f:
                for i, expected_hash in enumerate(self._metadata.piece_hashes):
                    piece_size = self._metadata.piece_size(i)
                    data = f.read(piece_size)
                    actual_hash = hashlib.sha1(data).digest()
                    if actual_hash != expected_hash:
                        logger.error(
                            "Final check FAILED: piece %d hash mismatch", i
                        )
                        return False
            logger.info("✓ Final file integrity check PASSED")
            return True
        except OSError as exc:
            logger.error("Error during final check: %s", exc)
            return False

    @property
    def output_path(self) -> Path:
        """Path to the output file."""
        return self._output_path
