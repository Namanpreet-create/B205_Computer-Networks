import json
import struct
import socket
import hashlib

# Network defaults
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000
CHUNK_SIZE   = 65536            # 64 KB per transfer chunk
HEADER_FMT   = "!I"            # 4-byte unsigned big-endian
HEADER_SIZE  = struct.calcsize(HEADER_FMT)

# Client -> Server commands
CMD_REGISTER        = "REGISTER"
CMD_LOGIN           = "LOGIN"
CMD_LOGOUT          = "LOGOUT"
CMD_LIST_DIR        = "LIST_DIR"
CMD_CREATE_FOLDER   = "CREATE_FOLDER"
CMD_DELETE_ITEM     = "DELETE_ITEM"
CMD_UPLOAD_INIT     = "UPLOAD_INIT"
CMD_UPLOAD_CHUNK    = "UPLOAD_CHUNK"
CMD_UPLOAD_DONE     = "UPLOAD_DONE"
CMD_DOWNLOAD_INIT   = "DOWNLOAD_INIT"
CMD_DOWNLOAD_CHUNK  = "DOWNLOAD_CHUNK"
CMD_SEARCH          = "SEARCH"
CMD_SET_PASSWORD    = "SET_PASSWORD"
CMD_VERIFY_PASSWORD = "VERIFY_PASSWORD"
CMD_SUBSCRIBE       = "SUBSCRIBE"

# Server -> Client response types
RESP_OK             = "OK"
RESP_ERROR          = "ERROR"
RESP_NOTIFY         = "NOTIFY"
RESP_DIR_LISTING    = "DIR_LISTING"
RESP_SEARCH_RESULTS = "SEARCH_RESULTS"
RESP_DOWNLOAD_META  = "DOWNLOAD_META"
RESP_DOWNLOAD_CHUNK = "DOWNLOAD_CHUNK"
RESP_UPLOAD_ACK     = "UPLOAD_ACK"

# Notification event subtypes (inside RESP_NOTIFY)
NOTIFY_FILE_ADDED    = "FILE_ADDED"
NOTIFY_FILE_DELETED  = "FILE_DELETED"
NOTIFY_FOLDER_ADDED  = "FOLDER_ADDED"
NOTIFY_UPLOAD_PROG   = "UPLOAD_PROGRESS"
NOTIFY_DOWNLOAD_PROG = "DOWNLOAD_PROGRESS"

# Low-level framing
def send_message(sock, payload):
    data   = json.dumps(payload).encode("utf-8")
    header = struct.pack(HEADER_FMT, len(data))
    sock.sendall(header + data)


def recv_message(sock):
    raw = _recv_exactly(sock, HEADER_SIZE)
    if raw is None:
        return None
    (length,) = struct.unpack(HEADER_FMT, raw)
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


def _recv_exactly(sock, n):
    """Read exactly *n* bytes from *sock*; return None on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

# Crypto helpers
def hash_password(password):
    """Return the SHA-256 hex digest of *password*."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()