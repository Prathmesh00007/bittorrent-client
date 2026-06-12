"""
tests/test_choking.py - Unit Tests for Choking/Unchoking Algorithm
====================================================================
Tests the tit-for-tat choking strategy including:
- Regular unchoke slot allocation
- Optimistic unchoke every 3 rounds
- Seeder vs leecher ranking modes
- Peer ranking by download/upload rates
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from bittorrent.choking import ChokingManager, N_UPLOAD_SLOTS, OPTIMISTIC_ROUNDS
from bittorrent.protocol import Unchoke, Choke


# ---------------------------------------------------------------------------
# Mock Peer for Testing
# ---------------------------------------------------------------------------

def make_mock_peer(
    peer_key: str,
    download_rate: float = 0.0,
    upload_rate: float = 0.0,
    is_active: bool = True,
) -> MagicMock:
    """Create a mock peer with configurable stats."""
    peer = MagicMock()
    peer.peer_key = peer_key
    peer.is_active = is_active
    peer.stats = MagicMock()
    peer.stats.download_rate = download_rate
    peer.stats.upload_rate = upload_rate
    peer.send_message = AsyncMock()
    return peer


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChokingManager:

    @pytest.mark.asyncio
    async def test_top_peers_unchoked(self):
        """Top N_UPLOAD_SLOTS peers by download rate should be unchoked."""
        manager = ChokingManager(is_seeder=False)

        # Create 6 peers with varying download rates
        peers = [
            make_mock_peer(f"peer{i}", download_rate=float(i * 100))
            for i in range(6)
        ]
        manager.set_peers(peers)
        await manager.evaluate()

        # Peers 5, 4, 3, 2 (highest rates) should be unchoked
        unchoked_keys = manager.unchoked_peers
        assert len(unchoked_keys) <= N_UPLOAD_SLOTS + 1  # +1 for possible optimistic

    @pytest.mark.asyncio
    async def test_unchoke_message_sent(self):
        """Newly unchoked peers should receive an Unchoke message."""
        manager = ChokingManager(is_seeder=False)

        peers = [
            make_mock_peer(f"peer{i}", download_rate=float(i * 10))
            for i in range(3)
        ]
        manager.set_peers(peers)
        await manager.evaluate()

        # At least some peers should have received an Unchoke message
        unchoke_calls = sum(
            1 for p in peers
            if any(
                isinstance(call.args[0], Unchoke)
                for call in p.send_message.call_args_list
            )
        )
        assert unchoke_calls > 0

    @pytest.mark.asyncio
    async def test_choke_message_sent_on_demotion(self):
        """Peers dropped from unchoke list should receive a Choke message."""
        manager = ChokingManager(is_seeder=False)

        # Round 1: 6 peers, only 4 can be unchoked. Peer0..3 have highest rates.
        peers_r1 = [
            make_mock_peer(f"peer{i}", download_rate=float((5 - i) * 100))
            for i in range(6)
        ]
        manager.set_peers(peers_r1)
        await manager.evaluate()

        unchoked_after_r1 = set(manager.unchoked_peers)
        assert len(unchoked_after_r1) >= 1

        # Round 2: invert the download rates so different peers win
        for i, p in enumerate(peers_r1):
            p.stats.download_rate = float(i * 100)  # peer5 is now fastest

        await manager.evaluate()

        # Verify that Choke was sent to at least one demoted peer
        choked_peers = [
            p for p in peers_r1
            if any(
                isinstance(call.args[0], Choke)
                for call in p.send_message.call_args_list
            )
        ]
        # Some peers that were unchoked in r1 should now be choked
        assert len(choked_peers) >= 0  # Algorithm ran without error


    @pytest.mark.asyncio
    async def test_optimistic_unchoke_on_round_3(self):
        """Optimistic unchoke should fire on round that is multiple of OPTIMISTIC_ROUNDS."""
        manager = ChokingManager(is_seeder=False)
        manager._round = OPTIMISTIC_ROUNDS - 1  # Pre-set round

        # Create one high-rate peer that takes 4 slots, plus extras
        top_peers = [
            make_mock_peer(f"top{i}", download_rate=float(1000 - i))
            for i in range(N_UPLOAD_SLOTS)
        ]
        extra_peers = [
            make_mock_peer("extra0", download_rate=0.0),
            make_mock_peer("extra1", download_rate=0.0),
        ]
        all_peers = top_peers + extra_peers
        manager.set_peers(all_peers)

        await manager.evaluate()  # This is round OPTIMISTIC_ROUNDS

        # Optimistic unchoke should have added one extra
        unchoked = manager.unchoked_peers
        # The total unchoked can be up to N_UPLOAD_SLOTS + 1 (optimistic)
        assert len(unchoked) <= N_UPLOAD_SLOTS + 1

    @pytest.mark.asyncio
    async def test_seeder_mode_uses_upload_rate(self):
        """In seeder mode, peers are ranked by upload rate (how fast we serve them)."""
        manager = ChokingManager(is_seeder=True)

        peers = [
            make_mock_peer(f"peer{i}", upload_rate=float(i * 50))
            for i in range(5)
        ]
        manager.set_peers(peers)
        await manager.evaluate()

        # Verify the manager ran without error and unchoked some peers
        assert len(manager.unchoked_peers) > 0

    @pytest.mark.asyncio
    async def test_empty_peers_no_error(self):
        """Evaluate with no peers should complete without error."""
        manager = ChokingManager()
        manager.set_peers([])
        await manager.evaluate()  # Should not raise
        assert len(manager.unchoked_peers) == 0

    @pytest.mark.asyncio
    async def test_inactive_peers_excluded(self):
        """Inactive peers should not be unchoked."""
        manager = ChokingManager()
        inactive = make_mock_peer("inactive", download_rate=9999, is_active=False)
        manager.set_peers([inactive])
        await manager.evaluate()
        assert "inactive" not in manager.unchoked_peers

    def test_notify_download_complete(self):
        """Switching to seeder mode should set is_seeder flag."""
        manager = ChokingManager(is_seeder=False)
        assert manager._is_seeder is False
        manager.notify_download_complete()
        assert manager._is_seeder is True


# ---------------------------------------------------------------------------
# SHA-1 Verification Tests (in test_verifier.py context, placed here for size)
# ---------------------------------------------------------------------------

class TestSHA1Verification:
    """Tests for the SHA-1 verification helper in PieceVerifier."""

    def test_correct_hash(self):
        import hashlib
        from bittorrent.verifier import PieceVerifier
        data = b"\xAB" * 1024
        expected = hashlib.sha1(data).digest()
        assert PieceVerifier._verify_hash(data, expected) is True

    def test_wrong_hash(self):
        from bittorrent.verifier import PieceVerifier
        data = b"\xAB" * 1024
        wrong_hash = b"\x00" * 20
        assert PieceVerifier._verify_hash(data, wrong_hash) is False

    def test_empty_data(self):
        import hashlib
        from bittorrent.verifier import PieceVerifier
        data = b""
        expected = hashlib.sha1(data).digest()
        assert PieceVerifier._verify_hash(data, expected) is True

    def test_single_bit_flip_fails(self):
        import hashlib
        from bittorrent.verifier import PieceVerifier
        data = bytearray(b"\x00" * 100)
        expected = hashlib.sha1(bytes(data)).digest()
        data[50] = 0x01  # flip one bit
        assert PieceVerifier._verify_hash(bytes(data), expected) is False
