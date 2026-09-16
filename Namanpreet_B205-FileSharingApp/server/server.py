import socket
import threading
import os
import sys
import logging
import argparse
import json
import base64
import mimetypes
from datetime import datetime
from pathlib import Path

# Allow imports from sibling packages when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.protocol import (
    send_message, recv_message, hash_password,
    DEFAULT_HOST, DEFAULT_PORT, CHUNK_SIZE,
    CMD_REGISTER, CMD_LOGIN, CMD_LOGOUT,
    CMD_LIST_DIR, CMD_CREATE_FOLDER,
    CMD_UPLOAD_INIT, CMD_UPLOAD_CHUNK, CMD_UPLOAD_DONE,
    CMD_DOWNLOAD_INIT, CMD_DOWNLOAD_CHUNK,
    CMD_SEARCH, CMD_SET_PASSWORD, CMD_VERIFY_PASSWORD,
    CMD_SUBSCRIBE, CMD_DELETE_ITEM,
    RESP_OK, RESP_ERROR, RESP_NOTIFY,
    RESP_DIR_LISTING, RESP_SEARCH_RESULTS,
    RESP_DOWNLOAD_META, RESP_DOWNLOAD_CHUNK, RESP_UPLOAD_ACK,
    NOTIFY_FILE_ADDED, NOTIFY_FILE_DELETED,
    NOTIFY_FOLDER_ADDED, NOTIFY_UPLOAD_PROG, NOTIFY_DOWNLOAD_PROG,
)
from server.database import Database

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            Path(__file__).parent.parent / "logs" / "server.log", encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("fileshare.server")

SERVER_FILES_DIR = Path(__file__).parent.parent / "server_files"
SERVER_FILES_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(__file__).parent.parent / "fileshare.db"


# ── Subscription / notification manager ─────────────────────────────────────

class SubscriptionManager:

    def __init__(self):
        self._subs: dict[str, socket.socket] = {}  # username → socket
        self._lock = threading.Lock()

    def subscribe(self, username: str, sock: socket.socket):
        with self._lock:
            self._subs[username] = sock
        logger.info("Subscribed: %s", username)

    def unsubscribe(self, username: str):
        with self._lock:
            self._subs.pop(username, None)

    def broadcast(self, event: dict, exclude: str | None = None):
        with self._lock:
            targets = {u: s for u, s in self._subs.items() if u != exclude}
        for username, sock in targets.items():
            try:
                send_message(sock, {
                    "type": RESP_NOTIFY,
                    "event": event,
                })
            except Exception as exc:
                logger.warning("Notify failed for %s: %s", username, exc)

    def notify_one(self, username: str, event: dict):
        with self._lock:
            sock = self._subs.get(username)
        if sock:
            try:
                send_message(sock, {"type": RESP_NOTIFY, "event": event})
            except Exception as exc:
                logger.warning("Notify-one failed for %s: %s", username, exc)


# ── Per-client handler ────────────────────────────────────────────────────────

