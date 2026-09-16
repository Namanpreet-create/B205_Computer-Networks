"""
network.py — Client-side networking for FileShare.

NetworkClient wraps all socket communication and provides:
  • Synchronous request/response methods (called from worker threads).
  • Notification handling through the normal request/response reader.
  • Upload / download helpers with per-chunk progress callbacks.
"""

import socket
import threading
import os
import base64
import uuid
import logging
import mimetypes
from pathlib import Path

from shared.protocol import (
    send_message, recv_message, hash_password,
    DEFAULT_HOST, DEFAULT_PORT, CHUNK_SIZE,
    CMD_REGISTER, CMD_LOGIN, CMD_LOGOUT,
    CMD_LIST_DIR, CMD_CREATE_FOLDER, CMD_DELETE_ITEM,
    CMD_UPLOAD_INIT, CMD_UPLOAD_CHUNK, CMD_UPLOAD_DONE,
    CMD_DOWNLOAD_INIT, CMD_DOWNLOAD_CHUNK,
    CMD_SEARCH, CMD_SET_PASSWORD, CMD_VERIFY_PASSWORD,
    CMD_SUBSCRIBE,
    RESP_OK, RESP_ERROR, RESP_NOTIFY,
    RESP_DIR_LISTING, RESP_SEARCH_RESULTS,
    RESP_DOWNLOAD_META, RESP_DOWNLOAD_CHUNK, RESP_UPLOAD_ACK,
)

logger = logging.getLogger("fileshare.client.net")


class NetworkError(Exception):
    """Raised when the server returns RESP_ERROR or the connection drops."""


