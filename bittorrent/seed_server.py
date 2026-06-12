"""
seed_server.py - Local Seeding Server for Testing
==================================================
A minimal async TCP server that acts as a BitTorrent seeder.

Purpose: Allows the client to be tested locally without real peers.
The seed server:
  1. Listens on a configurable port
  2. Accepts incoming TCP connections
  3. Performs the BitTorrent handshake
  4. Sends a complete bitfield (all pieces available)
  5. Responds to Request messages with the correct piece data
  6. Handles multiple clients concurrently (one task per connection)

This is clearly labeled as a SIMPLIFIED implementation:
  - No choking logic (always unchoked)
  - No upload rate limiting
  - No piece cache eviction
  - Single-file torrents only

Usage in tests:
    server = SeedServer(metadata=meta, content=file_bytes, port=6882)
    asyncio.create_task(server.run())
    # Now connect your client to 127.0.0.1:6882
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional, TYPE_CHECKING

from .protocol import (
    Handshake, Bitfield, Unchoke, Piece, Request, KeepAlive,
    Interested, Choke, NotInterested, Have, Cancel, MessageCodec,
    ProtocolError, MessageID,
)

if TYPE_CHECKING:
    from .torrent import TorrentMetadata

logger = logging.getLogger(__name__)


class SeedServer:
    """Async seeding server for local testing.

    Args:
        metadata: Torrent metadata (info hash, piece sizes, num pieces).
        content:  Full file content bytes to serve.
        host:     Bind address (default 127.0.0.1).
        port:     Bind port.
    """

    def __init__(
        self,
        metadata: "TorrentMetadata",
        content: bytes,
        host: str = "127.0.0.1",
        port: int = 6882,
    ) -> None:
        self._metadata = metadata
        self._content = content
        self._host = host
        self._port = port
        self._peer_id = b"-SD0001-" + os.urandom(12)  # Seeder peer ID
        self._server: Optional[asyncio.AbstractServer] = None
        self._connection_count = 0

    async def run(self) -> None:
        """Start the server and accept connections indefinitely."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info("SeedServer listening on %s:%d", addr[0], addr[1])
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the server and close all connections."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("SeedServer stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection.

        Args:
            reader: Async stream reader.
            writer: Async stream writer.
        """
        self._connection_count += 1
        peer_addr = writer.get_extra_info("peername", ("?", 0))
        client_key = f"{peer_addr[0]}:{peer_addr[1]}"
        logger.info("SeedServer: new connection from %s", client_key)

        try:
            # Handshake
            raw_hs = await asyncio.wait_for(reader.readexactly(68), timeout=10.0)
            remote_hs = Handshake.decode(raw_hs)

            if remote_hs.info_hash != self._metadata.info_hash:
                logger.warning(
                    "SeedServer: info_hash mismatch from %s", client_key
                )
                return

            our_hs = Handshake(
                info_hash=self._metadata.info_hash,
                peer_id=self._peer_id,
            )
            writer.write(our_hs.encode())
            await writer.drain()

            # Send complete bitfield
            pieces_available = [True] * self._metadata.num_pieces
            bf = Bitfield.from_bool_list(pieces_available)
            writer.write(bf.encode())

            # Send unchoke
            writer.write(Unchoke().encode())
            await writer.drain()

            logger.debug("SeedServer: sent bitfield+unchoke to %s", client_key)

            # Message loop: respond to requests
            codec = MessageCodec()
            send_lock = asyncio.Lock()

            async def send_piece(req: Request) -> None:
                """Send block data in response to a Request."""
                start = req.piece_index * self._metadata.piece_length + req.block_offset
                end = start + req.block_length
                data = self._content[start:end]
                msg = Piece(
                    piece_index=req.piece_index,
                    block_offset=req.block_offset,
                    data=data,
                )
                async with send_lock:
                    writer.write(msg.encode())
                    await writer.drain()

            while True:
                try:
                    raw = await asyncio.wait_for(reader.read(4096), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.debug("SeedServer: idle timeout for %s", client_key)
                    break

                if not raw:
                    break

                codec.feed(raw)
                try:
                    messages = codec.parse_messages()
                except ProtocolError as exc:
                    logger.warning(
                        "SeedServer protocol error from %s: %s", client_key, exc
                    )
                    break

                for msg in messages:
                    if isinstance(msg, Request):
                        asyncio.create_task(send_piece(msg))
                    elif isinstance(msg, Interested):
                        logger.debug("SeedServer: %s is interested", client_key)
                    elif isinstance(msg, KeepAlive):
                        pass
                    # Other messages (have, cancel, etc.) are silently ignored
                    # as this is a simplified seeder

        except asyncio.IncompleteReadError:
            logger.debug("SeedServer: %s disconnected during handshake", client_key)
        except Exception as exc:
            logger.warning("SeedServer error for %s: %s", client_key, exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("SeedServer: connection closed for %s", client_key)

    @property
    def connection_count(self) -> int:
        """Total number of connections accepted so far."""
        return self._connection_count

    @property
    def address(self) -> tuple[str, int]:
        """The (host, port) the server is bound to."""
        return self._host, self._port