class ClientHandler(threading.Thread):
    """Handles all messages from one connected client."""

    def __init__(self, sock: socket.socket, addr, db: Database,
                 sub_mgr: SubscriptionManager):
        super().__init__(daemon=True)
        self.sock    = sock
        self.addr    = addr
        self.db      = db
        self.sub_mgr = sub_mgr
        self.username: str | None = None          # set after successful login
        self._uploads: dict[str, dict] = {}       # upload_id → state

    # ── Thread entry ───────

    def run(self):
        logger.info("Connection from %s", self.addr)
        try:
            while True:
                msg = recv_message(self.sock)
                if msg is None:
                    break
                self._dispatch(msg)
        except Exception as exc:
            logger.error("Client %s error: %s", self.addr, exc, exc_info=True)
        finally:
            if self.username:
                self.sub_mgr.unsubscribe(self.username)
            self.sock.close()
            logger.info("Disconnected: %s (%s)", self.addr, self.username)

    # ── Dispatcher ────────

    def _dispatch(self, msg: dict):
        cmd = msg.get("cmd", "")
        handlers = {
            CMD_REGISTER:        self._handle_register,
            CMD_LOGIN:           self._handle_login,
            CMD_LOGOUT:          self._handle_logout,
            CMD_LIST_DIR:        self._handle_list_dir,
            CMD_CREATE_FOLDER:   self._handle_create_folder,
            CMD_DELETE_ITEM:     self._handle_delete_item,
            CMD_UPLOAD_INIT:     self._handle_upload_init,
            CMD_UPLOAD_CHUNK:    self._handle_upload_chunk,
            CMD_UPLOAD_DONE:     self._handle_upload_done,
            CMD_DOWNLOAD_INIT:   self._handle_download_init,
            CMD_DOWNLOAD_CHUNK:  self._handle_download_chunk,
            CMD_SEARCH:          self._handle_search,
            CMD_SET_PASSWORD:    self._handle_set_password,
            CMD_VERIFY_PASSWORD: self._handle_verify_password,
            CMD_SUBSCRIBE:       self._handle_subscribe,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self._err(f"Unknown command: {cmd}")
            return
        # All commands except REGISTER and LOGIN require authentication.
        if cmd not in (CMD_REGISTER, CMD_LOGIN) and not self.username:
            self._err("Not authenticated")
            return
        handler(msg)

    # ── Auth handlers ─────────

    def _handle_register(self, msg: dict):
        username = msg.get("username", "").strip()
        password = msg.get("password", "")
        if not username or not password:
            return self._err("Username and password required")
        if len(username) < 3:
            return self._err("Username must be at least 3 characters")
        ok = self.db.create_user(username, hash_password(password))
        if not ok:
            return self._err("Username already taken")
        logger.info("Registered new user: %s", username)
        self._ok({"message": f"User '{username}' registered successfully"})

    def _handle_login(self, msg: dict):
        username = msg.get("username", "").strip()
        password = msg.get("password", "")
        user = self.db.get_user(username)
        if user is None or user["password_hash"] != hash_password(password):
            return self._err("Invalid username or password")
        self.username = username
        logger.info("Login: %s from %s", username, self.addr)
        self._ok({"message": f"Welcome, {username}!", "username": username})

    def _handle_logout(self, _msg: dict):
        logger.info("Logout: %s", self.username)
        self.sub_mgr.unsubscribe(self.username)
        self.username = None
        self._ok({"message": "Logged out"})

    def _handle_subscribe(self, _msg: dict):
        """Register this socket for real-time notifications."""
        self.sub_mgr.subscribe(self.username, self.sock)
        self._ok({"message": "Subscribed to notifications"})

    # ── File-system handlers ──────────────────────────────────────────────────

    def _handle_list_dir(self, msg: dict):
        parent_id = msg.get("parent_id", 1)
        node = self.db.get_node(parent_id)
        if node is None:
            return self._err("Directory not found")
        # Check password if protected.
        pwd_hash = self.db.get_node_password_hash(parent_id)
        if pwd_hash and parent_id != 1:
            provided = msg.get("password_hash", "")
            if provided != pwd_hash:
                return self._err("PASSWORD_REQUIRED")
        rows = self.db.list_dir(parent_id)
        items = [self._node_to_dict(r) for r in rows]
        send_message(self.sock, {
            "type":     RESP_DIR_LISTING,
            "parent_id": parent_id,
            "items":    items,
        })

    def _handle_create_folder(self, msg: dict):
        parent_id = msg.get("parent_id", 1)
        name      = msg.get("name", "").strip()
        password  = msg.get("password", "")   # optional protection
        if not name:
            return self._err("Folder name required")
        if "/" in name or "\\" in name:
            return self._err("Folder name must not contain path separators")
        new_id = self.db.create_folder(parent_id, name, self.username)
        if new_id is None:
            return self._err(f"A folder or file named '{name}' already exists here")
        if password:
            self.db.set_node_password(new_id, hash_password(password))
        logger.info("%s created folder '%s' (id=%d)", self.username, name, new_id)
        # Broadcast to other subscribers.
        self.sub_mgr.broadcast({
            "event_type": NOTIFY_FOLDER_ADDED,
            "parent_id":  parent_id,
            "name":       name,
            "id":         new_id,
            "owner":      self.username,
        }, exclude=self.username)
        self._ok({"message": f"Folder '{name}' created", "id": new_id})

    def _handle_delete_item(self, msg: dict):
        node_id = msg.get("node_id")
        if node_id is None:
            return self._err("node_id required")
        node = self.db.get_node(node_id)
        if node is None:
            return self._err("Item not found")
        if node["id"] == 1:
            return self._err("Cannot delete root")
        # Only owner can delete.
        if node["owner"] != self.username:
            return self._err("Permission denied — you are not the owner")
        disk_paths = self.db.delete_node(node_id)
        # Remove files from disk.
        for dp in disk_paths:
            try:
                os.remove(dp)
            except FileNotFoundError:
                pass
        logger.info("%s deleted node %d (%s)", self.username, node_id, node["name"])
        self.sub_mgr.broadcast({
            "event_type": NOTIFY_FILE_DELETED,
            "node_id":    node_id,
            "name":       node["name"],
            "parent_id":  node["parent_id"],
        }, exclude=self.username)
        self._ok({"message": "Deleted successfully"})

    # ── Upload (3-phase: INIT → CHUNKs → DONE) ───────────────────────────────

    def _handle_upload_init(self, msg: dict):
        parent_id = msg.get("parent_id", 1)
        filename  = msg.get("filename", "").strip()
        file_size = msg.get("file_size", 0)
        mime_type = msg.get("mime_type", "application/octet-stream")
        upload_id = msg.get("upload_id", "")

        if not filename or not upload_id:
            return self._err("filename and upload_id required")
        if "/" in filename or "\\" in filename:
            return self._err("Filename must not contain path separators")
        # Check name uniqueness.
        existing = self.db.get_node_by_name(parent_id, filename)
        if existing:
            return self._err(f"A file named '{filename}' already exists here")

        # Prepare a temporary disk location.
        safe_name = f"{upload_id}_{filename}"
        disk_path = str(SERVER_FILES_DIR / safe_name)

        self._uploads[upload_id] = {
            "parent_id": parent_id,
            "filename":  filename,
            "file_size": file_size,
            "mime_type": mime_type,
            "disk_path": disk_path,
            "received":  0,
            "fh":        open(disk_path, "wb"),
        }
        logger.info("%s upload-init '%s' (%d bytes)", self.username, filename, file_size)
        send_message(self.sock, {"type": RESP_UPLOAD_ACK, "upload_id": upload_id,
                                 "status": "ready"})

    def _handle_upload_chunk(self, msg: dict):
        upload_id = msg.get("upload_id", "")
        state = self._uploads.get(upload_id)
        if state is None:
            return self._err("Unknown upload_id")
        data_b64 = msg.get("data", "")
        data = base64.b64decode(data_b64)
        state["fh"].write(data)
        state["received"] += len(data)
        # Progress notification to uploader.
        if state["file_size"] > 0:
            pct = min(100, int(state["received"] * 100 / state["file_size"]))
        else:
            pct = 100
        self.sub_mgr.notify_one(self.username, {
            "event_type": NOTIFY_UPLOAD_PROG,
            "upload_id":  upload_id,
            "filename":   state["filename"],
            "percent":    pct,
        })
        send_message(self.sock, {"type": RESP_UPLOAD_ACK, "upload_id": upload_id,
                                 "status": "chunk_ok", "received": state["received"]})

    def _handle_upload_done(self, msg: dict):
        upload_id = msg.get("upload_id", "")
        state = self._uploads.pop(upload_id, None)
        if state is None:
            return self._err("Unknown upload_id")
        state["fh"].close()
        # Commit to DB.
        node_id = self.db.create_file_record(
            parent_id=state["parent_id"],
            name=state["filename"],
            owner=self.username,
            disk_path=state["disk_path"],
            file_size=state["file_size"],
            mime_type=state["mime_type"],
        )
        if node_id is None:
            os.remove(state["disk_path"])
            return self._err("File name collision — upload aborted")
        logger.info("%s upload-done '%s' node_id=%d", self.username,
                    state["filename"], node_id)
        self.sub_mgr.broadcast({
            "event_type": NOTIFY_FILE_ADDED,
            "parent_id":  state["parent_id"],
            "name":       state["filename"],
            "id":         node_id,
            "owner":      self.username,
            "file_size":  state["file_size"],
            "mime_type":  state["mime_type"],
        }, exclude=self.username)
        self._ok({"message": "Upload complete", "node_id": node_id})

    # ──

    def _handle_download_init(self, msg: dict):
        node_id = msg.get("node_id")
        if node_id is None:
            return self._err("node_id required")
        node = self.db.get_node(node_id)
        if node is None or node["node_type"] != "file":
            return self._err("File not found")
        # Password check for parent folder.
        parent_pwd = self.db.get_node_password_hash(node["parent_id"])
        if parent_pwd and node["parent_id"] != 1:
            provided = msg.get("password_hash", "")
            if provided != parent_pwd:
                return self._err("PASSWORD_REQUIRED")
        if not os.path.exists(node["disk_path"]):
            return self._err("File data missing on server")
        send_message(self.sock, {
            "type":      RESP_DOWNLOAD_META,
            "node_id":   node_id,
            "filename":  node["name"],
            "file_size": node["file_size"],
            "mime_type": node["mime_type"],
            "owner":     node["owner"],
            "created_at": node["created_at"],
        })

    def _handle_download_chunk(self, msg: dict):
        node_id = msg.get("node_id")
        offset  = msg.get("offset", 0)
        node = self.db.get_node(node_id)
        if node is None or node["node_type"] != "file":
            return self._err("File not found")
        try:
            with open(node["disk_path"], "rb") as f:
                f.seek(offset)
                data = f.read(CHUNK_SIZE)
        except OSError as exc:
            return self._err(f"Read error: {exc}")
        eof = (offset + len(data)) >= (node["file_size"] or 0)
        send_message(self.sock, {
            "type":    RESP_DOWNLOAD_CHUNK,
            "node_id": node_id,
            "offset":  offset,
            "data":    base64.b64encode(data).decode(),
            "eof":     eof,
        })
        # Progress notification to downloader.
        if node["file_size"] and node["file_size"] > 0:
            pct = min(100, int((offset + len(data)) * 100 / node["file_size"]))
        else:
            pct = 100
        self.sub_mgr.notify_one(self.username, {
            "event_type": NOTIFY_DOWNLOAD_PROG,
            "node_id":    node_id,
            "filename":   node["name"],
            "percent":    pct,
        })

    # ── Search ───────

    def _handle_search(self, msg: dict):
        query = msg.get("query", "").strip()
        if not query:
            return self._err("Search query required")
        rows = self.db.search_files(query)
        results = [self._node_to_dict(r) for r in rows]
        send_message(self.sock, {
            "type":    RESP_SEARCH_RESULTS,
            "query":   query,
            "results": results,
        })

    # ── Password protection ──────────

    def _handle_set_password(self, msg: dict):
        node_id  = msg.get("node_id")
        password = msg.get("password", "")
        node = self.db.get_node(node_id)
        if node is None:
            return self._err("Node not found")
        if node["owner"] != self.username:
            return self._err("Only the owner can set a password")
        if password:
            self.db.set_node_password(node_id, hash_password(password))
            self._ok({"message": "Password set"})
        else:
            self.db.remove_node_password(node_id)
            self._ok({"message": "Password removed"})

    def _handle_verify_password(self, msg: dict):
        node_id  = msg.get("node_id")
        password = msg.get("password", "")
        expected = self.db.get_node_password_hash(node_id)
        if expected is None:
            return self._ok({"verified": True, "protected": False})
        if hash_password(password) == expected:
            return self._ok({"verified": True,
                              "password_hash": hash_password(password)})
        return self._ok({"verified": False})

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _ok(self, payload: dict):
        send_message(self.sock, {"type": RESP_OK, **payload})

    def _err(self, message: str):
        send_message(self.sock, {"type": RESP_ERROR, "message": message})
        logger.debug("Error sent to %s: %s", self.username or self.addr, message)

    @staticmethod
    def _node_to_dict(row) -> dict:
        return {
            "id":           row["id"],
            "parent_id":    row["parent_id"],
            "owner":        row["owner"],
            "node_type":    row["node_type"],
            "name":         row["name"],
            "file_size":    row["file_size"],
            "mime_type":    row["mime_type"],
            "created_at":   row["created_at"],
            "is_protected": bool(row["is_protected"]),
        }


# ── Server main ───────────────────────────────────────────────────────────────

class FileShareServer:
    def __init__(self, host: str, port: int):
        self.host    = host
        self.port    = port
        self.db      = Database(str(DB_PATH))
        self.sub_mgr = SubscriptionManager()

    def run(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_sock:
            server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_sock.bind((self.host, self.port))
            server_sock.listen(50)
            logger.info("FileShare server listening on %s:%d", self.host, self.port)
            while True:
                conn, addr = server_sock.accept()
                handler = ClientHandler(conn, addr, self.db, self.sub_mgr)
                handler.start()


def main():
    parser = argparse.ArgumentParser(description="FileShare Server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    server = FileShareServer(args.host, args.port)
    server.run()


if __name__ == "__main__":
    main()
