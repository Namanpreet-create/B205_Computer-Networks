# FileShare — Technical Documentation
**B205 Computer Networks — Individual Project**

---

## 1. System Architecture

### Overview

FileShare is a **Client–Server** file-sharing platform built entirely with Python's standard
library (no advanced third-party libraries that replicate core functionality). All network
communication is implemented using raw **TCP socket programming**.

```
┌─────────────────────────────────────────────────────────┐
│                       SERVER                            │
│                                                         │
│  ┌─────────────┐   ┌────────────────┐  ┌─────────────┐ │
│  │  TCP Socket │   │  ClientHandler │  │  Database   │ │
│  │  (Listener) │──▶│  (1 per conn) │─▶│  (SQLite)   │ │
│  └─────────────┘   └────────────────┘  └─────────────┘ │
│                            │                            │
│                    ┌───────▼────────┐                   │
│                    │ SubscriptionMgr│ ←── broadcasts    │
│                    │ (notifications)│     NOTIFY msgs   │
│                    └────────────────┘                   │
│                            │                            │
│                   server_files/ (disk)                  │
└─────────────────────────────────────────────────────────┘
             ▲ TCP / JSON-over-length-prefix
             ▼
┌─────────────────────────────────────────────────────────┐
│                       CLIENT(S)                         │
│                                                         │
│  ┌───────────────┐    ┌───────────────────────────────┐ │
│  │  NetworkClient│    │  FileShareApp (Tkinter GUI)   │ │
│  │  (TCP socket) │◀──▶│  • Auth window                │ │
│  │  + listener   │    │  • Folder tree (left panel)   │ │
│  │    thread     │    │  • File list (right panel)    │ │
│  └───────────────┘    │  • Upload / Download / Search │ │
│                       │  • Real-time toast popups     │ │
│                       └───────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

Multiple client instances may connect simultaneously; each is served by its own
`ClientHandler` thread on the server side.

---

## 2. Protocol Specification

### 2.1 Transport Layer

| Property | Value |
|---|---|
| Transport | TCP (connection-oriented, reliable, ordered) |
| Framing | 4-byte big-endian length prefix + JSON body |
| Encoding | UTF-8 JSON |
| Port (default) | 9000 |

### 2.2 Message Frame Format

```
 0       1       2       3       4         4+N
 ┌───────────────────────┬──────────────────────┐
 │  Length (4 bytes BE)  │  JSON payload (N B)  │
 └───────────────────────┴──────────────────────┘
```

The 4-byte header encodes the byte-length of the JSON body that immediately follows.
This eliminates ambiguity about message boundaries over a stream socket.

### 2.3 Command Reference (Client → Server)

| Command | Key Fields | Description |
|---|---|---|
| `REGISTER` | `username`, `password` | Create new account |
| `LOGIN` | `username`, `password` | Authenticate |
| `LOGOUT` | — | End session |
| `SUBSCRIBE` | — | Enable real-time notifications |
| `LIST_DIR` | `parent_id`, `password_hash?` | List folder contents |
| `CREATE_FOLDER` | `parent_id`, `name`, `password?` | Create folder (optionally protected) |
| `DELETE_ITEM` | `node_id` | Delete file or folder tree |
| `UPLOAD_INIT` | `parent_id`, `filename`, `file_size`, `mime_type`, `upload_id` | Begin upload |
| `UPLOAD_CHUNK` | `upload_id`, `data` (base64) | Transfer one chunk |
| `UPLOAD_DONE` | `upload_id` | Commit upload |
| `DOWNLOAD_INIT` | `node_id`, `password_hash?` | Retrieve file metadata |
| `DOWNLOAD_CHUNK` | `node_id`, `offset` | Fetch one 64 KB chunk |
| `SEARCH` | `query` | Full-text file search |
| `SET_PASSWORD` | `node_id`, `password` | Password-protect a node |
| `VERIFY_PASSWORD` | `node_id`, `password` | Check a password |

### 2.4 Response Reference (Server → Client)

| Response type | Description |
|---|---|
| `OK` | Success; may carry extra fields |
| `ERROR` | Failure; carries `message` string |
| `DIR_LISTING` | `items` array from `LIST_DIR` |
| `SEARCH_RESULTS` | `results` array |
| `DOWNLOAD_META` | File metadata before chunk transfer |
| `DOWNLOAD_CHUNK` | `data` (base64), `offset`, `eof` flag |
| `UPLOAD_ACK` | Per-chunk acknowledgement |
| `NOTIFY` | Asynchronous push event (see §2.5) |

### 2.5 Notification Events (Server → Client, async)

| Event type | Trigger |
|---|---|
| `FILE_ADDED` | Another user completes an upload |
| `FILE_DELETED` | Another user deletes a file |
| `FOLDER_ADDED` | Another user creates a folder |
| `UPLOAD_PROGRESS` | Percentage update sent to the uploader |
| `DOWNLOAD_PROGRESS` | Percentage update sent to the downloader |

---

## 3. Network Communication Flow

### 3.1 Upload Flow

```
Client                        Server
  │                              │
  │──UPLOAD_INIT───────────────▶│  (allocate temp file, store state)
  │◀─UPLOAD_ACK (ready)─────────│
  │                              │
  │──UPLOAD_CHUNK (offset 0)───▶│  (write chunk to disk)
  │◀─UPLOAD_ACK (chunk_ok)──────│
  │   ... (repeat per 64 KB)    │
  │──UPLOAD_DONE───────────────▶│  (commit to DB, broadcast NOTIFY)
  │◀─OK ────────────────────────│
  │                              │
  │     [other subscribers]      │
  │◀─NOTIFY (FILE_ADDED)────────│  (broadcast to all others)
