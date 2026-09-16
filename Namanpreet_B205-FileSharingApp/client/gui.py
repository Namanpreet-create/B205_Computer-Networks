import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
import os
import sys
import logging
from pathlib import Path
from datetime import datetime

# Allow running directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.network import NetworkClient, NetworkError
from shared.protocol import (
    DEFAULT_HOST, DEFAULT_PORT,
    NOTIFY_FILE_ADDED, NOTIFY_FILE_DELETED,
    NOTIFY_FOLDER_ADDED, NOTIFY_UPLOAD_PROG, NOTIFY_DOWNLOAD_PROG,
    hash_password,
)

logger = logging.getLogger("fileshare.client.gui")

# ── Colour palette
BG_DARK    = "#1a1d2e"      # deep navy — main background
BG_PANEL   = "#252841"      # slightly lighter panel
BG_ITEM    = "#2e3250"      # list item background
ACCENT     = "#5c6ef8"      # indigo accent
ACCENT2    = "#38d9a9"      # teal — success / upload
DANGER     = "#fc5c65"      # red — delete
TEXT_MAIN  = "#e8eaf6"      # near-white
TEXT_DIM   = "#9fa8da"      # muted label colour
BORDER     = "#3d4170"      # subtle border

FONT_HEAD  = ("Segoe UI", 18, "bold")
FONT_SUB   = ("Segoe UI", 10)
FONT_BODY  = ("Segoe UI", 10)
FONT_MONO  = ("Consolas",  9)
FONT_BTN   = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI",  8)

DOWNLOAD_DIR = str(Path.home() / "Downloads")


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d %b %Y  %H:%M")
    except ValueError:
        return iso


# ── Reusable styled widgets ─────────────────

def _btn(parent, text, command, bg=ACCENT, fg=TEXT_MAIN, **kw) -> tk.Button:
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=bg,
        fg=fg,
        relief="flat",
        cursor="hand2",
        activebackground=ACCENT2,
        activeforeground=BG_DARK,
        font=FONT_BTN,
        **kw
    )


def _label(parent, text=None, font=FONT_BODY, fg=TEXT_MAIN, **kw) -> tk.Label:
    if "bg" not in kw:
        kw["bg"] = parent.cget("bg")

    return tk.Label(
        parent,
        text=text,
        font=font,
        fg=fg,
        **kw
    )


def _entry(parent, **kw) -> tk.Entry:
    return tk.Entry(
        parent, bg=BG_ITEM, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
        relief="flat", font=FONT_BODY, **kw
    )


