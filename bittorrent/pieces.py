"""
pieces.py - Piece Picker and Swarm State Tracker
=================================================
Implements:
  - PieceState: Tracks download state of individual pieces and their blocks
  - PiecePicker: Rarest-first selection algorithm
  - PeerAvailability: Tracks which pieces each peer has

Rarest-first strategy:
  For each needed piece, count how many peers have it. Sort by availability
  (ascending), picking pieces seen by the fewest peers first. Ties broken
  randomly to spread load. This maximizes the diversity of pieces in the
  swarm and helps seed rare pieces back quickly.

Block-level parallelism:
  Each piece is divided into BLOCK_SIZE (16 KB) blocks. Multiple blocks
  from different pieces can be in-flight simultaneously to different peers.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .protocol import Request

logger = logging.getLogger(__name__)

BLOCK_SIZE = Request.STANDARD_BLOCK_SIZE  # 16,384 bytes


# ---------------------------------------------------------------------------
# Block State
# ---------------------------------------------------------------------------

class BlockState(Enum):
    """Download state of a single block."""

    FREE = auto()      # Not yet requested
    PENDING = auto()   # Requested from a peer, awaiting response
    RECEIVED = auto()  # Data received, awaiting piece verification


@dataclass
class Block:
    """Represents a single 16 KB block within a piece.

    Attributes:
        piece_index:  Parent piece index.
        block_offset: Byte offset within the piece.
        length:       Number of bytes in this block.
        state:        Current download state.
        data:         Received bytes (None until received).
        retries:      Number of times this block has been re-requested.
    """

    piece_index: int
    block_offset: int
    length: int
    state: BlockState = BlockState.FREE
    data: Optional[bytes] = None
    retries: int = 0
    MAX_RETRIES = 5


# ---------------------------------------------------------------------------
# Piece State
# ---------------------------------------------------------------------------

class PieceState(Enum):
    """Download state of a full piece."""

    NEEDED = auto()       # Not started
    IN_PROGRESS = auto()  # At least one block requested or received
    VERIFIED = auto()     # SHA-1 verified and written to disk
    FAILED = auto()       # Verification failed; will be retried


@dataclass
class Piece:
    """Tracks the download state of a single torrent piece.

    Attributes:
        index:       Zero-based piece index.
        length:      Total bytes in this piece.
        hash_:       Expected 20-byte SHA-1 digest.
        state:       Current piece state.
        blocks:      Ordered list of Block objects covering this piece.
        fail_count:  Number of SHA-1 verification failures.
    """

    index: int
    length: int
    hash_: bytes
    state: PieceState = PieceState.NEEDED
    blocks: list[Block] = field(default_factory=list)
    fail_count: int = 0

    def __post_init__(self) -> None:
        if not self.blocks:
            self._init_blocks()

    def _init_blocks(self) -> None:
        """Divide the piece into BLOCK_SIZE blocks."""
        offset = 0
        while offset < self.length:
            size = min(BLOCK_SIZE, self.length - offset)
            self.blocks.append(Block(
                piece_index=self.index,
                block_offset=offset,
                length=size,
            ))
            offset += size

    @property
    def num_blocks(self) -> int:
        return len(self.blocks)

    def get_free_blocks(self) -> list[Block]:
        """Return blocks that are FREE (not requested or received)."""
        return [b for b in self.blocks if b.state == BlockState.FREE]

    def get_pending_blocks(self) -> list[Block]:
        """Return blocks currently pending from a peer."""
        return [b for b in self.blocks if b.state == BlockState.PENDING]

    def receive_block(self, block_offset: int, data: bytes) -> bool:
        """Record received block data.

        Args:
            block_offset: Byte offset within this piece.
            data:         Raw bytes received.

        Returns:
            True if this was the last block needed (piece is complete).
        """
        for block in self.blocks:
            if block.block_offset == block_offset:
                block.data = data
                block.state = BlockState.RECEIVED
                break
        else:
            logger.warning(
                "Received unknown block offset %d for piece %d",
                block_offset, self.index
            )
            return False

        complete = all(b.state == BlockState.RECEIVED for b in self.blocks)
        if complete:
            self.state = PieceState.IN_PROGRESS
        return complete

    def assemble(self) -> bytes:
        """Concatenate all received block data into the full piece bytes.

        Returns:
            Assembled bytes for this piece.

        Raises:
            RuntimeError: If any block is not yet received.
        """
        parts = []
        for block in sorted(self.blocks, key=lambda b: b.block_offset):
            if block.data is None:
                raise RuntimeError(
                    f"Block at offset {block.block_offset} not received"
                )
            parts.append(block.data)
        return b"".join(parts)

    def reset(self) -> None:
        """Reset all blocks to FREE state for retry after a failed verification."""
        self.state = PieceState.NEEDED
        for block in self.blocks:
            block.state = BlockState.FREE
            block.data = None
        self.fail_count += 1


# ---------------------------------------------------------------------------
# Peer Availability Tracker
# ---------------------------------------------------------------------------

class PeerAvailability:
    """Tracks which pieces a single peer has available.

    Attributes:
        peer_id:      Peer identifier string.
        num_pieces:   Total number of pieces in the torrent.
        has_piece:    Boolean array indexed by piece index.
    """

    def __init__(self, peer_id: str, num_pieces: int) -> None:
        self.peer_id = peer_id
        self.num_pieces = num_pieces
        self.has_piece: list[bool] = [False] * num_pieces

    def apply_bitfield(self, bitfield_bytes: bytes) -> None:
        """Apply a Bitfield message to update availability.

        Args:
            bitfield_bytes: Raw bitfield bytes (MSB first per byte).
        """
        for i in range(self.num_pieces):
            byte_idx = i // 8
            bit_idx = 7 - (i % 8)
            if byte_idx < len(bitfield_bytes):
                self.has_piece[i] = bool(
                    bitfield_bytes[byte_idx] & (1 << bit_idx)
                )

    def set_have(self, piece_index: int) -> None:
        """Update availability from a Have message.

        Args:
            piece_index: Zero-based piece index now available.
        """
        if 0 <= piece_index < self.num_pieces:
            self.has_piece[piece_index] = True


# ---------------------------------------------------------------------------
# Piece Picker
# ---------------------------------------------------------------------------

class PiecePicker:
    """Rarest-first piece selection with end-game detection.

    The picker maintains:
    - A list of Piece objects representing torrent pieces.
    - Per-piece availability counts across all peers.
    - A set of pieces in-progress so blocks can be parallelized.

    Selection policy:
    1. Among NEEDED pieces that the target peer has, sort by rarity
       (ascending availability count), break ties randomly.
    2. Once < END_GAME_THRESHOLD pieces remain, switch to end-game:
       request all remaining blocks from all connected peers simultaneously.
    3. When a block arrives, cancel duplicates from other peers.
    """

    END_GAME_THRESHOLD = 4  # Switch to end-game when ≤ this many pieces remain

    def __init__(
        self,
        pieces: list[Piece],
        num_pieces: int,
    ) -> None:
        """Initialize the picker.

        Args:
            pieces:     Pre-constructed Piece objects (one per torrent piece).
            num_pieces: Total number of pieces.
        """
        self._pieces = pieces
        self._num_pieces = num_pieces
        # Availability count: how many connected peers have each piece
        self._availability: list[int] = [0] * num_pieces
        self._completed: set[int] = set()

    # ------------------------------------------------------------------
    # Availability Management
    # ------------------------------------------------------------------

    def peer_has_piece(self, piece_index: int) -> None:
        """Increment availability for a piece (Have or Bitfield update).

        Args:
            piece_index: Piece now known to be available on one more peer.
        """
        if 0 <= piece_index < self._num_pieces:
            self._availability[piece_index] += 1

    def peer_lost_piece(self, piece_index: int) -> None:
        """Decrement availability for all pieces when a peer disconnects.

        Args:
            piece_index: Piece index that one fewer peer has.
        """
        if 0 <= piece_index < self._num_pieces:
            self._availability[piece_index] = max(
                0, self._availability[piece_index] - 1
            )

    def peer_disconnected(self, availability: PeerAvailability) -> None:
        """Decrement availability counts for all pieces a peer had.

        Args:
            availability: Peer's PeerAvailability record.
        """
        for i, has in enumerate(availability.has_piece):
            if has:
                self.peer_lost_piece(i)

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def pick_blocks(
        self,
        peer_availability: PeerAvailability,
        max_blocks: int = 10,
    ) -> list[Block]:
        """Select the next blocks to request from a specific peer.

        Uses rarest-first ordering among pieces the peer has.

        Args:
            peer_availability: Availability record for the target peer.
            max_blocks:        Maximum number of blocks to return.

        Returns:
            List of Block objects the caller should request from this peer.
            Returns empty list if nothing is available.
        """
        selected: list[Block] = []

        # Collect candidate pieces: NEEDED or IN_PROGRESS, peer has it
        candidates = [
            self._pieces[i]
            for i in range(self._num_pieces)
            if (
                i not in self._completed
                and peer_availability.has_piece[i]
                and self._pieces[i].state in (
                    PieceState.NEEDED, PieceState.IN_PROGRESS
                )
            )
        ]

        if not candidates:
            return []

        # Check for end-game mode
        remaining = [p for p in self._pieces if p.state not in (PieceState.VERIFIED,)]
        end_game = len(remaining) <= self.END_GAME_THRESHOLD

        # Sort by rarity (ascending availability), randomize ties
        candidates.sort(
            key=lambda p: (self._availability[p.index], random.random())
        )

        for piece in candidates:
            if len(selected) >= max_blocks:
                break
            if end_game:
                # End-game: grab all free + pending blocks
                free_blocks = piece.get_free_blocks() + piece.get_pending_blocks()
            else:
                free_blocks = piece.get_free_blocks()

            for block in free_blocks:
                if len(selected) >= max_blocks:
                    break
                # Mark as pending so other pickers don't re-select
                block.state = BlockState.PENDING
                selected.append(block)
                piece.state = PieceState.IN_PROGRESS

        return selected

    # ------------------------------------------------------------------
    # Completion Tracking
    # ------------------------------------------------------------------

    def mark_verified(self, piece_index: int) -> None:
        """Mark a piece as verified and written to disk.

        Args:
            piece_index: Zero-based piece index.
        """
        self._pieces[piece_index].state = PieceState.VERIFIED
        self._completed.add(piece_index)
        logger.debug("Piece %d marked verified", piece_index)

    def mark_failed(self, piece_index: int) -> None:
        """Reset a piece after SHA-1 verification failure.

        Args:
            piece_index: Zero-based piece index.
        """
        piece = self._pieces[piece_index]
        piece.reset()
        logger.warning(
            "Piece %d failed verification (fail_count=%d), resetting",
            piece_index, piece.fail_count,
        )

    @property
    def num_completed(self) -> int:
        """Number of verified pieces."""
        return len(self._completed)

    @property
    def is_complete(self) -> bool:
        """True when all pieces have been verified."""
        return len(self._completed) == self._num_pieces

    @property
    def progress(self) -> float:
        """Download progress as a float in [0.0, 1.0]."""
        return self.num_completed / self._num_pieces if self._num_pieces else 0.0

    def get_piece(self, piece_index: int) -> Piece:
        """Access a specific Piece object.

        Args:
            piece_index: Zero-based piece index.

        Returns:
            The Piece object.
        """
        return self._pieces[piece_index]

    def expire_pending_blocks(self, timeout_retries: int = 3) -> list[Block]:
        """Reset PENDING blocks that have likely timed out.

        Called periodically to free blocks stuck in PENDING state.
        Blocks with too many retries are reset and their piece is reset.

        Args:
            timeout_retries: Max retries before block is abandoned.

        Returns:
            List of blocks that were reset to FREE.
        """
        reset_blocks: list[Block] = []
        for piece in self._pieces:
            if piece.state != PieceState.IN_PROGRESS:
                continue
            for block in piece.get_pending_blocks():
                block.retries += 1
                if block.retries >= timeout_retries:
                    block.state = BlockState.FREE
                    block.retries = 0
                    reset_blocks.append(block)
        return reset_blocks
