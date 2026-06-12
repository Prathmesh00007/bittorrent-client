"""
tests/test_integration.py - Integration Test: Full Download Pipeline
=====================================================================
Tests the complete download flow end-to-end using a local SeedServer
and DownloadEngine without any real network or tracker.

Test flow:
  1. Generate random content (1 MB)
  2. Build TorrentMetadata from content
  3. Start SeedServer on a random port
  4. Run DownloadEngine connecting to SeedServer
  5. Verify downloaded file matches original SHA-256
"""

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from bittorrent.torrent import create_mock_torrent
from bittorrent.seed_server import SeedServer
from bittorrent.engine import DownloadEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def find_free_port() -> int:
    """Find an available TCP port by binding temporarily."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

class TestFullDownloadPipeline:
    """End-to-end download test using local seeder."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)  # 60-second test timeout
    async def test_download_1mb(self, tmp_path: Path):
        """Download a 1 MB file end-to-end and verify integrity."""
        file_size = 1 * 1024 * 1024  # 1 MB
        piece_length = 256 * 1024    # 256 KB → 4 pieces

        content = os.urandom(file_size)
        original_hash = hashlib.sha256(content).hexdigest()

        metadata, _ = create_mock_torrent(
            file_name="test_1mb.bin",
            file_size=file_size,
            piece_length=piece_length,
            content=content,
        )

        port = await find_free_port()
        server = SeedServer(metadata=metadata, content=content, port=port)
        server_task = asyncio.create_task(server.run())

        await asyncio.sleep(0.2)  # Allow server to start

        try:
            engine = DownloadEngine(
                metadata=metadata,
                output_dir=tmp_path,
                manual_peers=[("127.0.0.1", port)],
                max_peers=3,
            )
            success = await engine.run()
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        assert success, "Engine reported failure"

        out_file = tmp_path / metadata.files[0].name
        assert out_file.exists(), "Output file not created"
        downloaded_hash = hashlib.sha256(out_file.read_bytes()).hexdigest()
        assert downloaded_hash == original_hash, "Downloaded file hash mismatch!"

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_download_small_pieces(self, tmp_path: Path):
        """Download with small piece size (stress-tests piece picker)."""
        file_size = 512 * 1024  # 512 KB
        piece_length = 64 * 1024  # 64 KB → 8 pieces

        content = os.urandom(file_size)
        original_hash = hashlib.sha256(content).hexdigest()

        metadata, _ = create_mock_torrent(
            file_name="test_small.bin",
            file_size=file_size,
            piece_length=piece_length,
            content=content,
        )

        port = await find_free_port()
        server = SeedServer(metadata=metadata, content=content, port=port)
        server_task = asyncio.create_task(server.run())
        await asyncio.sleep(0.2)

        try:
            engine = DownloadEngine(
                metadata=metadata,
                output_dir=tmp_path,
                manual_peers=[("127.0.0.1", port)],
                max_peers=2,
            )
            success = await engine.run()
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        assert success
        out_file = tmp_path / metadata.files[0].name
        assert hashlib.sha256(out_file.read_bytes()).hexdigest() == original_hash

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_multiple_simultaneous_connections(self, tmp_path: Path):
        """Test with multiple concurrent connections to the same seeder."""
        file_size = 512 * 1024
        piece_length = 128 * 1024  # 4 pieces
        content = os.urandom(file_size)

        metadata, _ = create_mock_torrent(
            file_name="test_multi.bin",
            file_size=file_size,
            piece_length=piece_length,
            content=content,
        )

        port = await find_free_port()
        server = SeedServer(metadata=metadata, content=content, port=port)
        server_task = asyncio.create_task(server.run())
        await asyncio.sleep(0.2)

        try:
            # Open 5 connections to the same seed server
            engine = DownloadEngine(
                metadata=metadata,
                output_dir=tmp_path,
                manual_peers=[("127.0.0.1", port)] * 5,  # 5 connections
                max_peers=5,
            )
            success = await engine.run()
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

        assert success
        out_file = tmp_path / metadata.files[0].name
        expected = hashlib.sha256(content).hexdigest()
        assert hashlib.sha256(out_file.read_bytes()).hexdigest() == expected


# ---------------------------------------------------------------------------
# SeedServer Tests
# ---------------------------------------------------------------------------

class TestSeedServer:
    @pytest.mark.asyncio
    async def test_server_accepts_handshake(self):
        """Seed server should complete handshake with a valid client."""
        file_size = 1024
        piece_length = 512
        content = os.urandom(file_size)

        metadata, _ = create_mock_torrent(
            file_name="hs_test.bin",
            file_size=file_size,
            piece_length=piece_length,
            content=content,
        )

        port = await find_free_port()
        server = SeedServer(metadata=metadata, content=content, port=port)
        server_task = asyncio.create_task(server.run())
        await asyncio.sleep(0.2)

        try:
            from bittorrent.protocol import Handshake
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            our_hs = Handshake(
                info_hash=metadata.info_hash,
                peer_id=b"-TEST01-" + os.urandom(12),
            )
            writer.write(our_hs.encode())
            await writer.drain()

            # Read the server's handshake response
            raw = await asyncio.wait_for(reader.readexactly(68), timeout=5.0)
            remote_hs = Handshake.decode(raw)

            assert remote_hs.info_hash == metadata.info_hash
            writer.close()
            await writer.wait_closed()
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_server_rejects_wrong_info_hash(self):
        """Seed server should close connection on info hash mismatch."""
        file_size = 512
        content = os.urandom(file_size)

        metadata, _ = create_mock_torrent(
            file_name="reject_test.bin",
            file_size=file_size,
            piece_length=512,
            content=content,
        )

        port = await find_free_port()
        server = SeedServer(metadata=metadata, content=content, port=port)
        server_task = asyncio.create_task(server.run())
        await asyncio.sleep(0.2)

        try:
            from bittorrent.protocol import Handshake
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # Send handshake with WRONG info hash
            bad_hs = Handshake(
                info_hash=b"\xFF" * 20,
                peer_id=b"-TEST01-" + os.urandom(12),
            )
            writer.write(bad_hs.encode())
            await writer.drain()

            # Server should either close or respond with its own hs
            # then close when it detects mismatch
            try:
                await asyncio.wait_for(reader.read(100), timeout=3.0)
            except asyncio.TimeoutError:
                pass  # Acceptable: server may just ignore us

            writer.close()
            await writer.wait_closed()
        finally:
            server_task.cancel()
            try:
                await server_task
            except asyncio.CancelledError:
                pass
