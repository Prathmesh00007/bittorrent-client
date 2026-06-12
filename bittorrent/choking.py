"""
choking.py - Tit-for-Tat Choking / Unchoking Strategy
=======================================================
Implements the BitTorrent choking algorithm:

Standard algorithm (every 10 seconds):
  1. Rank all peers by download rate from us (descending).
  2. Unchoke the top N_UPLOAD_SLOTS peers ("regular unchoke").
  3. Choke all other peers.
  4. Every 3rd round (every 30 seconds): replace one unchoked slot with a
     random choked-but-interested peer ("optimistic unchoke").
     This allows new peers a chance to prove themselves.

Leecher behavior (no pieces to offer):
  - Unchoke peers who are uploading the most TO us.
  - This ensures we can get data even when we're new.

Seeder behavior (complete download):
  - Unchoke peers who are downloading the fastest from us.
  - Optimistic unchoke continues to allow new peers in.

Design:
  The ChokingManager runs as an asyncio task calling evaluate() on a fixed
  interval. It receives a snapshot of PeerStats from each active PeerConnection
  and issues Choke/Unchoke messages accordingly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING, Protocol, Optional

if TYPE_CHECKING:
    from .peer import PeerConnection

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHOKE_INTERVAL = 10.0      # seconds between regular choke evaluations
OPTIMISTIC_ROUNDS = 3      # every N rounds, perform an optimistic unchoke
N_UPLOAD_SLOTS = 4         # maximum simultaneously unchoked peers


# ---------------------------------------------------------------------------
# Peer capability interface (avoids circular imports)
# ---------------------------------------------------------------------------

class ChokablePeer(Protocol):
    """Protocol interface expected by ChokingManager from PeerConnection."""

    peer_key: str
    """Unique identifier for the peer."""

    @property
    def is_active(self) -> bool:
        """True if connection is in ACTIVE state."""
        ...

    async def send_message(self, message) -> None:
        """Send a wire protocol message to the peer."""
        ...

    @property
    def stats(self):
        """PeerStats object with download_rate and upload_rate."""
        ...


# ---------------------------------------------------------------------------
# Choking Manager
# ---------------------------------------------------------------------------

class ChokingManager:
    """Manages the choking/unchoking state for the local peer.

    Implements standard BitTorrent tit-for-tat:
    - Regular evaluation every 10 seconds
    - Optimistic unchoke every 30 seconds

    Args:
        is_seeder: True if we have completed the download (seed mode).
    """

    def __init__(self, is_seeder: bool = False) -> None:
        self._is_seeder = is_seeder
        self._unchoked: set[str] = set()    # currently unchoked peer keys
        self._optimistic: Optional[str] = None  # key of optimistically unchoked peer
        self._round = 0
        self._peers: list[ChokablePeer] = []

    def set_peers(self, peers: list[ChokablePeer]) -> None:
        """Update the current list of managed peers.

        Args:
            peers: List of active PeerConnection objects.
        """
        self._peers = [p for p in peers if p.is_active]

    async def run(self) -> None:
        """Main loop: periodically evaluate and update choke states.

        This should be run as an asyncio Task for the duration of the
        download session.
        """
        logger.info("ChokingManager started (slots=%d)", N_UPLOAD_SLOTS)
        while True:
            try:
                await asyncio.sleep(CHOKE_INTERVAL)
                await self.evaluate()
            except asyncio.CancelledError:
                logger.debug("ChokingManager cancelled")
                break
            except Exception as exc:
                logger.error("ChokingManager error: %s", exc)

    async def evaluate(self) -> None:
        """Run one round of the choking algorithm.

        Called automatically by run(), but can also be called manually.
        """
        self._round += 1
        active_peers = [p for p in self._peers if p.is_active]
        if not active_peers:
            return

        # --- Rank peers ---
        # Leecher: rank by download rate from peer (how much they upload to us)
        # Seeder:  rank by upload rate to peer (how fast we can serve them)
        if self._is_seeder:
            ranked = sorted(
                active_peers,
                key=lambda p: p.stats.upload_rate,
                reverse=True,
            )
        else:
            ranked = sorted(
                active_peers,
                key=lambda p: p.stats.download_rate,
                reverse=True,
            )

        # Select top N_UPLOAD_SLOTS peers to unchoke
        top_peers = ranked[:N_UPLOAD_SLOTS]
        top_keys = {p.peer_key for p in top_peers}

        # --- Optimistic unchoke ---
        if self._round % OPTIMISTIC_ROUNDS == 0:
            choked_candidates = [
                p for p in active_peers
                if p.peer_key not in top_keys
            ]
            if choked_candidates:
                chosen = random.choice(choked_candidates)
                self._optimistic = chosen.peer_key
                top_keys.add(chosen.peer_key)
                logger.info(
                    "Optimistic unchoke: %s", chosen.peer_key
                )
            else:
                self._optimistic = None

        # --- Apply choke/unchoke ---
        newly_unchoked = top_keys - self._unchoked
        newly_choked = self._unchoked - top_keys

        from .protocol import Unchoke as UnchokeMSG, Choke as ChokeMSG

        for peer in active_peers:
            key = peer.peer_key
            if key in newly_unchoked:
                await peer.send_message(UnchokeMSG())
                logger.debug("Unchoking peer %s", key)
            elif key in newly_choked:
                await peer.send_message(ChokeMSG())
                logger.debug("Choking peer %s", key)

        self._unchoked = top_keys

        logger.info(
            "Choke round %d: unchoked=%d total=%d",
            self._round, len(self._unchoked), len(active_peers),
        )

    @property
    def unchoked_peers(self) -> frozenset[str]:
        """Set of currently unchoked peer keys."""
        return frozenset(self._unchoked)

    def notify_download_complete(self) -> None:
        """Switch to seeder mode after download completion."""
        self._is_seeder = True
        logger.info("ChokingManager switched to seeder mode")