class NetworkClient:
    """
    Thread-safe client transport.

    Usage:
        client = NetworkClient()
        client.connect(host, port)
        client.register("alice", "pass123")
        client.login("alice", "pass123")
        client.subscribe(on_notify_callback)
        ...
        client.disconnect()
    """

    def __init__(self):
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._listener: threading.Thread | None = None
        self._notify_cb = None
        self._username: str | None = None
        self._connected = False

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((host, port))
        self._connected = True
        logger.info("Connected to %s:%d", host, port)

    def disconnect(self):
        self._connected = False

        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                self._sock.close()
            except OSError:
                pass

            self._sock = None

        self._listener = None
        self._notify_cb = None

        logger.info("Disconnected from server")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def register(self, username: str, password: str) -> dict:
        return self._request({
            "cmd": CMD_REGISTER,
            "username": username,
            "password": password
        })

    def login(self, username: str, password: str) -> dict:
        resp = self._request({
            "cmd": CMD_LOGIN,
            "username": username,
            "password": password
        })

        self._username = username
        return resp

    def logout(self) -> dict:
        return self._request({"cmd": CMD_LOGOUT})

    # ── Notifications ──────────

    def subscribe(self, callback):

        self._notify_cb = callback

        resp = self._request({
            "cmd": CMD_SUBSCRIBE
        })

        return resp

    def _listen_loop(self):

        return

    # ── File-system ops ──────────

    def list_dir(self, parent_id: int = 1,
                 password: str = "") -> list[dict]:

        payload: dict = {
            "cmd": CMD_LIST_DIR,
            "parent_id": parent_id
        }

        if password:
            payload["password_hash"] = hash_password(password)

        resp = self._request(payload)

        return resp.get("items", [])

    def create_folder(self, parent_id: int, name: str,
                      password: str = "") -> dict:

        return self._request({
            "cmd": CMD_CREATE_FOLDER,
            "parent_id": parent_id,
            "name": name,
            "password": password,
        })

    def delete_item(self, node_id: int) -> dict:
        return self._request({
            "cmd": CMD_DELETE_ITEM,
            "node_id": node_id
        })

    def search(self, query: str) -> list[dict]:
        resp = self._request({
            "cmd": CMD_SEARCH,
            "query": query
        })

        return resp.get("results", [])

    def set_password(self, node_id: int, password: str) -> dict:
        return self._request({
            "cmd": CMD_SET_PASSWORD,
            "node_id": node_id,
            "password": password
        })

    def verify_password(self, node_id: int, password: str) -> dict:
        return self._request({
            "cmd": CMD_VERIFY_PASSWORD,
            "node_id": node_id,
            "password": password
        })

    # ── Upload ──────────────────────

    def upload_file(self, local_path: str, parent_id: int,
                    progress_cb=None) -> dict:

        path = Path(local_path)
        filename = path.name
        file_size = path.stat().st_size

        mime_type = (
            mimetypes.guess_type(str(path))[0]
            or "application/octet-stream"
        )

        upload_id = str(uuid.uuid4())

        logger.info(
            "UPLOAD START: %s (%d bytes)",
            filename,
            file_size
        )

        # ─────────────────────────────────────────────────────────────
        # Phase 1: INIT
        # ─────────────────────────────────────────────────────────────

        logger.info("UPLOAD INIT: waiting for socket lock")

        with self._lock:

            logger.info("UPLOAD INIT: lock acquired")

            if not self._connected or self._sock is None:
                raise NetworkError("Not connected to server")

            logger.info("UPLOAD INIT: sending request")

            send_message(self._sock, {
                "cmd": CMD_UPLOAD_INIT,
                "parent_id": parent_id,
                "filename": filename,
                "file_size": file_size,
                "mime_type": mime_type,
                "upload_id": upload_id,
            })

            logger.info(
                "UPLOAD INIT: request sent, waiting for response"
            )

            resp = self._recv_non_notify()

            logger.info(
                "UPLOAD INIT: response received: %s",
                resp
            )

        self._check(resp)

        # Phase 2: CHUNKS
        sent = 0
        chunk_number = 0
        with open(local_path, "rb") as f:
            while True:

                data = f.read(CHUNK_SIZE)

                if not data:
                    break

                chunk_number += 1

                logger.info(
                    "UPLOAD CHUNK %d: size=%d",
                    chunk_number,
                    len(data)
                )

                with self._lock:

                    if not self._connected or self._sock is None:
                        raise NetworkError(
                            "Connection lost during upload"
                        )

                    send_message(self._sock, {
                        "cmd": CMD_UPLOAD_CHUNK,
                        "upload_id": upload_id,
                        "data": base64.b64encode(data).decode(),
                    })

                    logger.info(
                        "UPLOAD CHUNK %d: sent, waiting for ACK",
                        chunk_number
                    )

                    ack = self._recv_non_notify()

                    logger.info(
                        "UPLOAD CHUNK %d: ACK received: %s",
                        chunk_number,
                        ack
                    )

                self._check(ack)

                sent += len(data)

                if progress_cb and file_size > 0:
                    progress_cb(
                        min(
                            100,
                            int(sent * 100 / file_size)
                        )
                    )

        # ─────────────────────────────────────────────────────────────
        # Phase 3: DONE
        # ─────────────────────────────────────────────────────────────

        logger.info(
            "UPLOAD DONE: sending final request, total=%d bytes",
            sent
        )

        with self._lock:

            if not self._connected or self._sock is None:
                raise NetworkError(
                    "Connection lost before upload completion"
                )

            send_message(self._sock, {
                "cmd": CMD_UPLOAD_DONE,
                "upload_id": upload_id
            })

            logger.info(
                "UPLOAD DONE: request sent, waiting for response"
            )

            resp = self._recv_non_notify()

            logger.info(
                "UPLOAD DONE: response received: %s",
                resp
            )

        self._check(resp)

        if progress_cb:
            progress_cb(100)

        logger.info(
            "UPLOAD COMPLETE: %s (%d bytes)",
            filename,
            sent
        )

        return resp

    # ── Download ──────────────────────────────────────────────────────────────

    def download_file(self, node_id: int, save_dir: str,
                      progress_cb=None,
                      password: str = "") -> str:

        # META
        payload: dict = {
            "cmd": CMD_DOWNLOAD_INIT,
            "node_id": node_id
        }

        if password:
            payload["password_hash"] = hash_password(password)

        with self._lock:

            send_message(self._sock, payload)

            meta = self._recv_non_notify()

        self._check(meta)

        filename = meta["filename"]
        file_size = meta["file_size"] or 0

        save_path = os.path.join(
            save_dir,
            filename
        )

        # Phase 2: CHUNKS

        offset = 0

        with open(save_path, "wb") as f:

            while True:

                with self._lock:

                    send_message(self._sock, {
                        "cmd": CMD_DOWNLOAD_CHUNK,
                        "node_id": node_id,
                        "offset": offset,
                    })

                    chunk_msg = self._recv_non_notify()

                self._check(chunk_msg)

                data = base64.b64decode(
                    chunk_msg["data"]
                )

                f.write(data)

                offset += len(data)

                if progress_cb and file_size > 0:
                    progress_cb(
                        min(
                            100,
                            int(offset * 100 / file_size)
                        )
                    )

                if chunk_msg.get("eof"):
                    break

        if progress_cb:
            progress_cb(100)

        return save_path

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _request(self, payload: dict) -> dict:
        logger.info(
            "REQUEST: %s",
            payload.get("cmd")
        )

        with self._lock:

            logger.info(
                "REQUEST %s: lock acquired",
                payload.get("cmd")
            )

            if not self._connected or self._sock is None:
                raise NetworkError(
                    "Not connected to server"
                )

            send_message(
                self._sock,
                payload
            )

            logger.info(
                "REQUEST %s: sent, waiting for response",
                payload.get("cmd")
            )

            resp = self._recv_non_notify()

            logger.info(
                "REQUEST %s: response=%s",
                payload.get("cmd"),
                resp
            )

        self._check(resp)

        return resp

    def _recv_non_notify(self) -> dict:
        while True:

            msg = recv_message(self._sock)

            if msg is None:
                raise NetworkError(
                    "Connection closed by server"
                )

            if msg.get("type") == RESP_NOTIFY:

                if self._notify_cb:

                    try:
                        self._notify_cb(
                            msg.get("event", {})
                        )

                    except Exception as exc:

                        logger.warning(
                            "Notification callback error: %s",
                            exc
                        )

                continue

            return msg

    @staticmethod
    def _check(resp: dict):

        if resp.get("type") == RESP_ERROR:

            raise NetworkError(
                resp.get(
                    "message",
                    "Server error"
                )
            )