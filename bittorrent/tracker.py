"""
tracker.py - Tracker Interface and Peer Discovery
==================================================
Implements:
  1. HTTP tracker client (GET request with compact peer list response)
  2. Mock tracker for local testing (in-memory peer registry)

HTTP Tracker Protocol (BEP-3):
  Client sends GET request with query params:
    info_hash, peer_id, port, uploaded, downloaded, left, compact, event

  Tracker responds with bencoded dict:
    {
      "interval": <int>,
      "peers": <compact 6-byte binary list or list of dicts>,
      "complete": <seeders>,
      "incomplete": <leechers>,
    }

  Compact peer format: 4 bytes IP + 2 bytes port (big-endian) per peer.

Mock Tracker:
  For local testing without a real tracker, MockTracker maintains an
  in-memory list of peers. Peers can self-register by calling register().
  The client calls get_peers() to retrieve the list.

Design:
  - HTTP tracker uses aiohttp for async HTTP (minimal dependency)
  - Falls back gracefully if tracker is unreachable
  - Returns a list of (host, port) tuples
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import urllib.parse
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Peer address type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeerAddress:
    """A peer's network address.

    Attributes:
        host: IP address string.
        port: TCP port number.
    """

    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


# ---------------------------------------------------------------------------
# HTTP Tracker Client
# ---------------------------------------------------------------------------

class HTTPTrackerClient:
    """Communicates with an HTTP tracker to discover peers.

    Uses urllib for HTTP requests (stdlib only, no aiohttp dependency).
    Runs in an executor to avoid blocking the event loop.

    Args:
        announce_url: The tracker's announce URL.
        info_hash:    20-byte torrent info hash.
        peer_id:      Our 20-byte peer identifier.
        port:         Our listening port.
        total_length: Total torrent size in bytes.
    """

    def __init__(
        self,
        announce_url: str,
        info_hash: bytes,
        peer_id: bytes,
        port: int,
        total_length: int,
    ) -> None:
        self._announce_url = announce_url
        self._info_hash = info_hash
        self._peer_id = peer_id
        self._port = port
        self._total_length = total_length
        self._downloaded = 0
        self._uploaded = 0

    async def announce(
        self,
        event: str = "started",  # "started", "completed", "stopped", or ""
    ) -> list[PeerAddress]:
        """Send an announce request to the tracker.

        Args:
            event: BitTorrent tracker event string.

        Returns:
            List of discovered PeerAddress objects.
            Returns empty list on failure.
        """
        params = {
            "info_hash": self._info_hash,
            "peer_id": self._peer_id,
            "port": str(self._port),
            "uploaded": str(self._uploaded),
            "downloaded": str(self._downloaded),
            "left": str(max(0, self._total_length - self._downloaded)),
            "compact": "1",
            "event": event,
        }

        url = self._build_url(params)
        logger.info("Announcing to tracker: %s (event=%s)", self._announce_url, event)

        loop = asyncio.get_running_loop()
        try:
            response_bytes = await asyncio.wait_for(
                loop.run_in_executor(None, self._http_get, url),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("Tracker announce timed out: %s", self._announce_url)
            return []
        except Exception as exc:
            logger.warning("Tracker announce failed: %s", exc)
            return []

        return self._parse_response(response_bytes)

    def _build_url(self, params: dict) -> str:
        """Build the full announce URL with query parameters.

        Info hash and peer_id must be percent-encoded preserving all bytes.
        """
        # Manual URL encoding to handle binary info_hash and peer_id
        query_parts = []
        for key, value in params.items():
            if isinstance(value, bytes):
                encoded = urllib.parse.quote(value, safe="")
                query_parts.append(f"{key}={encoded}")
            else:
                query_parts.append(f"{key}={urllib.parse.quote(str(value))}")
        return f"{self._announce_url}?{'&'.join(query_parts)}"

    @staticmethod
    def _http_get(url: str) -> bytes:
        """Perform a synchronous HTTP GET request.

        Args:
            url: Full URL with query string.

        Returns:
            Response body as bytes.

        Raises:
            Exception: On connection error or non-200 response.
        """
        import urllib.request
        with urllib.request.urlopen(url, timeout=20) as response:
            if response.status != 200:
                raise Exception(f"Tracker returned HTTP {response.status}")
            return response.read()

    def _parse_response(self, data: bytes) -> list[PeerAddress]:
        """Parse the bencoded tracker response.

        Args:
            data: Raw response bytes from the tracker.

        Returns:
            List of PeerAddress objects.
        """
        from .torrent import bdecode, BencodeDecodeError

        try:
            response = bdecode(data)
        except BencodeDecodeError as exc:
            logger.warning("Failed to decode tracker response: %s", exc)
            return []

        if b"failure reason" in response:
            reason = response[b"failure reason"].decode("utf-8", errors="replace")
            logger.warning("Tracker failure: %s", reason)
            return []

        interval = response.get(b"interval", 1800)
        logger.debug("Tracker interval: %d seconds", interval)

        peers_raw = response.get(b"peers", b"")
        peers: list[PeerAddress] = []

        if isinstance(peers_raw, bytes):
            # Compact format: 6 bytes per peer (4 IP + 2 port)
            peers = self._parse_compact_peers(peers_raw)
        elif isinstance(peers_raw, list):
            # Dict format
            for peer_dict in peers_raw:
                if isinstance(peer_dict, dict):
                    host = peer_dict.get(b"ip", b"").decode("utf-8", errors="replace")
                    port = peer_dict.get(b"port", 0)
                    if host and port:
                        peers.append(PeerAddress(host=host, port=port))

        logger.info("Tracker returned %d peers", len(peers))
        return peers

    @staticmethod
    def _parse_compact_peers(data: bytes) -> list[PeerAddress]:
        """Parse compact peer format: 6 bytes per peer.

        Args:
            data: Raw compact peer bytes.

        Returns:
            List of PeerAddress objects.
        """
        peers = []
        for i in range(0, len(data) - 5, 6):
            ip_bytes = data[i : i + 4]
            port = struct.unpack_from(">H", data, i + 4)[0]
            host = socket.inet_ntoa(ip_bytes)
            peers.append(PeerAddress(host=host, port=port))
        return peers


# ---------------------------------------------------------------------------
# Mock Tracker (for local testing)
# ---------------------------------------------------------------------------

class MockTracker:
    """In-memory peer registry for local testing without a real tracker.

    Usage in tests:
        tracker = MockTracker()
        tracker.register(PeerAddress("127.0.0.1", 6882))
        peers = await tracker.get_peers()
    """

    def __init__(self) -> None:
        self._peers: list[PeerAddress] = []

    def register(self, address: PeerAddress) -> None:
        """Register a peer with the mock tracker.

        Args:
            address: PeerAddress to add.
        """
        if address not in self._peers:
            self._peers.append(address)
            logger.debug("MockTracker: registered %s", address)

    def unregister(self, address: PeerAddress) -> None:
        """Remove a peer from the mock tracker.

        Args:
            address: PeerAddress to remove.
        """
        self._peers = [p for p in self._peers if p != address]

    async def get_peers(self) -> list[PeerAddress]:
        """Return all registered peers.

        Returns:
            Copy of the current peer list.
        """
        return list(self._peers)


# ---------------------------------------------------------------------------
# Peer Discovery Coordinator
# ---------------------------------------------------------------------------

class PeerDiscovery:
    """Coordinates peer discovery from multiple sources.

    Tries HTTP trackers in priority order, deduplicates results,
    and returns a combined list.

    Args:
        trackers:     List of HTTP tracker URLs.
        mock_peers:   Optional list of manually provided peer addresses
                      (for local testing or peer exchange).
        info_hash:    20-byte info hash.
        peer_id:      Our 20-byte peer identifier.
        port:         Our listen port.
        total_length: Total torrent size.
    """

    def __init__(
        self,
        trackers: list[str],
        info_hash: bytes,
        peer_id: bytes,
        port: int,
        total_length: int,
        mock_peers: Optional[list[PeerAddress]] = None,
    ) -> None:
        self._trackers = trackers
        self._info_hash = info_hash
        self._peer_id = peer_id
        self._port = port
        self._total_length = total_length
        self._mock_peers = mock_peers or []

    async def discover(self) -> list[PeerAddress]:
        """Discover peers from all available sources.

        Returns:
            Deduplicated list of PeerAddress objects.
        """
        all_peers: list[PeerAddress] = list(self._mock_peers)

        # Try all trackers concurrently
        tasks = [
            self._announce_to_tracker(url)
            for url in self._trackers
            if url.startswith("http")
        ]

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, list):
                    all_peers.extend(result)

        # Deduplicate
        seen: set[str] = set()
        unique_peers: list[PeerAddress] = []
        for peer in all_peers:
            key = str(peer)
            if key not in seen:
                seen.add(key)
                unique_peers.append(peer)

        logger.info("Discovered %d unique peers", len(unique_peers))
        return unique_peers

    async def _announce_to_tracker(self, url: str) -> list[PeerAddress]:
        """Announce to a single tracker and return its peer list.

        Args:
            url: Tracker announce URL.

        Returns:
            List of peers from this tracker (empty on failure).
        """
        client = HTTPTrackerClient(
            announce_url=url,
            info_hash=self._info_hash,
            peer_id=self._peer_id,
            port=self._port,
            total_length=self._total_length,
        )
        try:
            return await client.announce(event="started")
        except Exception as exc:
            logger.debug("Tracker %s failed: %s", url, exc)
            return []
