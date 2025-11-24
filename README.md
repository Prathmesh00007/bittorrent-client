# BitTorrent Client

A lightweight peer-to-peer file sharing client built from scratch in Python to understand the BitTorrent protocol at a fundamental level. Implements core BitTorrent features including tracker communication, piece management, and asynchronous peer connections.

## Features

- **Tracker Communication**: Connects to HTTP/HTTPS trackers and retrieves peer lists
- **Concurrent Downloads**: Handles multiple peer connections simultaneously using asyncio
- **Data Integrity**: Verifies downloaded pieces with SHA-1 hashing before assembly
- **Multi-file Support**: Downloads both single-file and multi-file torrents
- **Progress Tracking**: Real-time download progress visualization in terminal

## Quick Start

git clone https://github.com/yourusername/bittorrent-client.git
cd bittorrent-client
pip install -r requirements.txt
python main.py path/to/yourfile.torrent

## Dependencies

- `bencodepy` - Decoding/encoding torrent metadata
- `requests` - HTTP tracker communication
- `rich` - Terminal UI and progress bars
- `async-timeout` - Timeout handling for async operations

Install all at once:
pip install bencodepy requests rich async-timeout

## How It Works

1. **Parse Torrent**: Reads `.torrent` file and extracts announce URL, piece hashes, and file info
2. **Contact Tracker**: Sends announce request to get list of active peers in the swarm
3. **Connect to Peers**: Establishes TCP connections with peers and performs BitTorrent handshake
4. **Download Pieces**: Requests file pieces from multiple peers concurrently
5. **Verify & Assemble**: Validates each piece's hash and writes to disk in correct order

## Limitations

This is a learning project with intentional simplifications:
- Does not support seeding (upload-only mode)
- No DHT (Distributed Hash Table) support
- No magnet link support
- No encryption (BEP 3 only)

## Technical Details

Built using Python 3.8+ with heavy reliance on `asyncio` for non-blocking I/O. The client implements the BitTorrent protocol as specified in [BEP 3](http://www.bittorrent.org/beps/bep_0003.html).

**Key concepts implemented:**
- Bencoding/Bdecoding for .torrent metadata parsing
- Binary protocol message framing (handshake, keep-alive, choke, interested, bitfield, request, piece)
- Piece selection strategy and request pipelining
- Async peer connection pooling

## License

MIT License - feel free to use this code for learning or personal projects