```

### 3.2 Download Flow

```
Client                        Server
  │                              │
  │──DOWNLOAD_INIT─────────────▶│  (lookup, check password)
  │◀─DOWNLOAD_META──────────────│  (filename, size, mime)
  │                              │
  │──DOWNLOAD_CHUNK (offset=0)─▶│  (read 64 KB from disk)
  │◀─DOWNLOAD_CHUNK (data, eof)─│
  │   ... (repeat until eof)    │
```

### 3.3 Notification Subscription Flow

```
Client                        Server
  │──SUBSCRIBE─────────────────▶│  (register socket in SubscriptionManager)
  │◀─OK ────────────────────────│
  │                              │
  │  [client stays connected]    │
  │◀─NOTIFY (any event) ────────│  (async push, no request needed)
```

---

## 4. Database Schema

```sql
users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- SHA-256 hex
    created_at    TEXT NOT NULL
);

fs_nodes (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER REFERENCES fs_nodes(id) ON DELETE CASCADE,
    owner      TEXT NOT NULL,
    node_type  TEXT CHECK(node_type IN ('file','folder')),
    name       TEXT NOT NULL,
    disk_path  TEXT,                      -- server-side path (files only)
    file_size  INTEGER,
    mime_type  TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(parent_id, name)               -- unique name per folder
);

node_passwords (
    node_id       INTEGER PRIMARY KEY REFERENCES fs_nodes(id),
    password_hash TEXT NOT NULL           -- SHA-256 of folder/file password
);
```

---

## 5. Installation & Usage

### Prerequisites

- Python 3.11+ (standard library only — no pip install required)
- `tkinter` (included with CPython; on Linux: `sudo apt install python3-tk`)

### Directory Structure

```
fileshare/
├── run_server.py        ← Launch server
├── run_client.py        ← Launch GUI client
├── test_system.py       ← Integration tests
├── DOCUMENTATION.md     ← This file
├── shared/
│   └── protocol.py      ← Message framing & constants
├── server/
│   ├── server.py        ← TCP server, handlers, broadcaster
│   └── database.py      ← SQLite persistence layer
├── client/
│   ├── network.py       ← Socket client wrapper
│   └── gui.py           ← Tkinter GUI
├── server_files/        ← Uploaded files stored here
└── logs/
    ├── server.log
    └── client.log
```

### Running the Server

```bash
# Default: 127.0.0.1:9000
python run_server.py

# Custom host/port
python run_server.py --host 0.0.0.0 --port 9000
```

### Running the Client

```bash
python run_client.py
```

1. Enter the server host and port in the Sign In dialog.
2. Register a new account or sign in with an existing one.
3. Use the GUI to upload, download, search, and manage files.

### Running Integration Tests

```bash
python test_system.py
```

This starts an in-process server on port 9001 and exercises all features with 3 users.

---

## 6. Protocol Analysis

### 6.1 Protocol Selection Rationale

| Layer | Choice | Rationale |
|---|---|---|
| Transport | **TCP** | Reliability is essential for file transfer; TCP guarantees ordered, loss-free delivery |
| Framing | **Length-prefix + JSON** | Simple to implement, human-readable, naturally handles variable-length messages |
| Auth | **Username + SHA-256 password** | Passwords are never sent in plaintext; the hash travels instead |
| Chunking | **64 KB chunks** | Balances memory usage against round-trip overhead; allows progress reporting |
| Notifications | **Persistent subscription socket** | Server pushes events without polling; low latency, no extra port needed |

### 6.2 Pros and Cons of the Chosen Approach

#### TCP + Length-Prefix JSON

| Pros | Cons |
|---|---|
| No external libraries needed | JSON adds overhead vs. binary formats (e.g., protobuf) |
| Human-readable messages aid debugging | No built-in compression |
| Reliable, ordered delivery matches file-transfer semantics | More verbose than a binary protocol |
| Easy to extend with new command types | |

#### Client–Server Architecture

| Pros | Cons |
|---|---|
| Centralised file storage — easier to enforce permissions | Single point of failure (server) |
| Simple authentication model | Does not scale horizontally without extra work |
| Straightforward to implement search and metadata | Clients always need the server to be online |

#### SHA-256 Password Hashing

| Pros | Cons |
|---|---|
| No plaintext passwords stored or transmitted | SHA-256 without salt is vulnerable to rainbow-table attacks in a production context; bcrypt/scrypt recommended for real deployment |
| Lightweight — no library needed | |

---

## 7. Security Notes

- Passwords are SHA-256 hashed before storage and transmission.
- Folder/file passwords protect access at the protocol level.
- Only the owner of a node may delete it or change its password.
- The server validates all inputs and rejects path-separator characters in filenames.

> **Academic scope:** For a production system, TLS should wrap the TCP channel, and
> bcrypt/argon2 should replace SHA-256 for credential storage.

---

## 8. References

- Python `socket` documentation: https://docs.python.org/3/library/socket.html  
- Python `sqlite3` documentation: https://docs.python.org/3/library/sqlite3.html  
- Python `tkinter` documentation: https://docs.python.org/3/library/tkinter.html  
- Tanenbaum, A. S. & Wetherall, D. J. (2011). *Computer Networks* (5th ed.). Pearson.  
- Stevens, W. R. (1994). *TCP/IP Illustrated, Volume 1*. Addison-Wesley.
