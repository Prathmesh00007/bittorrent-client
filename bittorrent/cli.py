"""
cli.py - Command-Line Interface Entry Point
===========================================
Provides a clean CLI using argparse for all client operations:

  bittorrent download   - Download from a .torrent file
  bittorrent seed       - Serve a local file as a seed (for testing)
  bittorrent mock       - Run a self-contained local test (no real torrents needed)

Logging is configured here with structured format and configurable level.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

def configure_logging(level: str = "INFO") -> None:
    """Configure application-wide logging.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Silence overly verbose asyncio debug output unless DEBUG requested
    if numeric_level > logging.DEBUG:
        logging.getLogger("asyncio").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Command: download
# ---------------------------------------------------------------------------

async def cmd_download(args: argparse.Namespace) -> int:
    """Download a file from a .torrent file.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success, 1 = failure).
    """
    from .torrent import TorrentParser
    from .engine import DownloadEngine

    torrent_path = Path(args.torrent)
    if not torrent_path.exists():
        print(f"Error: Torrent file not found: {torrent_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    parser = TorrentParser()
    metadata = parser.parse_file(torrent_path)

    # Parse manual peers if provided
    manual_peers = []
    if args.peers:
        for peer_str in args.peers:
            try:
                host, port_str = peer_str.rsplit(":", 1)
                manual_peers.append((host, int(port_str)))
            except ValueError:
                print(f"Warning: Invalid peer address: {peer_str}", file=sys.stderr)

    engine = DownloadEngine(
        metadata=metadata,
        output_dir=output_dir,
        listen_port=args.port,
        manual_peers=manual_peers if manual_peers else None,
        max_peers=args.max_peers,
    )

    success = await engine.run()
    return 0 if success else 1


# ---------------------------------------------------------------------------
# Command: seed
# ---------------------------------------------------------------------------

async def cmd_seed(args: argparse.Namespace) -> int:
    """Serve a local file as a BitTorrent seeder.

    SIMPLIFIED: Builds metadata from the file and serves it over TCP.
    Real clients can connect and download from it.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code.
    """
    from .torrent import create_mock_torrent
    from .seed_server import SeedServer

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File not found: {file_path}", file=sys.stderr)
        return 1

    content = file_path.read_bytes()
    metadata, _ = create_mock_torrent(
        file_name=file_path.name,
        file_size=len(content),
        piece_length=args.piece_length * 1024,
        content=content,
    )

    print(f"Serving: {file_path.name} ({len(content):,} bytes)")
    print(f"Info hash: {metadata.info_hash.hex()}")
    print(f"Pieces: {metadata.num_pieces}")
    print(f"Listening on port {args.port}")
    print("Press Ctrl+C to stop.")

    server = SeedServer(
        metadata=metadata,
        content=content,
        host="0.0.0.0",
        port=args.port,
    )

    try:
        await server.run()
    except KeyboardInterrupt:
        await server.stop()
    return 0


# ---------------------------------------------------------------------------
# Command: mock (self-contained local test)
# ---------------------------------------------------------------------------

async def cmd_mock(args: argparse.Namespace) -> int:
    """Run a complete self-contained download test using local seeder.

    Steps:
      1. Generate random file content
      2. Build torrent metadata from content
      3. Start a local SeedServer
      4. Run DownloadEngine connecting to the local server
      5. Verify downloaded file matches original
      6. Report results

    Args:
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success).
    """
    import hashlib
    from .torrent import create_mock_torrent
    from .seed_server import SeedServer
    from .engine import DownloadEngine

    file_size = args.size * 1024 * 1024  # MB to bytes
    piece_length = args.piece_length * 1024  # KB to bytes
    seed_port = args.seed_port
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print("  BitTorrent Client - Mock Local Test")
    print(f"{'='*60}")
    print(f"  File size:    {args.size} MB")
    print(f"  Piece size:   {args.piece_length} KB")
    print(f"  Seed port:    {seed_port}")
    print(f"  Output:       {output_dir}")
    print(f"{'='*60}\n")

    # Generate test content
    print("[1/4] Generating random file content...")
    content = os.urandom(file_size)
    original_hash = hashlib.sha256(content).hexdigest()
    print(f"      SHA-256 of original: {original_hash[:16]}...")

    # Build metadata
    print("[2/4] Building torrent metadata...")
    metadata, _ = create_mock_torrent(
        file_name="mock_test_file.bin",
        file_size=file_size,
        piece_length=piece_length,
        tracker_url="http://localhost:6969/announce",
        content=content,
    )
    print(f"      Info hash: {metadata.info_hash.hex()[:16]}...")
    print(f"      Pieces: {metadata.num_pieces}")

    # Start seed server
    print(f"[3/4] Starting seed server on port {seed_port}...")
    server = SeedServer(
        metadata=metadata,
        content=content,
        host="127.0.0.1",
        port=seed_port,
    )
    server_task = asyncio.create_task(server.run(), name="seed-server")

    # Give server a moment to bind
    await asyncio.sleep(0.3)

    # Run download engine
    print("[4/4] Starting download engine...")
    engine = DownloadEngine(
        metadata=metadata,
        output_dir=output_dir,
        manual_peers=[("127.0.0.1", seed_port)],
        max_peers=args.connections,
    )

    try:
        success = await engine.run()
    finally:
        server_task.cancel()
        try:
            await server_task
        except asyncio.CancelledError:
            pass

    if success:
        # Verify output matches original
        out_path = output_dir / metadata.files[0].name
        downloaded_hash = hashlib.sha256(out_path.read_bytes()).hexdigest()
        if downloaded_hash == original_hash:
            print(f"\n✓ PERFECT MATCH! SHA-256: {downloaded_hash[:16]}...")
            print("  Test PASSED — file integrity verified end-to-end.")
            return 0
        else:
            print(f"\n✗ HASH MISMATCH!")
            print(f"  Expected: {original_hash[:16]}...")
            print(f"  Got:      {downloaded_hash[:16]}...")
            return 1
    else:
        print("\n✗ Download FAILED.")
        return 1


# ---------------------------------------------------------------------------
# Argument Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="bittorrent",
        description="Educational BitTorrent Client — async Python implementation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download a .torrent file:
  python -m bittorrent download ubuntu.torrent --output ./downloads

  # Connect to specific peers:
  python -m bittorrent download file.torrent --peers 192.168.1.10:6881 --output ./out

  # Run a self-contained local test (1 MB file):
  python -m bittorrent mock --size 1 --piece-length 256 --output ./test_out

  # Serve a local file as a seeder:
  python -m bittorrent seed myfile.bin --port 6882

  # Run with verbose logging:
  python -m bittorrent --log-level DEBUG mock --size 2
        """,
    )

    # Global options
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity (default: INFO)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- download ---
    dl_parser = subparsers.add_parser(
        "download",
        help="Download a file from a .torrent",
        description="Download a file from a .torrent metadata file.",
    )
    dl_parser.add_argument("torrent", help="Path to .torrent file")
    dl_parser.add_argument(
        "--output", "-o",
        default="./downloads",
        help="Output directory (default: ./downloads)",
    )
    dl_parser.add_argument(
        "--port", "-p",
        type=int, default=6881,
        help="Our listen port (default: 6881)",
    )
    dl_parser.add_argument(
        "--peers",
        nargs="+",
        metavar="HOST:PORT",
        help="Manual peer addresses (e.g. 192.168.1.10:6881)",
    )
    dl_parser.add_argument(
        "--max-peers",
        type=int, default=50,
        help="Max concurrent peer connections (default: 50)",
    )

    # --- seed ---
    seed_parser = subparsers.add_parser(
        "seed",
        help="Serve a local file as a seeder",
        description="Serve a local file to other BitTorrent clients.",
    )
    seed_parser.add_argument("file", help="Path to file to serve")
    seed_parser.add_argument(
        "--port", "-p",
        type=int, default=6882,
        help="Listen port (default: 6882)",
    )
    seed_parser.add_argument(
        "--piece-length",
        type=int, default=512,
        metavar="KB",
        help="Piece length in KB (default: 512)",
    )

    # --- mock ---
    mock_parser = subparsers.add_parser(
        "mock",
        help="Run a self-contained local download test",
        description="Generate random content, seed it locally, and download it.",
    )
    mock_parser.add_argument(
        "--size",
        type=int, default=10,
        metavar="MB",
        help="File size in MB (default: 10)",
    )
    mock_parser.add_argument(
        "--piece-length",
        type=int, default=512,
        metavar="KB",
        help="Piece length in KB (default: 512)",
    )
    mock_parser.add_argument(
        "--seed-port",
        type=int, default=16882,
        help="Local seed server port (default: 16882)",
    )
    mock_parser.add_argument(
        "--output", "-o",
        default="./mock_downloads",
        help="Output directory (default: ./mock_downloads)",
    )
    mock_parser.add_argument(
        "--connections",
        type=int, default=5,
        help="Number of peer connections to seed server (default: 5)",
    )

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    configure_logging(args.log_level)

    command_map = {
        "download": cmd_download,
        "seed": cmd_seed,
        "mock": cmd_mock,
    }

    cmd_fn = command_map.get(args.command)
    if cmd_fn is None:
        parser.print_help()
        sys.exit(1)

    try:
        exit_code = asyncio.run(cmd_fn(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