# ── Login / Register dialog ──────────────────────
class AuthWindow(tk.Toplevel):

    def __init__(self, master, on_success):
        super().__init__(master)
        self.title("FileShare — Sign In")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.grab_set()
        self._on_success = on_success
        self._client = None
        self._mode = tk.StringVar(value="login")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Close the authentication window safely."""
        self.grab_release()
        self.destroy()

    def _build(self):
        pad = {"padx": 36, "pady": 10}

        # Title
        _label(self, "FileShare", font=("Segoe UI", 22, "bold"),
               fg=ACCENT).pack(pady=(36, 4))
        _label(self, "Secure File Exchange Platform",
               font=FONT_SUB, fg=TEXT_DIM).pack(pady=(0, 24))

        # Connection fields
        conn_frame = tk.Frame(self, bg=BG_DARK)
        conn_frame.pack(**pad, fill="x")
        _label(conn_frame, "Server Host").grid(row=0, column=0, sticky="w")
        self._host_var = tk.StringVar(value=DEFAULT_HOST)
        _entry(conn_frame, textvariable=self._host_var, width=20).grid(
            row=0, column=1, padx=(8, 0))
        _label(conn_frame, "Port").grid(row=1, column=0, sticky="w", pady=(6,0))
        self._port_var = tk.StringVar(value=str(DEFAULT_PORT))
        _entry(conn_frame, textvariable=self._port_var, width=8).grid(
            row=1, column=1, padx=(8, 0), pady=(6,0), sticky="w")
        conn_frame.columnconfigure(1, weight=1)

        # Separator
        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=36, pady=12)

        # Mode selector
        mode_frame = tk.Frame(self, bg=BG_DARK)
        mode_frame.pack()
        for text, val in (("Sign In", "login"), ("Register", "register")):
            tk.Radiobutton(
                mode_frame, text=text, variable=self._mode, value=val,
                bg=BG_DARK, fg=TEXT_MAIN, selectcolor=BG_PANEL,
                activebackground=BG_DARK, font=FONT_BODY,
                command=self._on_mode_change,
            ).pack(side="left", padx=12)

        # Credentials
        cred_frame = tk.Frame(self, bg=BG_DARK)
        cred_frame.pack(**pad, fill="x")
        _label(cred_frame, "Username").grid(row=0, column=0, sticky="w")
        self._user_var = tk.StringVar()
        _entry(cred_frame, textvariable=self._user_var, width=28).grid(
            row=0, column=1, padx=(8,0))
        _label(cred_frame, "Password").grid(row=1, column=0, sticky="w", pady=(10,0))
        self._pass_var = tk.StringVar()
        _entry(cred_frame, textvariable=self._pass_var, show="●", width=28).grid(
            row=1, column=1, padx=(8,0), pady=(10,0))
        cred_frame.columnconfigure(1, weight=1)

        # Error label
        self._err_var = tk.StringVar()
        _label(self, textvariable=self._err_var, fg=DANGER,
               font=FONT_SMALL).pack()

        # Submit
        self._btn_submit = _btn(self, "Sign In", self._submit, width=20)
        self._btn_submit.pack(pady=(8, 24))

    def _on_mode_change(self):
        mode = self._mode.get()
        self._btn_submit.config(
            text="Sign In" if mode == "login" else "Register"
        )

    def _submit(self):
        host = self._host_var.get().strip()

        try:
            port = int(self._port_var.get().strip())
        except ValueError:
            return self._show_err("Invalid port number")

        username = self._user_var.get().strip()
        password = self._pass_var.get()

        if not username or not password:
            return self._show_err(
                "Username and password are required"
            )

        # Disable button while authentication is running
        self._btn_submit.config(
            state="disabled",
            text="Connecting…"
        )

        # Run network authentication in background thread
        threading.Thread(
            target=self._do_auth,
            args=(
                host,
                port,
                username,
                password,
                self._mode.get()
            ),
            daemon=True
        ).start()

    def _show_err(self, message):
        """Display an authentication error message."""
        self._err_var.set(str(message))
        self._btn_submit.config(
            state="normal",
            text="Sign In" if self._mode.get() == "login" else "Register"
        )

    def _do_auth(self, host, port, username, password, mode):
        client = NetworkClient()

        try:
            client.connect(host, port)

            if mode == "register":
                client.register(username, password)

            client.login(username, password)

            self.after(
                0,
                lambda c=client, u=username: self._success(c, u)
            )

        except NetworkError as exc:
            error_message = str(exc)
            self.after(
                0,
                lambda msg=error_message: self._show_err(msg)
            )

        except Exception as exc:
            error_message = f"Connection failed: {exc}"
            self.after(
                0,
                lambda msg=error_message: self._show_err(msg)
            )

    def _success(self, client, username):
        """Finish authentication and open the main FileShare window."""
        try:
            self.grab_release()
        except Exception:
            pass

        self.destroy()

        if self._on_success:
            self._on_success(client, username)


# ── Password prompt dialog ──────

class PasswordDialog(tk.Toplevel):
    def __init__(self, master, title="Enter Password"):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.grab_set()
        self.result: str | None = None
        _label(self, title, font=FONT_HEAD).pack(padx=24, pady=(20, 8))
        self._var = tk.StringVar()
        _entry(self, textvariable=self._var, show="●", width=28).pack(padx=24)
        _btn(self, "OK", self._ok).pack(pady=16)
        self.bind("<Return>", lambda _: self._ok())

    def _ok(self):
        self.result = self._var.get()
        self.destroy()


# ── New folder dialog ─────────

class NewFolderDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("New Folder")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self.grab_set()
        self.name: str | None = None
        self.password: str | None = None

        _label(self, "Folder Name", font=FONT_BODY).pack(padx=24, pady=(20, 4))
        self._name_var = tk.StringVar()
        _entry(self, textvariable=self._name_var, width=32).pack(padx=24)

        _label(self, "Password (optional — leave blank for public)",
               font=FONT_SMALL, fg=TEXT_DIM).pack(padx=24, pady=(12, 4))
        self._pwd_var = tk.StringVar()
        _entry(self, textvariable=self._pwd_var, show="●", width=32).pack(padx=24)

        _btn(self, "Create Folder", self._ok).pack(pady=20)
        self.bind("<Return>", lambda _: self._ok())

    def _ok(self):
        self.name = self._name_var.get().strip()
        self.password = self._pwd_var.get().strip()
        self.destroy()


# ── Main application window ─────

class FileShareApp(tk.Tk):
    """Root window — shown after successful login."""

    def __init__(self):
        super().__init__()
        self.title("FileShare")
        self.configure(bg=BG_DARK)
        self.geometry("1100x680")
        self.minsize(900, 560)

        self._client: NetworkClient | None = None
        self._username = ""
        self._current_parent_id = 1       # root folder id
        self._breadcrumb: list[tuple] = []  # [(id, name), …]
        self._folder_passwords: dict[int, str] = {}  # cached hashes

        # Build UI skeleton (empty until login).
        self._build_placeholder()
        self.after(100, self._open_auth)

    # ── Placeholder ────────

    def _build_placeholder(self):
        self._splash = tk.Frame(self, bg=BG_DARK)
        self._splash.place(relx=.5, rely=.5, anchor="center")
        _label(self._splash, "FileShare", font=("Segoe UI", 36, "bold"),
               fg=ACCENT).pack()
        _label(self._splash, "Connecting…", font=FONT_SUB, fg=TEXT_DIM).pack(pady=8)

    def _open_auth(self):
        AuthWindow(self, on_success=self._on_login)

    def _on_login(self, client: NetworkClient, username: str):
        self._client = client
        self._username = username
        if hasattr(self, "_splash"):
            self._splash.destroy()
        self._build_main_ui()
        # Subscribe for real-time notifications.
        threading.Thread(
            target=lambda: client.subscribe(self._on_notify),
            daemon=True
        ).start()
        self._load_dir(1)

    # ── Main UI ──────

    def _build_main_ui(self):
        # ── Header ──────
        header = tk.Frame(self, bg=BG_PANEL, height=52)
        header.pack(fill="x")
        header.pack_propagate(False)

        _label(header, "⟁ FileShare", font=("Segoe UI", 16, "bold"),
               fg=ACCENT, bg=BG_PANEL).pack(side="left", padx=20, pady=10)

        # Search bar in header
        search_frame = tk.Frame(header, bg=BG_PANEL)
        search_frame.pack(side="left", padx=30)
        self._search_var = tk.StringVar()
        search_entry = _entry(search_frame, textvariable=self._search_var, width=32)
        search_entry.pack(side="left")
        search_entry.bind("<Return>", lambda _: self._do_search())
        _btn(search_frame, "Search", self._do_search, padx=8, pady=3).pack(
            side="left", padx=6)

        # Right side of header
        right = tk.Frame(header, bg=BG_PANEL)
        right.pack(side="right", padx=16)
        _label(right, f"👤 {self._username}", fg=ACCENT2, bg=BG_PANEL,
               font=FONT_BODY).pack(side="left", padx=10)
        _btn(right, "Logout", self._logout, bg=DANGER, pady=3).pack(side="left")

        # ── Body ─────
        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True)

        # Left panel — folder navigation
        left = tk.Frame(body, bg=BG_PANEL, width=220)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        _label(left, "Folders", font=("Segoe UI", 11, "bold"),
               fg=TEXT_DIM, bg=BG_PANEL).pack(padx=16, pady=(16, 6), anchor="w")

        # Folder tree
        tree_frame = tk.Frame(left, bg=BG_PANEL)
        tree_frame.pack(fill="both", expand=True, padx=8)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Folder.Treeview",
            background=BG_PANEL, fieldbackground=BG_PANEL,
            foreground=TEXT_MAIN, rowheight=26,
            borderwidth=0, font=FONT_BODY,
        )
        style.map("Folder.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#fff")])

        self._folder_tree = ttk.Treeview(
            tree_frame, style="Folder.Treeview",
            selectmode="browse", show="tree",
        )
        self._folder_tree.pack(fill="both", expand=True)
        self._folder_tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._folder_tree.bind("<Double-1>", self._on_tree_double)

        btn_frame = tk.Frame(left, bg=BG_PANEL)
        btn_frame.pack(fill="x", padx=8, pady=8)
        _btn(btn_frame, "+ New Folder", self._new_folder,
             bg=ACCENT, pady=4).pack(fill="x")

        # Right panel — file list + actions
        right_panel = tk.Frame(body, bg=BG_DARK)
        right_panel.pack(side="left", fill="both", expand=True)

        # Breadcrumb
        self._breadcrumb_frame = tk.Frame(right_panel, bg=BG_DARK)
        self._breadcrumb_frame.pack(fill="x", padx=16, pady=(10, 4))

        # Toolbar
        toolbar = tk.Frame(right_panel, bg=BG_DARK)
        toolbar.pack(fill="x", padx=12, pady=(0, 6))
        _btn(toolbar, "⬆ Upload", self._upload_file, bg=ACCENT2,
             fg=BG_DARK).pack(side="left", padx=4)
        _btn(toolbar, "⬇ Download", self._download_selected,
             bg=ACCENT).pack(side="left", padx=4)
        _btn(toolbar, "🗑 Delete", self._delete_selected,
             bg=DANGER).pack(side="left", padx=4)
        _btn(toolbar, " Set Password", self._set_password_on_node,
             bg=BG_ITEM, fg=ACCENT).pack(side="left", padx=4)
        _btn(toolbar, "↻ Refresh", lambda: self._load_dir(self._current_parent_id),
             bg=BG_ITEM, fg=TEXT_DIM).pack(side="right", padx=4)

        # File list (Treeview used as table)
        cols = ("name", "type", "size", "owner", "created")
        col_headers = ("Name", "Type", "Size", "Owner", "Created")
        col_widths   = (280, 100, 80, 100, 160)

        style.configure(
            "File.Treeview",
            background=BG_PANEL, fieldbackground=BG_PANEL,
            foreground=TEXT_MAIN, rowheight=28,
            borderwidth=0, font=FONT_BODY,
        )
        style.configure("File.Treeview.Heading",
                         background=BG_ITEM, foreground=TEXT_DIM,
                         font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("File.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "#fff")])

        list_frame = tk.Frame(right_panel, bg=BG_DARK)
        list_frame.pack(fill="both", expand=True, padx=12)

        self._file_list = ttk.Treeview(
            list_frame, style="File.Treeview",
            columns=cols, show="headings", selectmode="browse",
        )
        for col, hdr, w in zip(cols, col_headers, col_widths):
            self._file_list.heading(col, text=hdr)
            self._file_list.column(col, width=w, anchor="w")

        vsb = ttk.Scrollbar(list_frame, orient="vertical",
                             command=self._file_list.yview)
        self._file_list.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._file_list.pack(fill="both", expand=True)
        self._file_list.bind("<Double-1>", self._on_file_double)

        # ── Status bar ────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Ready")
        self._status_bar = tk.Frame(self, bg=BG_PANEL, height=28)
        self._status_bar.pack(fill="x", side="bottom")
        self._status_bar.pack_propagate(False)
        _label(self._status_bar, textvariable=self._status_var,
               font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL).pack(
            side="left", padx=16, pady=4)

        # Progress bar (hidden by default)
        self._progress = ttk.Progressbar(
            self._status_bar, orient="horizontal", length=200, mode="determinate"
        )
        self._progress.pack(side="right", padx=16, pady=4)
        self._progress.pack_forget()

        # Tag for folders vs files in file list
        self._file_list.tag_configure("folder", foreground="#ffd166")
        self._file_list.tag_configure("protected", foreground="#f4acb7")

        # Map iid → node data
        self._node_map: dict[str, dict] = {}

        # Tree root insertion
        self._tree_root_id = self._folder_tree.insert(
            "", "end", iid="1", text="📁 / (root)", open=True, values=(1,)
        )

    # ── Directory loading ─────────────────────────────────────────────────────

    def _load_dir(self, parent_id: int, password: str = ""):
        self._status("Loading…")
        threading.Thread(
            target=self._bg_load_dir,
            args=(parent_id, password),
            daemon=True
        ).start()

    def _bg_load_dir(self, parent_id: int, password: str):
        try:
            if password:
                self._folder_passwords[parent_id] = hash_password(password)
            pwd_hash_arg = ""
            if parent_id in self._folder_passwords:
                pwd_hash_arg = ""   # Already stored; server checks via hash
            items = self._client.list_dir(parent_id, password)
            self.after(0, lambda: self._populate_file_list(parent_id, items))
        except NetworkError as exc:
            msg = str(exc)
            if msg == "PASSWORD_REQUIRED":
                self.after(0, lambda: self._prompt_folder_password(parent_id))
            else:
                self.after(0, lambda: self._status(f"Error: {msg}", error=True))


    def _prompt_folder_password(self, parent_id: int):
        dlg = PasswordDialog(self, title="Folder is Password Protected")
        self.wait_window(dlg)
        if dlg.result is not None:
            self._load_dir(parent_id, dlg.result)

    def _populate_file_list(self, parent_id: int, items: list[dict]):
        self._current_parent_id = parent_id
        self._file_list.delete(*self._file_list.get_children())
        self._node_map.clear()
        self._update_breadcrumb()

        for item in items:
            icon = "📁" if item["node_type"] == "folder" else "📄"
            lock = "🔒 " if item.get("is_protected") else ""
            name_display = f"{icon} {lock}{item['name']}"
            size_display = _fmt_size(item["file_size"]) if item["node_type"] == "file" else "—"
            mime = item.get("mime_type") or "—"
            tag = "folder" if item["node_type"] == "folder" else ""
            if item.get("is_protected"):
                tag = "protected"

            iid = str(item["id"])
            self._file_list.insert(
                "", "end", iid=iid,
                values=(name_display, mime, size_display,
                        item["owner"], _fmt_dt(item["created_at"])),
                tags=(tag,),
            )
            self._node_map[iid] = item

        # Update left panel tree — add folder children
        self._refresh_tree_node(parent_id, items)
        count = len(items)
        self._status(f"{count} item{'s' if count != 1 else ''} in this folder")

    # ── Folder tree (left panel) ──────────────────────────────────────────────

    def _refresh_tree_node(self, parent_id: int, items: list[dict]):
        """Update tree children for *parent_id*."""
        tree_parent = str(parent_id)
        if not self._folder_tree.exists(tree_parent):
            return
        # Remove old children.
        for child in self._folder_tree.get_children(tree_parent):
            self._folder_tree.delete(child)
        # Re-add folders.
        for item in items:
            if item["node_type"] == "folder":
                lock = "🔒" if item.get("is_protected") else ""
                self._folder_tree.insert(
                    tree_parent, "end",
                    iid=str(item["id"]),
                    text=f"📁 {lock}{item['name']}",
                    values=(item["id"],),
                    open=False,
                )

    def _on_tree_select(self, _evt):
        sel = self._folder_tree.selection()
        if not sel:
            return
        node_id = int(sel[0])
        if node_id != self._current_parent_id:
            self._load_dir(node_id)

    def _on_tree_double(self, _evt):
        self._on_tree_select(_evt)

    # ── Breadcrumb ────────────────────────────────────────────────────────────

    def _update_breadcrumb(self):
        for w in self._breadcrumb_frame.winfo_children():
            w.destroy()
        # Build breadcrumb from root.
        crumbs = [("Root (1)", 1)] + self._breadcrumb
        for i, (name, nid) in enumerate(crumbs):
            if i > 0:
                _label(self._breadcrumb_frame, " › ", fg=TEXT_DIM).pack(side="left")
            btn = tk.Button(
                self._breadcrumb_frame, text=name,
                bg=BG_DARK, fg=ACCENT, font=FONT_SMALL,
                relief="flat", cursor="hand2",
                command=lambda n=nid: self._navigate_breadcrumb(n),
            )
            btn.pack(side="left")

    def _navigate_breadcrumb(self, node_id: int):
        # Trim breadcrumb to this node.
        idx = next((i for i, (_, nid) in enumerate(self._breadcrumb)
                    if nid == node_id), -1)
        if idx == -1:
            self._breadcrumb.clear()
        else:
            self._breadcrumb = self._breadcrumb[:idx + 1]
        self._load_dir(node_id)

    # ── File list interactions ────────────────────────────────────────────────

    def _on_file_double(self, _evt):
        sel = self._file_list.selection()
        if not sel:
            return
        node = self._node_map.get(sel[0])
        if not node:
            return
        if node["node_type"] == "folder":
            self._breadcrumb.append((node["name"], node["id"]))
            # Select in left tree.
            if self._folder_tree.exists(str(node["id"])):
                self._folder_tree.selection_set(str(node["id"]))
            self._load_dir(node["id"])
        else:
            self._download_node(node)

    def _selected_node(self) -> dict | None:
        sel = self._file_list.selection()
        if not sel:
            messagebox.showinfo("No selection", "Please select an item first.")
            return None
        return self._node_map.get(sel[0])

    # ── Upload ────────────────────────────────────────────────────────────────

    def _upload_file(self):
        paths = filedialog.askopenfilenames(title="Select files to upload")
        if not paths:
            return
        for path in paths:
            threading.Thread(
                target=self._bg_upload, args=(path,), daemon=True
            ).start()

    def _bg_upload(self, local_path: str):
        filename = Path(local_path).name
        self.after(0, lambda: self._status(f"Uploading {filename}…"))
        self.after(0, self._show_progress)
        try:
            def prog(pct):
                self.after(0, lambda: self._set_progress(pct))
                self.after(0, lambda: self._status(f"Uploading {filename}… {pct}%"))

            self._client.upload_file(local_path, self._current_parent_id, prog)
            self.after(0, lambda: self._status(f"✓ {filename} uploaded"))
            self.after(0, self._hide_progress)
            self.after(0, lambda: self._load_dir(self._current_parent_id))
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Upload Error", str(exc)))
            self.after(0, self._hide_progress)

    # ── Download ──────────────────────────────────────────────────────────────

    def _download_selected(self):
        node = self._selected_node()
        if node and node["node_type"] == "file":
            self._download_node(node)

    def _download_node(self, node: dict):
        save_dir = filedialog.askdirectory(title="Choose download location",
                                           initialdir=DOWNLOAD_DIR)
        if not save_dir:
            return
        threading.Thread(
            target=self._bg_download, args=(node, save_dir), daemon=True
        ).start()

    def _bg_download(self, node: dict, save_dir: str):
        self.after(0, lambda: self._status(f"Downloading {node['name']}…"))
        self.after(0, self._show_progress)
        try:
            def prog(pct):
                self.after(0, lambda: self._set_progress(pct))
                self.after(0, lambda: self._status(
                    f"Downloading {node['name']}… {pct}%"))

            saved = self._client.download_file(
                node["id"], save_dir, progress_cb=prog)
            self.after(0, lambda: self._status(f"✓ Saved to {saved}"))
            self.after(0, self._hide_progress)
        except NetworkError as exc:
            msg = str(exc)
            if msg == "PASSWORD_REQUIRED":
                self.after(0, lambda: self._prompt_download_password(node, save_dir))
            else:
                self.after(0, lambda: messagebox.showerror("Download Error", msg))
                self.after(0, self._hide_progress)

    def _prompt_download_password(self, node: dict, save_dir: str):
        dlg = PasswordDialog(self, title="Enter folder password")
        self.wait_window(dlg)
        if dlg.result is not None:
            threading.Thread(
                target=self._bg_download_with_pwd,
                args=(node, save_dir, dlg.result),
                daemon=True
            ).start()

    def _bg_download_with_pwd(self, node: dict, save_dir: str, pwd: str):
        self.after(0, self._show_progress)
        try:
            def prog(pct):
                self.after(0, lambda: self._set_progress(pct))
            saved = self._client.download_file(
                node["id"], save_dir, progress_cb=prog, password=pwd)
            self.after(0, lambda: self._status(f"✓ Saved to {saved}"))
            self.after(0, self._hide_progress)
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Download Error", str(exc)))
            self.after(0, self._hide_progress)

    # ── Delete ────────────────────────────────────────────────────────────────

    def _delete_selected(self):
        node = self._selected_node()
        if not node:
            return
        if not messagebox.askyesno(
            "Confirm Delete",
            f"Delete '{node['name']}'? This cannot be undone."
        ):
            return
        threading.Thread(
            target=self._bg_delete, args=(node,), daemon=True
        ).start()

    def _bg_delete(self, node: dict):
        try:
            self._client.delete_item(node["id"])
            self.after(0, lambda: self._status(f"✓ Deleted {node['name']}"))
            self.after(0, lambda: self._load_dir(self._current_parent_id))
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Delete Error", str(exc)))

    # ── New folder ────────────────────────────────────────────────────────────

    def _new_folder(self):
        dlg = NewFolderDialog(self)
        self.wait_window(dlg)
        if not dlg.name:
            return
        threading.Thread(
            target=self._bg_create_folder,
            args=(dlg.name, dlg.password or ""),
            daemon=True
        ).start()

    def _bg_create_folder(self, name: str, password: str):
        try:
            self._client.create_folder(self._current_parent_id, name, password)
            self.after(0, lambda: self._status(f"✓ Folder '{name}' created"))
            self.after(0, lambda: self._load_dir(self._current_parent_id))
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Error", str(exc)))

    # ── Set password on node ──────────────────────────────────────────────────

    def _set_password_on_node(self):
        node = self._selected_node()
        if not node:
            return
        dlg = PasswordDialog(self, title=f"Set password for '{node['name']}'")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        threading.Thread(
            target=self._bg_set_password,
            args=(node["id"], dlg.result),
            daemon=True
        ).start()

    def _bg_set_password(self, node_id: int, password: str):
        try:
            self._client.set_password(node_id, password)
            msg = "Password set" if password else "Password removed"
            self.after(0, lambda: self._status(f"✓ {msg}"))
            self.after(0, lambda: self._load_dir(self._current_parent_id))
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Error", str(exc)))

    # ── Search ────────────────────────────────────────────────────────────────

    def _do_search(self):
        query = self._search_var.get().strip()
        if not query:
            return
        threading.Thread(target=self._bg_search, args=(query,), daemon=True).start()

    def _bg_search(self, query: str):
        try:
            results = self._client.search(query)
            self.after(0, lambda: self._show_search_results(query, results))
        except NetworkError as exc:
            self.after(0, lambda: messagebox.showerror("Search Error", str(exc)))

    def _show_search_results(self, query: str, results: list[dict]):
        self._file_list.delete(*self._file_list.get_children())
        self._node_map.clear()
        for item in results:
            lock = "🔒 " if item.get("is_protected") else ""
            iid = str(item["id"])
            self._file_list.insert(
                "", "end", iid=iid,
                values=(f"📄 {lock}{item['name']}",
                        item.get("mime_type") or "—",
                        _fmt_size(item.get("file_size")),
                        item["owner"],
                        _fmt_dt(item.get("created_at"))),
                tags=("file",),
            )
            self._node_map[iid] = item
        self._status(f'{len(results)} result(s) for "{query}"')

    # ── Real-time notifications ───────────────────────────────────────────────

    def _on_notify(self, event: dict):
        """Called from background listener thread — dispatch to main thread."""
        self.after(0, lambda: self._process_notify(event))

    def _process_notify(self, event: dict):
        etype = event.get("event_type", "")
        if etype == NOTIFY_FILE_ADDED:
            name = event.get("name", "")
            owner = event.get("owner", "")
            self._toast(f"📄 {owner} added '{name}'")
            if event.get("parent_id") == self._current_parent_id:
                self._load_dir(self._current_parent_id)
        elif etype == NOTIFY_FOLDER_ADDED:
            name = event.get("name", "")
            owner = event.get("owner", "")
            self._toast(f"📁 {owner} created folder '{name}'")
            if event.get("parent_id") == self._current_parent_id:
                self._load_dir(self._current_parent_id)
        elif etype == NOTIFY_FILE_DELETED:
            name = event.get("name", "")
            self._toast(f"🗑 '{name}' was deleted")
            if event.get("parent_id") == self._current_parent_id:
                self._load_dir(self._current_parent_id)
        elif etype == NOTIFY_UPLOAD_PROG:
            pct = event.get("percent", 0)
            fname = event.get("filename", "")
            self._status(f"⬆ Uploading {fname}… {pct}%")
            self._show_progress()
            self._set_progress(pct)
            if pct >= 100:
                self._hide_progress()
        elif etype == NOTIFY_DOWNLOAD_PROG:
            pct = event.get("percent", 0)
            fname = event.get("filename", "")
            self._status(f"⬇ Downloading {fname}… {pct}%")

    def _toast(self, msg: str):
        """Temporary notification popup near bottom-right."""
        toast = tk.Toplevel(self)
        toast.overrideredirect(True)
        toast.configure(bg=BG_PANEL)
        toast.attributes("-alpha", 0.92)
        # Position near bottom right.
        x = self.winfo_x() + self.winfo_width() - 340
        y = self.winfo_y() + self.winfo_height() - 80
        toast.geometry(f"320x52+{x}+{y}")
        tk.Label(
            toast, text=msg, bg=BG_PANEL, fg=TEXT_MAIN,
            font=FONT_BODY, wraplength=300, padx=12, pady=8,
        ).pack(fill="both", expand=True)
        toast.after(4000, toast.destroy)

    # ── Status bar helpers ────────────────────────────────────────────────────

    def _status(self, msg: str, error: bool = False):
        self._status_var.set(msg)
        # Log level.
        if error:
            logger.warning(msg)
        else:
            logger.info(msg)

    def _show_progress(self):
        self._progress.pack(side="right", padx=16, pady=4)

    def _hide_progress(self):
        self._progress.pack_forget()
        self._progress["value"] = 0

    def _set_progress(self, pct: int):
        self._progress["value"] = pct

    # ── Logout ────────────────────────────────────────────────────────────────

    def _logout(self):
        if self._client:
            try:
                self._client.logout()
            except Exception:
                pass
            self._client.disconnect()
        self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                Path(__file__).parent.parent / "logs" / "client.log",
                encoding="utf-8"
            ),
        ],
    )
    app = FileShareApp()
    app.mainloop()


if __name__ == "__main__":
    main()