import sqlite3
import threading
import os
import logging
from datetime import datetime

logger = logging.getLogger("fileshare.db")

class Database:
    """Thread-safe wrapper around a SQLite database."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")   # concurrent reads
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._create_schema()
        logger.info("Database ready at %s", db_path)

    # ── Schema ──────────

    def _create_schema(self):
        with self._lock:
            cur = self._conn
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT    UNIQUE NOT NULL,
                    password_hash TEXT    NOT NULL,
                    created_at    TEXT    NOT NULL
                );

                -- fs_nodes: both files and folders live here.
                -- parent_id NULL  → root-level node.
                -- node_type: 'file' | 'folder'
                CREATE TABLE IF NOT EXISTS fs_nodes (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_id     INTEGER REFERENCES fs_nodes(id) ON DELETE CASCADE,
                    owner         TEXT    NOT NULL,
                    node_type     TEXT    NOT NULL CHECK(node_type IN ('file','folder')),
                    name          TEXT    NOT NULL,
                    disk_path     TEXT,           -- server-side path (files only)
                    file_size     INTEGER,        -- bytes  (files only)
                    mime_type     TEXT,           -- files only
                    created_at    TEXT    NOT NULL,
                    UNIQUE(parent_id, name)       -- unique names within a folder
                );

                CREATE TABLE IF NOT EXISTS node_passwords (
                    node_id       INTEGER PRIMARY KEY REFERENCES fs_nodes(id) ON DELETE CASCADE,
                    password_hash TEXT    NOT NULL
                );

                -- Seed a virtual root folder (id=1) used as the global root.
                INSERT OR IGNORE INTO fs_nodes(id, parent_id, owner, node_type, name, created_at)
                VALUES (1, NULL, 'system', 'folder', '/', datetime('now'));
            """)
            self._conn.commit()

    # ── User operations ────────────

    def create_user(self, username: str, password_hash: str) -> bool:
        """Return True on success, False if username already exists."""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                    (username, password_hash, datetime.utcnow().isoformat())
                )
                self._conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, username: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    # ── Node operations ──────────────

    def list_dir(self, parent_id: int) -> list[sqlite3.Row]:
        """Return all direct children of *parent_id*."""
        with self._lock:
            return self._conn.execute(
                """SELECT n.*, np.password_hash IS NOT NULL AS is_protected
                   FROM fs_nodes n
                   LEFT JOIN node_passwords np ON np.node_id = n.id
                   WHERE n.parent_id = ?
                   ORDER BY n.node_type DESC, n.name""",
                (parent_id,)
            ).fetchall()

    def get_node(self, node_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM fs_nodes WHERE id = ?", (node_id,)
            ).fetchone()

    def get_node_by_name(self, parent_id: int, name: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM fs_nodes WHERE parent_id = ? AND name = ?",
                (parent_id, name)
            ).fetchone()

    def create_folder(self, parent_id: int, name: str, owner: str) -> int | None:
        """Create folder; return new id or None if name collision."""
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT INTO fs_nodes (parent_id, owner, node_type, name, created_at)
                       VALUES (?,?,'folder',?,?)""",
                    (parent_id, owner, name, datetime.utcnow().isoformat())
                )
                self._conn.commit()
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def create_file_record(self, parent_id: int, name: str, owner: str,
                           disk_path: str, file_size: int, mime_type: str) -> int | None:
        """Insert a file node; return new id or None on collision."""
        try:
            with self._lock:
                cur = self._conn.execute(
                    """INSERT INTO fs_nodes
                         (parent_id, owner, node_type, name, disk_path, file_size, mime_type, created_at)
                       VALUES (?,?,'file',?,?,?,?,?)""",
                    (parent_id, owner, name, disk_path,
                     file_size, mime_type, datetime.utcnow().isoformat())
                )
                self._conn.commit()
                return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def delete_node(self, node_id: int) -> list[str]:
        """
        Recursively delete *node_id* and all descendants.
        Returns list of disk_path values that the caller must remove.
        """
        with self._lock:
            # Collect all descendant disk paths before deletion.
            paths = self._collect_disk_paths(node_id)
            # CASCADE handles children in the DB.
            self._conn.execute("DELETE FROM fs_nodes WHERE id = ?", (node_id,))
            self._conn.commit()
        return paths

    def _collect_disk_paths(self, node_id: int) -> list[str]:
        """BFS to collect disk_path of this node and all descendants (no lock)."""
        paths = []
        queue = [node_id]
        while queue:
            nid = queue.pop()
            row = self._conn.execute(
                "SELECT id, disk_path, node_type FROM fs_nodes WHERE id = ?", (nid,)
            ).fetchone()
            if row is None:
                continue
            if row["disk_path"]:
                paths.append(row["disk_path"])
            children = self._conn.execute(
                "SELECT id FROM fs_nodes WHERE parent_id = ?", (nid,)
            ).fetchall()
            queue.extend(c["id"] for c in children)
        return paths

    def search_files(self, query: str) -> list[sqlite3.Row]:
        """Full-text-style search on name, owner, mime_type."""
        q = f"%{query}%"
        with self._lock:
            return self._conn.execute(
                """SELECT n.*, np.password_hash IS NOT NULL AS is_protected
                   FROM fs_nodes n
                   LEFT JOIN node_passwords np ON np.node_id = n.id
                   WHERE n.node_type = 'file'
                     AND (n.name LIKE ? OR n.owner LIKE ? OR n.mime_type LIKE ?)
                   ORDER BY n.created_at DESC
                   LIMIT 100""",
                (q, q, q)
            ).fetchall()

    # ── Password protection ────

    def set_node_password(self, node_id: int, password_hash: str):
        with self._lock:
            self._conn.execute(
                """INSERT INTO node_passwords (node_id, password_hash) VALUES (?,?)
                   ON CONFLICT(node_id) DO UPDATE SET password_hash=excluded.password_hash""",
                (node_id, password_hash)
            )
            self._conn.commit()

    def get_node_password_hash(self, node_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash FROM node_passwords WHERE node_id = ?", (node_id,)
            ).fetchone()
        return row["password_hash"] if row else None

    def remove_node_password(self, node_id: int):
        with self._lock:
            self._conn.execute(
                "DELETE FROM node_passwords WHERE node_id = ?", (node_id,)
            )
            self._conn.commit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def node_path_parts(self, node_id: int) -> list[str]:
        """Return breadcrumb names from root to *node_id* (excluding root '/')."""
        parts = []
        nid = node_id
        while nid and nid != 1:
            row = self._conn.execute(
                "SELECT name, parent_id FROM fs_nodes WHERE id = ?", (nid,)
            ).fetchone()
            if row is None:
                break
            parts.append(row["name"])
            nid = row["parent_id"]
        return list(reversed(parts))

    def close(self):
        self._conn.close()
