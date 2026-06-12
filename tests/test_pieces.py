"""
tests/test_pieces.py - Unit Tests for Piece Picker and Piece State
===================================================================
Tests rarest-first selection, end-game behavior, block tracking,
availability updates, and completion detection.
"""

import pytest
from bittorrent.pieces import (
    PiecePicker,
    PeerAvailability,
    Piece,
    Block,
    BlockState,
    PieceState,
    BLOCK_SIZE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pieces(num_pieces: int, piece_length: int = BLOCK_SIZE * 4) -> list[Piece]:
    """Create a list of Piece objects for testing."""
    return [
        Piece(
            index=i,
            length=piece_length,
            hash_=bytes(20),  # placeholder hash
        )
        for i in range(num_pieces)
    ]


def make_peer(peer_id: str, num_pieces: int, has_all: bool = False) -> PeerAvailability:
    """Create a PeerAvailability instance."""
    peer = PeerAvailability(peer_id=peer_id, num_pieces=num_pieces)
    if has_all:
        peer.has_piece = [True] * num_pieces
    return peer


# ---------------------------------------------------------------------------
# Block Initialization Tests
# ---------------------------------------------------------------------------

class TestPieceBlockInit:
    def test_single_block_piece(self):
        piece = Piece(index=0, length=BLOCK_SIZE, hash_=bytes(20))
        assert len(piece.blocks) == 1
        assert piece.blocks[0].length == BLOCK_SIZE

    def test_multi_block_piece(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 3, hash_=bytes(20))
        assert len(piece.blocks) == 3
        assert all(b.length == BLOCK_SIZE for b in piece.blocks)

    def test_partial_last_block(self):
        length = BLOCK_SIZE * 2 + 1000  # 2 full + 1 partial
        piece = Piece(index=0, length=length, hash_=bytes(20))
        assert len(piece.blocks) == 3
        assert piece.blocks[0].length == BLOCK_SIZE
        assert piece.blocks[1].length == BLOCK_SIZE
        assert piece.blocks[2].length == 1000

    def test_block_offsets_sequential(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 3, hash_=bytes(20))
        offsets = [b.block_offset for b in piece.blocks]
        assert offsets == [0, BLOCK_SIZE, BLOCK_SIZE * 2]

    def test_all_blocks_free_initially(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 2, hash_=bytes(20))
        assert all(b.state == BlockState.FREE for b in piece.blocks)


# ---------------------------------------------------------------------------
# Piece Receive and Assemble Tests
# ---------------------------------------------------------------------------

class TestPieceReceive:
    def test_receive_all_blocks(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 2, hash_=bytes(20))
        data1 = b"\xAA" * BLOCK_SIZE
        data2 = b"\xBB" * BLOCK_SIZE

        complete1 = piece.receive_block(0, data1)
        assert complete1 is False  # Not yet complete

        complete2 = piece.receive_block(BLOCK_SIZE, data2)
        assert complete2 is True   # Now complete

    def test_assemble_correct_order(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 2, hash_=bytes(20))
        data1 = b"\x01" * BLOCK_SIZE
        data2 = b"\x02" * BLOCK_SIZE

        piece.receive_block(BLOCK_SIZE, data2)  # receive out of order
        piece.receive_block(0, data1)

        assembled = piece.assemble()
        assert assembled == data1 + data2

    def test_assemble_raises_if_incomplete(self):
        piece = Piece(index=0, length=BLOCK_SIZE * 2, hash_=bytes(20))
        piece.receive_block(0, b"\x00" * BLOCK_SIZE)
        with pytest.raises(RuntimeError):
            piece.assemble()

    def test_receive_unknown_offset_returns_false(self):
        piece = Piece(index=0, length=BLOCK_SIZE, hash_=bytes(20))
        result = piece.receive_block(99999, b"\x00")
        assert result is False

    def test_reset_clears_data(self):
        piece = Piece(index=0, length=BLOCK_SIZE, hash_=bytes(20))
        piece.receive_block(0, b"\xFF" * BLOCK_SIZE)
        piece.reset()
        assert all(b.state == BlockState.FREE for b in piece.blocks)
        assert all(b.data is None for b in piece.blocks)
        assert piece.fail_count == 1


# ---------------------------------------------------------------------------
# PeerAvailability Tests
# ---------------------------------------------------------------------------

class TestPeerAvailability:
    def test_initial_all_false(self):
        peer = PeerAvailability("p1", 10)
        assert not any(peer.has_piece)

    def test_apply_bitfield_all_set(self):
        peer = PeerAvailability("p1", 8)
        peer.apply_bitfield(bytes([0xFF]))  # all 8 bits set
        assert all(peer.has_piece)

    def test_apply_bitfield_partial(self):
        peer = PeerAvailability("p1", 8)
        peer.apply_bitfield(bytes([0b10100000]))
        assert peer.has_piece[0] is True
        assert peer.has_piece[1] is False
        assert peer.has_piece[2] is True
        assert peer.has_piece[3] is False

    def test_set_have(self):
        peer = PeerAvailability("p1", 5)
        peer.set_have(3)
        assert peer.has_piece[3] is True
        assert not peer.has_piece[0]

    def test_set_have_out_of_range(self):
        peer = PeerAvailability("p1", 5)
        peer.set_have(100)  # Should not raise
        assert all(not h for h in peer.has_piece)


# ---------------------------------------------------------------------------
# PiecePicker Tests
# ---------------------------------------------------------------------------

class TestPiecePicker:
    def test_basic_pick(self):
        pieces = make_pieces(4, BLOCK_SIZE * 2)
        picker = PiecePicker(pieces=pieces, num_pieces=4)

        peer = make_peer("p1", 4, has_all=True)
        # Register availability
        for i in range(4):
            picker.peer_has_piece(i)

        blocks = picker.pick_blocks(peer, max_blocks=4)
        assert len(blocks) > 0
        assert all(isinstance(b, Block) for b in blocks)

    def test_pick_marks_blocks_pending(self):
        pieces = make_pieces(2, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=2)

        peer = make_peer("p1", 2, has_all=True)
        picker.peer_has_piece(0)
        picker.peer_has_piece(1)

        blocks = picker.pick_blocks(peer, max_blocks=5)
        for b in blocks:
            assert b.state == BlockState.PENDING

    def test_no_picks_if_peer_has_nothing(self):
        pieces = make_pieces(3, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=3)
        peer = PeerAvailability("p1", 3)  # Has no pieces

        blocks = picker.pick_blocks(peer, max_blocks=10)
        assert blocks == []

    def test_rarest_first_ordering(self):
        """Picker should prefer pieces with lower availability counts."""
        pieces = make_pieces(3, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=3)

        # Piece 0: 3 peers, Piece 1: 1 peer (rarest), Piece 2: 2 peers
        picker.peer_has_piece(0)
        picker.peer_has_piece(0)
        picker.peer_has_piece(0)
        picker.peer_has_piece(1)       # rarest
        picker.peer_has_piece(2)
        picker.peer_has_piece(2)

        peer = make_peer("p1", 3, has_all=True)
        # Request just 1 block — should pick from piece 1 (rarest)
        blocks = picker.pick_blocks(peer, max_blocks=1)
        assert len(blocks) == 1
        assert blocks[0].piece_index == 1

    def test_mark_verified(self):
        pieces = make_pieces(3, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=3)
        picker.mark_verified(1)
        assert 1 in {i for i in range(3) if pieces[i].state == PieceState.VERIFIED}
        assert picker.num_completed == 1

    def test_is_complete_when_all_verified(self):
        pieces = make_pieces(3, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=3)
        for i in range(3):
            picker.mark_verified(i)
        assert picker.is_complete

    def test_progress(self):
        pieces = make_pieces(4, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=4)
        assert picker.progress == 0.0
        picker.mark_verified(0)
        assert picker.progress == 0.25
        picker.mark_verified(1)
        assert picker.progress == 0.5

    def test_mark_failed_resets_piece(self):
        pieces = make_pieces(2, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=2)
        # Simulate a piece being in-progress
        pieces[0].state = PieceState.IN_PROGRESS
        pieces[0].blocks[0].state = BlockState.RECEIVED
        pieces[0].blocks[0].data = b"\x00" * BLOCK_SIZE
        picker.mark_failed(0)
        assert pieces[0].state == PieceState.NEEDED
        assert pieces[0].fail_count == 1

    def test_peer_disconnected_decrements_availability(self):
        pieces = make_pieces(3, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=3)

        peer = PeerAvailability("p1", 3)
        peer.has_piece = [True, True, False]
        picker.peer_has_piece(0)
        picker.peer_has_piece(1)

        picker.peer_disconnected(peer)
        # After disconnect, piece 0 and 1 availability should be 0
        assert picker._availability[0] == 0
        assert picker._availability[1] == 0

    def test_expire_pending_blocks(self):
        pieces = make_pieces(1, BLOCK_SIZE * 2)
        picker = PiecePicker(pieces=pieces, num_pieces=1)

        peer = make_peer("p1", 1, has_all=True)
        picker.peer_has_piece(0)

        # Get blocks (they become PENDING)
        blocks = picker.pick_blocks(peer, max_blocks=5)
        assert all(b.state == BlockState.PENDING for b in blocks)

        # Manually set retry count to max to trigger expiry
        for b in blocks:
            b.retries = Block.MAX_RETRIES

        # Expire
        expired = picker.expire_pending_blocks(timeout_retries=Block.MAX_RETRIES)
        assert len(expired) == len(blocks)
        assert all(b.state == BlockState.FREE for b in expired)

    def test_completed_piece_not_re_picked(self):
        pieces = make_pieces(2, BLOCK_SIZE)
        picker = PiecePicker(pieces=pieces, num_pieces=2)
        picker.peer_has_piece(0)
        picker.peer_has_piece(1)
        picker.mark_verified(0)

        peer = make_peer("p1", 2, has_all=True)
        blocks = picker.pick_blocks(peer, max_blocks=10)
        # Only piece 1 should be picked (piece 0 is verified)
        assert all(b.piece_index == 1 for b in blocks)
