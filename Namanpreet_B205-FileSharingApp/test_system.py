import sys
import os
import time
import threading
import tempfile
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from server.server import FileShareServer
from client.network import NetworkClient, NetworkError
from shared.protocol import DEFAULT_HOST

TEST_PORT = 9001


def start_server():
    """Start server in a daemon thread."""
    server = FileShareServer(DEFAULT_HOST, TEST_PORT)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    time.sleep(0.5)   # wait for bind
    return server


def make_client() -> NetworkClient:
    c = NetworkClient()
    c.connect(DEFAULT_HOST, TEST_PORT)
    return c


def write_temp_file(name: str, content: bytes) -> str:
    p = Path(tempfile.gettempdir()) / name
    p.write_bytes(content)
    return str(p)


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print('─'*60)


def ok(msg: str):
    print(f"  ✓  {msg}")


def fail(msg: str):
    print(f"  ✗  {msg}")
    sys.exit(1)


def run_tests():
    section("Starting server")
    start_server()
    ok("Server started on port " + str(TEST_PORT))

    # ── Register 3 users ─────────────────────────────────────────────────────
    section("User registration & login")
    users = {}
    for name, pwd in [("alice", "alicepwd!1"), ("bob", "b0bsecret"), ("carol", "Car0l#pw")]:
        c = make_client()
        c.register(name, pwd)
        c.login(name, pwd)
        users[name] = (c, pwd)
        ok(f"Registered & logged in: {name}")

    alice_c, _ = users["alice"]
    bob_c, _   = users["bob"]
    carol_c, _ = users["carol"]

    # ── Root directory listing ────────────────────────────────────────────────
    section("Root directory listing")
    items = alice_c.list_dir(1)
    ok(f"Root listing returned {len(items)} items (initially empty)")

    # ── Create folders ────────────────────────────────────────────────────────
    section("Folder creation")
    resp = alice_c.create_folder(1, "Documents")
    docs_id = resp["id"]
    ok(f"Alice created 'Documents' (id={docs_id})")

    resp = alice_c.create_folder(1, "SecureVault", password="vaultpass")
    vault_id = resp["id"]
    ok(f"Alice created 'SecureVault' (id={vault_id}, password-protected)")

    resp = bob_c.create_folder(1, "BobStuff")
    bob_folder_id = resp["id"]
    ok(f"Bob created 'BobStuff' (id={bob_folder_id})")

    # Nested folder
    resp = alice_c.create_folder(docs_id, "Subdir_Level1")
    sub1_id = resp["id"]
    resp = alice_c.create_folder(sub1_id, "Subdir_Level2")
    sub2_id = resp["id"]
    ok(f"Recursive folders: Documents/Subdir_Level1/Subdir_Level2 (ids={sub1_id},{sub2_id})")

    # Duplicate name rejection
    try:
        alice_c.create_folder(1, "Documents")
        fail("Duplicate folder name should have been rejected")
    except NetworkError:
        ok("Duplicate folder name correctly rejected")

    # ── File upload ───────────────────────────────────────────────────────────
    section("File upload")

    # Alice uploads a text file to Documents
    content_a = b"Hello from Alice! " * 500
    tmp_a = write_temp_file("alice_report.txt", content_a)
    progress_pcts = []
    alice_c.upload_file(tmp_a, docs_id, progress_cb=lambda p: progress_pcts.append(p))
    ok(f"Alice uploaded alice_report.txt ({len(content_a)} bytes), "
       f"progress reported {len(progress_pcts)} updates")

    # Bob uploads a binary file
    content_b = os.urandom(131072)   # 128 KB random binary
    tmp_b = write_temp_file("bob_data.bin", content_b)
    bob_c.upload_file(tmp_b, bob_folder_id)
    ok("Bob uploaded bob_data.bin (128 KB binary)")

    # Carol uploads to root
    content_c = b"Carol's shared readme\n" * 100
    tmp_c = write_temp_file("readme.txt", content_c)
    carol_c.upload_file(tmp_c, 1)
    ok("Carol uploaded readme.txt to root")

    # ── Listing after uploads ─────────────────────────────────────────────────
    section("Directory listing after uploads")
    root_items = alice_c.list_dir(1)
    names = [i["name"] for i in root_items]
    ok(f"Root contains: {names}")
    assert "Documents" in names, "Documents missing from root"
    assert "readme.txt" in names, "readme.txt missing from root"

    docs_items = alice_c.list_dir(docs_id)
    ok(f"Documents contains: {[i['name'] for i in docs_items]}")

    # ── Download & integrity check ────────────────────────────────────────────
    section("File download & integrity")
    # Find alice_report node id.
    node = next(i for i in docs_items if i["name"] == "alice_report.txt")
    dl_dir = tempfile.gettempdir()
    saved = bob_c.download_file(node["id"], dl_dir)
    downloaded = Path(saved).read_bytes()
    assert downloaded == content_a, "Downloaded content does not match!"
    ok(f"Bob downloaded alice_report.txt — content integrity verified (SHA256 match)")

    # ── Search ────────────────────────────────────────────────────────────────
    section("Search")
    results = alice_c.search("alice")
    ok(f"Search 'alice' → {len(results)} result(s): {[r['name'] for r in results]}")
    assert any(r["name"] == "alice_report.txt" for r in results)

    results2 = carol_c.search("readme")
    ok(f"Search 'readme' → {len(results2)} result(s)")
    assert any(r["name"] == "readme.txt" for r in results2)

    results3 = bob_c.search("text")
    ok(f"Search 'text' (mime type) → {len(results3)} result(s)")

    # ── Password-protected folder ─────────────────────────────────────────────
    section("Password-protected folders")

    # Bob tries to list SecureVault without password.
    try:
        bob_c.list_dir(vault_id)
        fail("Should have required password")
    except NetworkError as exc:
        assert "PASSWORD_REQUIRED" in str(exc)
        ok("Bob correctly blocked from SecureVault without password")

    # Bob lists with correct password.
    items_vault = bob_c.list_dir(vault_id, password="vaultpass")
    ok(f"Bob listed SecureVault with correct password: {len(items_vault)} items")

    # Wrong password.
    try:
        bob_c.list_dir(vault_id, password="wrongpass")
        fail("Wrong password should be rejected")
    except NetworkError:
        ok("Wrong password correctly rejected")

    # ── Delete ────────────────────────────────────────────────────────────────
    section("Deletion & ownership")

    # Carol tries to delete Alice's file — should fail.
    try:
        carol_c.delete_item(node["id"])
        fail("Carol should not be able to delete Alice's file")
    except NetworkError:
        ok("Carol correctly blocked from deleting Alice's file")

    # Alice deletes her own file.
    alice_c.delete_item(node["id"])
    docs_items_after = alice_c.list_dir(docs_id)
    assert not any(i["name"] == "alice_report.txt" for i in docs_items_after)
    ok("Alice deleted her own file — no longer listed")

    # ── Metadata presence ─────────────────────────────────────────────────────
    section("File metadata")
    bob_items = bob_c.list_dir(bob_folder_id)
    bin_file = next(i for i in bob_items if i["name"] == "bob_data.bin")
    assert bin_file["file_size"] == len(content_b)
    assert bin_file["owner"] == "bob"
    assert bin_file["created_at"]
    assert bin_file["mime_type"]
    ok(f"bob_data.bin metadata: size={bin_file['file_size']}B, "
       f"type={bin_file['mime_type']}, owner={bin_file['owner']}")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("All tests passed ✓")
    print()


if __name__ == "__main__":
    run_tests()
