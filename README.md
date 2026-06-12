# Distributed BitTorrent Client — Python async

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![asyncio](https://img.shields.io/badge/concurrency-asyncio-green.svg)](https://docs.python.org/3/library/asyncio.html)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A production-quality **educational implementation** of a BitTorrent client in Python using `asyncio`. Built to demonstrate core distributed systems concepts including TCP wire protocol handling, concurrent peer communication, rarest-first piece selection, tit-for-tat choking/unchoking, SHA-1 integrity verification, and a decoupled network/disk I/O pipeline.

> **⚠️ Educational Use Only** — This client is designed for learning and local/private testing. Use only with files you own or have rights to distribute.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       CLI / Entry Point                          │
│                       bittorrent/cli.py                          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    DownloadEngine
                    engine.py
                           │
         ┌─────────────────┼──────────────────┐
         │                 │                  │
   PeerDiscovery      SwarmManager       PiecePicker
   tracker.py          swarm.py           pieces.py
         │                 │
         │         ┌───────┴────────────────┐
         │         │  PeerConnection × N≤50  │
         │         │  peer.py                │
         │         │  + ChokingManager       │
         │         │    choking.py           │
         │         └───────┬────────────────┘
         │                 │ block_queue (asyncio.Queue)
         │          PieceVerifier
         │          verifier.py   (SHA-1 check)
         │                 │ write_queue (asyncio.Queue)
         │           DiskWriter
         │           disk_writer.py
         │                 │
         └─────────── Output File ──────────────────┘
```

### Module Breakdown

| Module | Responsibility |
|--------|----------------|
| `torrent.py` | Bencode encode/decode, `.torrent` parsing, mock torrent builder |
| `protocol.py` | Wire protocol codec: all 10 message types + handshake + streaming parser |
| `pieces.py` | PiecePicker (rarest-first), PeerAvailability, Block/Piece state machines |
| `peer.py` | Single peer TCP connection: handshake, read loop, request pipeline, keep-alive |
| `choking.py` | Tit-for-tat choking/unchoking with optimistic unchoke |
| `verifier.py` | SHA-1 piece verification (runs in executor, non-blocking) |
| `disk_writer.py` | Async disk writes with pre-allocation and final integrity check |
| `tracker.py` | HTTP tracker client, mock tracker, peer discovery coordinator |
| `swarm.py` | SwarmManager: peer lifecycle, backoff, progress reporting |
| `seed_server.py` | Local seeding server for integration testing |
| `engine.py` | Top-level download session orchestrator |
| `cli.py` | Argument parsing, `download` / `seed` / `mock` commands |

---

## Quick Start

### Prerequisites

```bash
python --version   # Must be 3.11+
```

### Install

```bash
# Clone or unzip the project
cd bittorrent

# Install in editable mode with dev dependencies
pip install -e ".[dev]"
```

### Run the Self-Contained Mock Test

No real `.torrent` file or internet needed:

```bash
# Download a 10 MB randomly generated file from a local seeder
python -m bittorrent mock --size 10 --piece-length 512

# Smaller test (1 MB, tiny pieces — stress tests the picker)
python -m bittorrent mock --size 1 --piece-length 64

# With debug logging
python -m bittorrent --log-level DEBUG mock --size 2 --piece-length 256
```

### Download from a Real .torrent File

```bash
# Basic download
python -m bittorrent download ubuntu.torrent --output ./downloads

# Connect to specific peers + tracker
python -m bittorrent download myfile.torrent \
    --output ./downloads \
    --peers 192.168.1.10:6881 192.168.1.11:6881 \
    --max-peers 50
```

### Serve a Local File (Seeder Mode)

```bash
# Serve a file — other clients can connect and download
python -m bittorrent seed myfile.bin --port 6882 --piece-length 512
```

---

## Running Tests

```bash
# Run all tests
pytest

# Run specific test modules
pytest tests/test_torrent.py -v
pytest tests/test_protocol.py -v
pytest tests/test_pieces.py -v
pytest tests/test_choking.py -v
pytest tests/test_integration.py -v

# Run with coverage
pytest --cov=bittorrent --cov-report=html

# Run integration tests only
pytest tests/test_integration.py -v --timeout=60
```

Expected output:
```
tests/test_torrent.py    ✓ 22 passed
tests/test_protocol.py   ✓ 28 passed
tests/test_pieces.py     ✓ 21 passed
tests/test_choking.py    ✓ 11 passed
tests/test_integration.py ✓ 5 passed
```

---

## Protocol Implementation

### Supported Messages

| ID | Name | Direction | Notes |
|----|------|-----------|-------|
| — | Handshake | ↔ | 68 bytes, info_hash validation |
| — | Keep-alive | ↔ | 4-byte zero length prefix |
| 0 | Choke | → | Remote will not serve requests |
| 1 | Unchoke | → | Remote will serve requests |
| 2 | Interested | ↔ | We want data from remote |
| 3 | Not Interested | ↔ | We don't want data from remote |
| 4 | Have | ← | Remote has a new piece |
| 5 | Bitfield | ← | Remote's complete piece availability |
| 6 | Request | → | Ask for a 16 KB block |
| 7 | Piece | ← | Block data received |
| 8 | Cancel | → | Cancel a pending request |
| 20 | Extension | — | BEP-10 (logged and ignored) |

### Wire Protocol Details

```
Handshake: <1: pstrlen=19><19: "BitTorrent protocol"><8: reserved><20: info_hash><20: peer_id>
Messages:  <4: length (BE uint32)><1: message_id><N: payload>
Keep-alive: <4: 0x00000000>
```

---

## Design Decisions & Performance Choices

### 1. asyncio over threading

All I/O is async — no threads for networking. This means:
- 50+ simultaneous peer connections with a single OS thread
- No GIL contention for I/O-bound work
- CPU-bound work (SHA-1, file writes) offloaded via `run_in_executor`

### 2. Three-Stage Pipeline

```
Network → [block_queue] → Verifier → [write_queue] → DiskWriter
```

This decoupling means:
- Network I/O is never blocked by disk I/O
- Verification (SHA-1) runs in a thread pool, not blocking the event loop
- Queue back-pressure prevents unbounded memory growth

### 3. Rarest-First Selection

The PiecePicker sorts candidate pieces by `_availability[i]` count (ascending).
This ensures rare pieces are replicated first, which is the foundation of
BitTorrent's swarm health. Ties are broken randomly to spread load.

### 4. End-Game Mode

When ≤ 4 pieces remain, the picker enters end-game: it sends requests for
all remaining blocks to all connected peers simultaneously. The first response
wins. This eliminates the "last piece" slowdown common in naive clients.

### 5. Block-Level Pipelining

Each peer connection keeps up to 10 block requests in-flight simultaneously
(`MAX_PIPELINED_REQUESTS = 10`). This exploits TCP's full throughput potential
and avoids RTT-limited one-at-a-time fetching.

### 6. Tit-for-Tat Choking

Every 10 seconds, peers are ranked by download rate. Top 4 are unchoked.
Every 30 seconds, one randomly chosen choked peer gets an "optimistic unchoke"
— giving new peers a chance to prove themselves. This prevents exploitation
and encourages contribution.

### 7. File Pre-allocation

Before any writes, the output file is pre-allocated to its full expected size:
- Linux: `posix_fallocate` (true block allocation)  
- Windows/macOS: seek to end and write one byte (sparse file)

This avoids file system fragmentation and prevents the kernel from repeatedly
extending the file during download.

### 8. Zero External Dependencies

The entire implementation uses Python stdlib only:
- `asyncio` for concurrency
- `hashlib` for SHA-1
- `struct` for binary protocol parsing
- `urllib` for HTTP tracker requests
- `argparse` for CLI

---

## Performance Expectations

| Scenario | Expected Throughput |
|----------|---------------------|
| Local loopback (mock test) | 50–200 MB/s |
| LAN (1 GbE) | 10–50 MB/s |
| Internet (with good peers) | Bandwidth-limited |

The bottleneck is typically:
1. **Network**: RTT and peer upload capacity
2. **SHA-1 hashing**: ~2 GB/s on modern CPUs (not a bottleneck for typical torrents)
3. **Disk writes**: Sequential writes are fast; concurrent random writes may be slower

---

## Limitations (Educational Simplifications)

| Feature | Status | Notes |
|---------|--------|-------|
| DHT (BEP-5) | ❌ | Not implemented; uses HTTP trackers only |
| PEX (Peer Exchange) | ❌ | Not implemented |
| µTP transport | ❌ | TCP only |
| Upload (seeding) | ⚠️ | SeedServer only; PeerConnection doesn't upload |
| Multi-file torrents | ⚠️ | Parsing supported, disk mapping simplified |
| Magnet links | ❌ | Not implemented |
| Resume/checkpoint | ⚠️ | No disk state persistence between runs |
| IPv6 | ❌ | IPv4 only |
| Rate limiting | ❌ | No upload rate limits |

---

## File Structure

```
bittorrent/
├── bittorrent/
│   ├── __init__.py        # Package
│   ├── __main__.py        # python -m bittorrent entry
│   ├── cli.py             # CLI: download / seed / mock commands
│   ├── torrent.py         # Bencode + .torrent parser + mock builder
│   ├── protocol.py        # Wire protocol codec (all message types)
│   ├── pieces.py          # PiecePicker, PeerAvailability, Block state
│   ├── peer.py            # Async peer TCP connection manager
│   ├── choking.py         # Tit-for-tat choking/unchoking
│   ├── verifier.py        # SHA-1 piece verification pipeline stage
│   ├── disk_writer.py     # Async disk I/O pipeline stage
│   ├── tracker.py         # HTTP tracker + mock tracker + discovery
│   ├── swarm.py           # SwarmManager: peer lifecycle + progress
│   ├── seed_server.py     # Local TCP seeder for testing
│   └── engine.py          # DownloadEngine: top-level orchestrator
├── tests/
│   ├── __init__.py
│   ├── test_torrent.py    # Bencode + metadata parsing tests
│   ├── test_protocol.py   # Wire protocol + codec tests
│   ├── test_pieces.py     # Piece picker + block state tests
│   ├── test_choking.py    # Choking algorithm + SHA-1 tests
│   └── test_integration.py # End-to-end download pipeline tests
├── pyproject.toml
├── .gitignore
└── README.md
```

---

## License

MIT License — free to use, modify, and share.
