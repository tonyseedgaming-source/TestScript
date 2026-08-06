"""
Enhanced Fallout Multiverse item scraper with Google Sheets sync and GitHub uploads.

Features:
- Scans Bundles and Workshop folders
- Syncs with Google Sheets (smart deduplication, no duplicate IDs)
- Uploads .dat and .asset files to GitHub repository
- Only updates if values have actually changed
- Uses official Fallout Multiverse category structure

Author: Tony Seed
2026
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import shutil
import hashlib
import base64

# Google Sheets and GitHub imports
try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
except ImportError:
    print("Installing Google Sheets API dependencies...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "google-auth-oauthlib", "google-auth-httplib2", "google-api-python-client"])
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

try:
    from github import Github, GithubException
except ImportError:
    print("Installing PyGithub...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "PyGithub"])
    from github import Github, GithubException

# Configuration
DEFAULT_BUNDLES_FOLDER = Path(r"D:\SteamLibrary\steamapps\common\Unturned\Bundles")
DEFAULT_WORKSHOP_FOLDER = Path(r"D:\SteamLibrary\steamapps\workshop\content\304930")
DEFAULT_IGNORED_FOLDERS = {"Mythics"}

# Folders under Bundles\Items to exclude by default
DEFAULT_EXCLUDE_ITEMS = {
    'Anniversary_10','Arid','Boxes','Buak','Cali2','ComboCrate2024','Dango','Elver','Elver2','Frost','GlacierArena','Halloween2024','Keys','Kuwait','Kuwait2','Limestone','PBS','RioRemastered'
}

# Ignore paths file (persistent)
IGNORE_FILE = Path(__file__).parent / "ignored_paths.txt"
IGNORED_PATHS: set[Path] = set()

# Load config from .env.txt
ENV_FILE = Path(__file__).parent / ".env.txt"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"

SPREADSHEET_ID = ""
GITHUB_TOKEN = ""
GITHUB_REPO = ""


def load_env_config():
    """Load configuration from .env.txt file."""
    global SPREADSHEET_ID, GITHUB_TOKEN, GITHUB_REPO
    
    if ENV_FILE.exists():
        with open(ENV_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()
                    if key == "SPREADSHEET_ID":
                        SPREADSHEET_ID = value
                    elif key == "GITHUB_TOKEN":
                        GITHUB_TOKEN = value
                    elif key == "GITHUB_REPO":
                        GITHUB_REPO = value


def create_default_ignore_file():
    """Create a default ignored_paths.txt populated with common paths if the file is missing.

    The file contains absolute paths (one per line). It will only write paths that
    actually exist on disk to avoid cluttering the file with non-existent folders.
    """
    lines: list[str] = [
        "# Ignored absolute folder paths for Unturned ID Scraper",
        "# One path per line. Edit this file in the GUI via Edit ignore paths or by editing this file directly.",
        "# Lines starting with # are ignored.",
        "",
    ]

    # Mythics folder under Bundles
    mythics = DEFAULT_BUNDLES_FOLDER / "Mythics"
    if mythics.exists():
        lines.append(str(mythics))

    # Default excluded item packs under Bundles\Items
    items_base = DEFAULT_BUNDLES_FOLDER / "Items"
    for name in sorted(DEFAULT_EXCLUDE_ITEMS):
        p = items_base / name
        if p.exists():
            lines.append(str(p))

    # Write the file only if we have any non-comment lines to add
    try:
        with open(IGNORE_FILE, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        # If writing fails, silently continue; load_ignored_paths will simply not load any
        pass


def load_ignored_paths():
    """Load ignore file with absolute folder paths to persist across runs.

    If the ignore file doesn't exist yet, create a default one populated with
    commonly ignored absolute paths (Mythics and default exclude item packs).
    """
    global IGNORED_PATHS
    if not IGNORE_FILE.exists():
        create_default_ignore_file()

    IGNORED_PATHS = set()
    if IGNORE_FILE.exists():
        try:
            with open(IGNORE_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    try:
                        IGNORED_PATHS.add(Path(line))
                    except Exception:
                        # ignore malformed lines
                        continue
        except Exception:
            pass

load_env_config()
load_ignored_paths()

# Fallout Multiverse Category Mapping
CATEGORY_MAPPING = {
    "Supplies": "Supplies",
    "Workshops": "Workshops",
    "Blueprints": "Blueprints",
    "Utilities": "Utilities",
    "Entertainment": "Entertainment",
    "Structures": "Structures",
    "Buildables": "Buildables",
    "Clothing": "Clothing",
    "Consumables": "Consumables",
    "Melee_Weapons": "Melee Weapons",
    "Melee": "Melee Weapons",
    "Guns": "Guns",
    "Attachments": "Attachments",
    "Ammunition": "Ammunition",
    "Ammo": "Ammunition",
    "Throwables": "Throwables",
    "Animals": "Animals",
    "Spawns": "Spawns",
    "Trees": "Trees",
    "Vehicles": "Vehicles",
    "Effects": "Effects",
    "Items": "Items",
    "Objects": "Objects",
    "Plants": "Plants",
    "Resources": "Resources",
}

ITEM_ID_RE = re.compile(r"^ID\s+(\d+)\s*(?://.*)?$")
GUID_RE = re.compile(r"^GUID\s+(.+?)\s*(?://.*)?$")
NAME_RE = re.compile(r"^Name\s+(.+)$")
DESCRIPTION_RE = re.compile(r"^Description\s+(.+)$")
TYPE_RE = re.compile(r"^Type\s+(\S+)", re.IGNORECASE)
HEADER_ROW = ["(Leave Empty)", "ID", "Name", "Description", "(Leave Empty)", "GUID"]


@dataclass(frozen=True)
class ItemRecord:
    """A single scraped item row."""
    category: str
    folder_name: str
    item_id: str
    name: str
    description: str
    guid: str
    dat_path: Optional[Path] = None


def normalize_names(names: Iterable[str]) -> set[str]:
    """Normalize folder names to a set for case-insensitive comparison."""
    return {name.strip().casefold() for name in names if name.strip()}


def should_ignore(path: Path, ignored_folders: set[str]) -> bool:
    """Return True when a path should be ignored.

    Checks (in order):
    - Explicit absolute ignored paths loaded from IGNORE_FILE
    - Folder name matches the ignored_folders set (case-insensitive)
    - Excluded top-level Bundles\Items folders listed in DEFAULT_EXCLUDE_ITEMS
    """
    # 1) Absolute path ignores
    for ignore_path in IGNORED_PATHS:
        try:
            # If path is inside an ignored path, skip it
            path.relative_to(ignore_path)
            return True
        except Exception:
            # Not relative
            if path == ignore_path:
                return True

    # 2) Name-based ignores
    if any(part.casefold() in ignored_folders for part in path.parts):
        return True

    # 3) Exclude specific folders under Bundles\Items
    try:
        rel = path.relative_to(DEFAULT_BUNDLES_FOLDER / "Items")
        # The first part under Items indicates the top-level asset pack folder
        if rel.parts and rel.parts[0] in DEFAULT_EXCLUDE_ITEMS:
            return True
    except Exception:
        pass

    return False


def find_asset_dat(path: Path) -> Path | None:
    """Return the best .dat metadata file for an asset folder.

    Items usually use ``FolderName/FolderName.dat`` plus ``English.dat``, while
    Vehicles, Trees, and Effects can use different .dat names or skip
    ``English.dat`` entirely. Prefer the matching folder-name .dat when it
    exists, otherwise use the first non-English .dat file in the folder.
    """
    matching_dat = path / f"{path.name}.dat"
    if matching_dat.is_file():
        return matching_dat

    try:
        dat_files = sorted(
            file
            for file in path.glob("*.dat")
            if file.name.casefold() not in {"english.dat", "masterbundle.dat"}
        )
    except OSError as error:
        print(f"Error scanning {path}: {error}")
        return None

    return dat_files[0] if dat_files else None


def is_item_folder(path: Path) -> bool:
    """Return True when a folder contains a scrapeable asset .dat file."""
    return find_asset_dat(path) is not None


def contains_item_folders(path: Path, ignored_folders: set[str]) -> bool:
    """Return True when a folder has at least one direct child asset folder."""
    try:
        children = path.iterdir()
    except OSError as error:
        print(f"Error scanning {path}: {error}")
        return False

    return any(
        child.is_dir() and not should_ignore(child, ignored_folders) and is_item_folder(child)
        for child in children
    )


def discover_export_folders(root_folders: Sequence[Path], ignored_folders: set[str]) -> list[Path]:
    """Find asset category folders that should become worksheets in the export workbook."""
    export_folders: list[Path] = []
    pending = deque()

    for folder in root_folders:
        if folder.exists():
            pending.append(folder)
        else:
            print(f"Skipping missing folder: {folder}")

    while pending:
        current = pending.popleft()
        if should_ignore(current, ignored_folders):
            continue

        if contains_item_folders(current, ignored_folders):
            export_folders.append(current)
            continue

        try:
            child_folders = sorted(child for child in current.iterdir() if child.is_dir())
        except OSError as error:
            print(f"Error scanning {current}: {error}")
            continue

        pending.extend(child for child in child_folders if not should_ignore(child, ignored_folders))

    return export_folders


def category_name(root_folder: Path, bundles_folder: Path) -> str:
    """Return a readable category name from a discovered folder path using official mapping."""
    try:
        parts = root_folder.relative_to(bundles_folder).parts
        if parts:
            folder_name = parts[0]
            # Try to map to official category
            if folder_name in CATEGORY_MAPPING:
                if len(parts) > 1:
                    return f"{CATEGORY_MAPPING[folder_name]} / {' / '.join(parts[1:])}"
                return CATEGORY_MAPPING[folder_name]
            # If not in mapping, use as-is
            return " / ".join(parts)
        return " / ".join(parts) if parts else root_folder.name
    except ValueError:
        return root_folder.name


def worksheet_title(title: str, used_titles: set[str]) -> str:
    """Create a safe, unique Excel worksheet title."""
    title = INVALID_SHEET_CHARS_RE.sub("-", title).strip() or "Items"
    title = title[:MAX_EXCEL_SHEET_NAME_LENGTH]
    candidate = title
    suffix = 2

    while candidate in used_titles:
        suffix_text = f" ({suffix})"
        candidate = f"{title[:MAX_EXCEL_SHEET_NAME_LENGTH - len(suffix_text)]}{suffix_text}"
        suffix += 1

    used_titles.add(candidate)
    return candidate


def read_item_metadata(item_dat: Path) -> tuple[str, str]:
    """Return the top-level numeric item ID and GUID from an item .dat file."""
    item_id = ""
    guid = ""
    block_depth = 0

    with item_dat.open("r", encoding="utf-8-sig", errors="ignore") as file:
        for raw_line in file:
            line = raw_line.strip()

            if block_depth == 0:
                item_id_match = ITEM_ID_RE.match(line)
                if item_id_match:
                    item_id = item_id_match.group(1)
                guid_match = GUID_RE.match(line)
                if guid_match:
                    guid = guid_match.group(1)

            if line.startswith("{"):
                block_depth += 1
            elif line.startswith("}"):
                block_depth -= 1

            if item_id and guid:
                break

    return item_id, guid


def find_asset_dat(path: Path) -> Path | None:
    """Return the best .dat metadata file for an asset folder.

    Items usually use ``FolderName/FolderName.dat`` plus ``English.dat``, while
    Vehicles, Trees, and Effects can use different .dat names or skip
    ``English.dat`` entirely. Prefer the matching folder-name .dat when it
    exists, otherwise use the first non-English .dat file in the folder.
    """
    matching_dat = path / f"{path.name}.dat"
    if matching_dat.is_file():
        return matching_dat

    try:
        dat_files = sorted(
            file
            for file in path.glob("*.dat")
            if file.name.casefold() not in {"english.dat", "masterbundle.dat"}
        )
    except OSError as error:
        print(f"Error scanning {path}: {error}")
        return None

    return dat_files[0] if dat_files else None


def find_asset_files(folder_path: Path) -> list[Path]:
    """Find all .dat and .asset files in the folder."""
    return sorted(
        list(folder_path.glob("*.dat")) + list(folder_path.glob("*.asset"))
    )


def read_english_metadata(english_dat: Path) -> tuple[str, str]:
    """Extract the Name and Description from English.dat file."""
    name = ""
    description = ""

    with english_dat.open("r", encoding="utf-8-sig", errors="ignore") as file:
        for raw_line in file:
            line = raw_line.strip()

            if not name:
                name_match = NAME_RE.match(line)
                if name_match:
                    name = name_match.group(1).strip()
                    continue

            if not description:
                description_match = DESCRIPTION_RE.match(line)
                if description_match:
                    description = description_match.group(1).strip()

    return name, description


def read_asset_record(folder_path: Path, category: str) -> ItemRecord | None:
    """Scrape one asset folder into an ItemRecord."""
    item_dat = find_asset_dat(folder_path)
    if item_dat is None:
        return None

    english_dat = folder_path / "English.dat"

    try:
        item_id, guid = read_item_metadata(item_dat)
    except OSError as error:
        print(f"Error reading {item_dat}: {error}")
        return None

    if english_dat.exists():
        try:
            name, description = read_english_metadata(english_dat)
        except OSError as error:
            print(f"Error reading {english_dat}: {error}")
            name = ""
            description = ""
    else:
        # Fall back to folder name with underscores replaced
        name = folder_path.name.replace("_", " ")
        description = ""

    return ItemRecord(
        category=category,
        folder_name=folder_path.name,
        item_id=item_id,
        name=name,
        description=description,
        guid=guid,
        dat_path=item_dat,
    )


def collect_item_records(
    root_folder: Path,
    bundles_folder: Path,
    ignored_folders: set[str],
) -> list[ItemRecord]:
    """Scrape all assets from one category folder."""
    records: list[ItemRecord] = []
    category = category_name(root_folder, bundles_folder)

    try:
        item_folders = sorted(child for child in root_folder.iterdir() if child.is_dir())
    except OSError as error:
        print(f"Error scanning {root_folder}: {error}")
        return records

    for folder_path in item_folders:
        if should_ignore(folder_path, ignored_folders) or not is_item_folder(folder_path):
            continue

        record = read_asset_record(folder_path, category)
        if record is not None:
            records.append(record)

    return sorted(records, key=lambda record: (record.name.casefold(), record.item_id))


def collect_selected_asset_records(
    asset_folders: Sequence[Path],
    root_folders: Sequence[Path],
    ignored_folders: set[str],
) -> list[ItemRecord]:
    """Scrape checked asset folders and return flat list of ItemRecords."""
    records: list[ItemRecord] = []

    for folder_path in sorted(asset_folders):
        if should_ignore(folder_path, ignored_folders) or not is_item_folder(folder_path):
            continue

        # find base root for this folder
        base_root = root_folders[0] if root_folders else folder_path
        for r in root_folders:
            try:
                folder_path.relative_to(r)
                base_root = r
                break
            except Exception:
                continue

        parent_folder = folder_path.parent
        category = category_name(parent_folder, base_root)
        record = read_asset_record(folder_path, category)
        if record is not None:
            records.append(record)

    return sorted(records, key=lambda record: (record.category, record.name.casefold(), record.item_id))


def launch_gui(default_bundles_folder: Path, default_workshop_folder: Path) -> None:
    """Launch a Tkinter app for tree-based scanning, Google Sheets sync, and GitHub upload."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    CHECKED = "☑"
    UNCHECKED = "☐"

    class ExportApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title("Unturned ID Scraper")
            self.bundles_folder = tk.StringVar(value=str(default_bundles_folder))
            self.workshop_folder = tk.StringVar(value=str(default_workshop_folder))
            self.ignore_text = tk.StringVar(value=", ".join(sorted(DEFAULT_IGNORED_FOLDERS)))
            self.status = tk.StringVar(value="Choose folders, then click Scan folders.")
            self.node_paths: dict[str, Path] = {}
            self.node_labels: dict[str, str] = {}
            self.asset_nodes: set[str] = set()
            self.checked_nodes: set[str] = set()

            self.frame = ttk.Frame(root, padding=12)
            self.frame.grid(row=0, column=0, sticky="nsew")
            root.columnconfigure(0, weight=1)
            root.rowconfigure(0, weight=1)
            # Make the treeview expand with the window
            self.frame.columnconfigure(0, weight=1)
            self.frame.columnconfigure(1, weight=1)
            self.frame.rowconfigure(4, weight=1)

            ttk.Label(self.frame, text="Bundles folder").grid(row=0, column=0, sticky="w")
            ttk.Entry(self.frame, textvariable=self.bundles_folder).grid(row=0, column=1, sticky="ew")
            ttk.Button(self.frame, text="Browse", command=self.choose_bundles_folder).grid(row=0, column=2)

            ttk.Label(self.frame, text="Workshop folder").grid(row=1, column=0, sticky="w")
            ttk.Entry(self.frame, textvariable=self.workshop_folder).grid(row=1, column=1, sticky="ew")
            ttk.Button(self.frame, text="Browse", command=self.choose_workshop_folder).grid(row=1, column=2)

            ttk.Label(self.frame, text="Ignore folders").grid(row=2, column=0, sticky="w")
            ttk.Entry(self.frame, textvariable=self.ignore_text).grid(row=2, column=1, sticky="ew")
            ttk.Label(self.frame, text="Comma-separated").grid(row=2, column=2, sticky="w")

            self.button_frame = ttk.Frame(self.frame)
            self.button_frame.grid(row=3, column=0, columnspan=3, sticky="w")
            ttk.Button(self.button_frame, text="Scan folders", command=self.scan_folders).grid(row=0, column=0)
            ttk.Button(self.button_frame, text="Sync to Sheets", command=self.sync_to_sheets).grid(row=0, column=1)
            ttk.Button(self.button_frame, text="Upload to GitHub", command=self.upload_to_github).grid(row=0, column=2)
            ttk.Button(self.button_frame, text="Check all", command=self.check_all).grid(row=0, column=3)
            ttk.Button(self.button_frame, text="Uncheck all", command=self.uncheck_all).grid(row=0, column=4)
            ttk.Button(self.button_frame, text="Edit ignore paths", command=self.edit_ignore_paths).grid(row=0, column=5)
            ttk.Button(self.button_frame, text="Import ignore paths", command=self.import_ignore_file).grid(row=0, column=6)
            ttk.Button(self.button_frame, text="Show ignore paths", command=self.show_ignored_paths).grid(row=0, column=7)

            self.tree = ttk.Treeview(self.frame, show="tree", selectmode="browse")
            self.scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=self.scrollbar.set)
            self.tree.grid(row=4, column=0, columnspan=2, sticky="nsew")
            self.scrollbar.grid(row=4, column=2, sticky="ns")
            self.tree.bind("<ButtonRelease-1>", self.toggle_clicked_node)
            self.tree.bind("<space>", self.toggle_selected_node)

            ttk.Label(self.frame, textvariable=self.status).grid(row=5, column=0, columnspan=3, sticky="w")

        def ignored_folders(self) -> set[str]:
            return normalize_names(self.ignore_text.get().split(","))

        def choose_bundles_folder(self) -> None:
            folder = filedialog.askdirectory(initialdir=self.bundles_folder.get())
            if folder:
                self.bundles_folder.set(folder)

        def choose_workshop_folder(self) -> None:
            folder = filedialog.askdirectory(initialdir=self.workshop_folder.get())
            if folder:
                self.workshop_folder.set(folder)

        def import_ignore_file(self) -> None:
            """Import a text file containing absolute folder paths to ignore (one per line).

            The selected file is copied to the internal ignore file location so it
            will be remembered on subsequent runs.
            """
            file = filedialog.askopenfilename(title="Select ignore paths file", filetypes=[("Text files","*.txt"), ("All files","*.*")])
            if not file:
                return
            try:
                shutil.copy(file, IGNORE_FILE)
                load_ignored_paths()
                messagebox.showinfo("Import complete", f"Imported ignores from {file}\nSaved to {IGNORE_FILE}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to import ignore file: {e}")

        def edit_ignore_paths(self) -> None:
            """Open a small editor to view and edit the persistent ignore file."""
            editor = tk.Toplevel(self.root)
            editor.title("Edit ignore paths")
            editor.geometry("700x400")

            txt = tk.Text(editor, wrap="none")
            txt.pack(fill="both", expand=True)

            # Load current contents (if any)
            try:
                if IGNORE_FILE.exists():
                    with open(IGNORE_FILE, 'r', encoding='utf-8') as f:
                        content = f.read()
                else:
                    content = "# Add absolute paths to ignore, one per line. Lines starting with # are ignored.\n"
                txt.insert('1.0', content)
            except Exception as e:
                txt.insert('1.0', f"# Error loading file: {e}\n")

            def do_save():
                try:
                    with open(IGNORE_FILE, 'w', encoding='utf-8') as f:
                        f.write(txt.get('1.0', 'end'))
                    load_ignored_paths()
                    messagebox.showinfo("Saved", f"Saved ignores to {IGNORE_FILE}")
                    editor.destroy()
                except Exception as e:
                    messagebox.showerror("Error", f"Failed to save ignore file: {e}")

            btn_frame = ttk.Frame(editor)
            btn_frame.pack(fill='x')
            ttk.Button(btn_frame, text='Save', command=do_save).pack(side='right', padx=6, pady=6)
            ttk.Button(btn_frame, text='Cancel', command=editor.destroy).pack(side='right', padx=6, pady=6)

        def show_ignored_paths(self) -> None:
            """Show currently loaded absolute ignored paths in a message box."""
            if not IGNORED_PATHS:
                messagebox.showinfo("Ignored paths", "No absolute ignored paths loaded.")
                return
            paths = "\n".join(str(p) for p in sorted(IGNORED_PATHS))
            messagebox.showinfo("Ignored paths", paths)

        def scan_folders(self) -> None:
            self.tree.delete(*self.tree.get_children())
            self.node_paths.clear()
            self.node_labels.clear()
            self.asset_nodes.clear()
            self.checked_nodes.clear()

            bundles_folder = Path(self.bundles_folder.get())
            workshop_folder = Path(self.workshop_folder.get())
            roots = [bundles_folder, workshop_folder]
            folders = discover_export_folders(roots, self.ignored_folders())

            for category_folder in folders:
                base_root = bundles_folder
                for r in roots:
                    try:
                        category_folder.relative_to(r)
                        base_root = r
                        break
                    except ValueError:
                        continue

                category_node = self.insert_path_nodes(category_folder, base_root)
                for asset_folder in self.asset_folders_for_category(category_folder):
                    asset_node = self.tree.insert(
                        category_node,
                        "end",
                        text=f"{CHECKED} {asset_folder.name}",
                        open=False,
                    )
                    self.node_paths[asset_node] = asset_folder
                    self.node_labels[asset_node] = asset_folder.name
                    self.asset_nodes.add(asset_node)
                    self.checked_nodes.add(asset_node)

            for node in self.tree.get_children():
                self.refresh_parent_checks(node)
                self.tree.item(node, open=True)

            self.status.set(f"Found {len(self.asset_nodes)} assets. Select items to sync, then click Sync to Sheets.")

        def insert_path_nodes(self, folder: Path, bundles_folder: Path) -> str:
            parent = ""
            current_path = bundles_folder
            for part in folder.relative_to(bundles_folder).parts:
                current_path = current_path / part
                existing = self.find_child(parent, current_path)
                if existing:
                    parent = existing
                    continue

                node = self.tree.insert(parent, "end", text=f"{CHECKED} {part}", open=True)
                self.node_paths[node] = current_path
                self.node_labels[node] = part
                self.checked_nodes.add(node)
                parent = node

            return parent

        def find_child(self, parent: str, path: Path) -> str:
            for child in self.tree.get_children(parent):
                if self.node_paths.get(child) == path:
                    return child
            return ""

        def asset_folders_for_category(self, category_folder: Path) -> list[Path]:
            try:
                children = sorted(child for child in category_folder.iterdir() if child.is_dir())
            except OSError as error:
                print(f"Error scanning {category_folder}: {error}")
                return []

            ignored = self.ignored_folders()
            return [child for child in children if not should_ignore(child, ignored) and is_item_folder(child)]

        def toggle_clicked_node(self, event: tk.Event) -> None:
            node = self.tree.identify_row(event.y)
            if node:
                self.toggle_node(node)

        def toggle_selected_node(self, event: tk.Event) -> str:
            selected = self.tree.selection()
            if selected:
                self.toggle_node(selected[0])
            return "break"

        def toggle_node(self, node: str) -> None:
            self.set_checked(node, node not in self.checked_nodes)
            self.refresh_ancestors(node)
            self.status.set(f"{len(self.selected_asset_folders())} assets checked for sync.")

        def set_checked(self, node: str, checked: bool) -> None:
            if checked:
                self.checked_nodes.add(node)
            else:
                self.checked_nodes.discard(node)
            self.update_node_text(node)

            for child in self.tree.get_children(node):
                self.set_checked(child, checked)

        def refresh_ancestors(self, node: str) -> None:
            parent = self.tree.parent(node)
            while parent:
                self.refresh_parent_checks(parent)
                parent = self.tree.parent(parent)

        def refresh_parent_checks(self, node: str) -> bool:
            children = self.tree.get_children(node)
            if children:
                checked = all(self.refresh_parent_checks(child) for child in children)
                if checked:
                    self.checked_nodes.add(node)
                else:
                    self.checked_nodes.discard(node)
                self.update_node_text(node)
                return checked

            self.update_node_text(node)
            return node in self.checked_nodes

        def update_node_text(self, node: str) -> None:
            icon = CHECKED if node in self.checked_nodes else UNCHECKED
            self.tree.item(node, text=f"{icon} {self.node_labels[node]}")

        def selected_asset_folders(self) -> list[Path]:
            return [self.node_paths[node] for node in self.asset_nodes if node in self.checked_nodes]

        def check_all(self) -> None:
            for node in self.tree.get_children():
                self.set_checked(node, True)
            self.status.set(f"{len(self.selected_asset_folders())} assets checked for sync.")

        def uncheck_all(self) -> None:
            for node in self.tree.get_children():
                self.set_checked(node, False)
            self.status.set("0 assets checked for sync.")

        def sync_to_sheets(self) -> None:
            selected = self.selected_asset_folders()
            if not selected:
                messagebox.showwarning("Nothing selected", "Scan first, then keep at least one asset checked.")
                return

            records = collect_selected_asset_records(
                selected,
                [Path(self.bundles_folder.get()), Path(self.workshop_folder.get())],
                self.ignored_folders(),
            )
            
            sync_to_google_sheets(records)
            self.status.set(f"Synced {len(records)} assets to Google Sheets.")
            messagebox.showinfo("Done", f"Synced {len(records)} assets to Google Sheets.")

        def upload_to_github(self) -> None:
            selected = self.selected_asset_folders()
            if not selected:
                messagebox.showwarning("Nothing selected", "Scan first, then keep at least one asset checked.")
                return

            files = collect_asset_files(selected, self.ignored_folders())
            if not files:
                messagebox.showwarning("No files", "No .dat or .asset files found in selected folders.")
                return

            count = upload_files_to_github(files)
            self.status.set(f"Uploaded {count} files to GitHub.")
            messagebox.showinfo("Done", f"Uploaded {count} files to GitHub.")

    root = tk.Tk()
    ExportApp(root)
    root.mainloop()


# ===========================
# GOOGLE SHEETS INTEGRATION
# ===========================

def get_google_sheets_service():
    """Authenticate and return Google Sheets service using OAuth."""
    if not CREDENTIALS_FILE.exists():
        print(f"Error: {CREDENTIALS_FILE} not found")
        return None

    try:
        creds = None
        token_file = CREDENTIALS_FILE.parent / 'token.json'
        
        # Load saved credentials
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(str(token_file))
        
        # Refresh or re-authenticate if needed
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                scopes = ["https://www.googleapis.com/auth/spreadsheets"]
                flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), scopes=scopes)
                creds = flow.run_local_server(port=0)
            
            # Save credentials for next run
            with open(token_file, 'w') as token:
                token.write(creds.to_json())
        
        service = build("sheets", "v4", credentials=creds)
        return service
    except Exception as e:
        print(f"Error authenticating with Google Sheets: {e}")
        return None


def sync_to_google_sheets(records: list[ItemRecord]) -> None:
    """Sync scraped records to Google Sheets using batched reads and a local GUID cache.

    This reduces API usage by:
    - Using values().batchGet to read all sheets in one request
    - Caching the GUID index for a short TTL (default 5 minutes)
    - Comparing by GUID only
    - Reusing fetched row counts to avoid extra reads when appending
    """
    if not SPREADSHEET_ID or not CREDENTIALS_FILE.exists():
        print("Google Sheets sync disabled: missing config or credentials")
        return

    service = get_google_sheets_service()
    if not service:
        return

    GUID_INDEX_FILE = Path(__file__).parent / "guid_index.json"
    CACHE_TTL = 300  # seconds

    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        sheets = spreadsheet.get('sheets', [])
        existing_sheet_names = [s['properties']['title'] for s in sheets]
        sheet_name_to_id = {s['properties']['title']: s['properties']['sheetId'] for s in sheets}

        now_ts = int(__import__('time').time())
        existing_guids: set[str] = set()
        sheet_row_counts: dict[str, int] = {}

        # Try load cache
        cache_valid = False
        if GUID_INDEX_FILE.exists():
            try:
                import json
                with open(GUID_INDEX_FILE, 'r', encoding='utf-8') as cf:
                    cache = json.load(cf)
                if isinstance(cache, dict) and 'timestamp' in cache and 'guids' in cache:
                    if now_ts - int(cache.get('timestamp', 0)) <= CACHE_TTL:
                        existing_guids = set(cache.get('guids', []))
                        # cached row counts optional
                        sheet_row_counts = cache.get('row_counts', {})
                        cache_valid = True
                        print(f"Loaded GUID cache ({len(existing_guids)} guids) from {GUID_INDEX_FILE}")
            except Exception:
                cache_valid = False

        # If cache not valid, perform a single batchGet for all sheets (A1:Z10000)
        batch_values = None
        if not cache_valid:
            ranges = [f"'{name}'!A1:Z10000" for name in existing_sheet_names]
            # Make a single batchGet call
            try:
                batch = service.spreadsheets().values().batchGet(spreadsheetId=SPREADSHEET_ID, ranges=ranges).execute()
                value_ranges = batch.get('valueRanges', [])
            except Exception as e:
                print(f"Error reading sheets (batchGet): {e}")
                return

            # Parse each returned range to find item sheets and build GUID set
            for vr in value_ranges:
                vr_range = vr.get('range', '')  # e.g. "Sheet1'!A1:Z10000" or "'Sheet Name'!A1:Z10000"
                # Extract sheet name from range (before the !)
                try:
                    left = vr_range.split('!')[0]
                    sheet_name = left.strip().strip("'")
                except Exception:
                    continue

                values = vr.get('values', [])
                if not values:
                    sheet_row_counts[sheet_name] = 0
                    continue

                # header row
                header_row = values[0]
                header_lc = [c.strip().lower() for c in header_row]
                if 'id' not in header_lc or 'guid' not in header_lc:
                    # not an item sheet
                    sheet_row_counts[sheet_name] = len(values)
                    continue

                id_idx = header_lc.index('id')
                name_idx = header_lc.index('name') if 'name' in header_lc else None
                guid_idx = header_lc.index('guid')

                # data rows follow
                data_rows = values[1:]
                for row in data_rows:
                    guid_val = ''
                    if len(row) > guid_idx:
                        guid_val = str(row[guid_idx]).strip().lower()
                    if guid_val:
                        existing_guids.add(guid_val)

                sheet_row_counts[sheet_name] = len(values)

            # Save cache
            try:
                import json
                cache = {'timestamp': now_ts, 'guids': sorted(existing_guids), 'row_counts': sheet_row_counts}
                with open(GUID_INDEX_FILE, 'w', encoding='utf-8') as cf:
                    json.dump(cache, cf)
                print(f"Saved GUID cache ({len(existing_guids)} guids) to {GUID_INDEX_FILE}")
            except Exception:
                pass

        else:
            print("Using recent GUID cache; skipping sheet downloads")

        print(f"Found {len(existing_guids)} existing GUIDs across sheets")

        # Find missing records by GUID-only comparison
        missing_records: list[ItemRecord] = []
        for rec in records:
            rec_guid = str(rec.guid).strip().lower()
            if not rec_guid or rec_guid in existing_guids:
                continue
            missing_records.append(rec)

        if not missing_records:
            print("Google Sheets sync complete: 0 records added (all GUIDs already exist)")
            return

        # Build workshop mapping (Modlist) from the already-fetched value_ranges if available,
        # otherwise do a single values.get for Modlist only (cheap).
        WORKSHOP_SOURCE_MAP = {
            '2850118461': 'UNV Test Pack',
            '2941229986': 'Resources from all Curated Assets! Trees, Foliage & Materials!',
            '741253727': "Pako's Objects Pack [FINAL] [Unturned 3.28+]",
            '2899489911': 'Objects from all Curated Maps! 10,650 Objects!',
            '3232252391': 'New Fallout CA NPCs',
            '1770244026': 'Fallout: New Vegas',
            '1688587902': 'Fallout : Broken Steel',
            '3307503997': 'Fallout LoneStar Bundle',
            '3069018295': 'Fallout California Repeatable Quests',
            '3057462723': 'Fallout : Vehicles',
            '2822523418': 'Fallout : Vault Clothing',
            '2819268928': 'Fallout : Junk and Crafting',
            '2229264867': 'Fallout : Energy Weapons',
            '3303582332': 'COMBINED SHELTER PACK [OPEN FOR USE]',
            '2741976114': 'City Expansion 2',
            '2946099784': 'City Expansion',
            '1655064299': '[3.28] Project Fallout Update 4',
            '3354594338': 'FMV Recipes',
            '3531617918': 'Generic FM NPCs',
            '3353889992': 'Multiverse PA Redone',
            '1232252658': 'More Farming Mod',
            '2143463292': "The Bunker Pack [Reuploaded] [PUBLIC]",
            '3722741198': 'California 2 Objects',
            '3253740707': 'Admin Tools Mod',
            '3490089430': 'Chinese Stealth Suit',
            '2069342497': "Survivor's Expansion",
            '1717095606': 'Creator Tools',
            '3561174947': 'YES! Solutions: Item Stacking',
            '3520029342': 'Fallout California',
            '877777769': 'Furniture Expansion',
            '3283020514': 'PK - Philippine Front Artillery',
            '3008320662': "Derpy's WW2 Explosives",
        }

        # Try to parse Modlist from batch results if we have them
        try:
            if not cache_valid:
                for vr in value_ranges:
                    vr_range = vr.get('range', '')
                    left = vr_range.split('!')[0]
                    sheet_name = left.strip().strip("'")
                    if sheet_name == 'Modlist':
                        values = vr.get('values', [])
                        # rows may include header; accept either order
                        for row in values[1:]:
                            if len(row) >= 2:
                                a = row[0].strip()
                                b = row[1].strip()
                                if b.isdigit():
                                    WORKSHOP_SOURCE_MAP[b] = a
                                elif a.isdigit():
                                    WORKSHOP_SOURCE_MAP[a] = b
                        break
            else:
                # cached path: fetch Modlist only
                if 'Modlist' in existing_sheet_names:
                    try:
                        modlist_res = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="'Modlist'!A2:B1000").execute()
                        for row in modlist_res.get('values', []):
                            if len(row) >= 2:
                                left = row[0].strip()
                                right = row[1].strip()
                                if right.isdigit():
                                    WORKSHOP_SOURCE_MAP[right] = left
                                elif left.isdigit():
                                    WORKSHOP_SOURCE_MAP[left] = right
                    except Exception:
                        pass
        except Exception:
            pass

        # Determine target sheet and source tag per record using dat Type and path (reuse existing code)
        BEHAVIOR_RE = re.compile(r'^(?:behaviou?r)\s+(\S+)', re.IGNORECASE)
        TYPE_SORT_MAP: dict[str, tuple[Optional[str], Optional[str]]] = {
            'arrest_ends': ('Utilities', 'Restraint Unlockers'),
            'arrest_end': ('Utilities', 'Restraint Unlockers'),
            'arrest_starts': ('Utilities', 'Restraints'),
            'arrest_start': ('Utilities', 'Restraints'),
            'backpacks': ('Clothing', 'Backpacks'),
            'backpack': ('Clothing', 'Backpacks'),
            'barrels': ('Attachments', 'Barrels'),
            'barrel': ('Attachments', 'Barrels'),
            'barricades': (None, None),
            'barricade': (None, None),
            'blueprints': (None, None),
            'blueprint': (None, None),
            'clouds': ('Utilities', 'Parachutes'),
            'cloud': ('Utilities', 'Parachutes'),
            'detonator': ('Utilities', 'Detonators'),
            'detonators': ('Utilities', 'Detonators'),
            'filters': ('Utilities', 'Filters'),
            'filter': ('Utilities', 'Filters'),
            'fishers': ('Utilities', 'Fishing Tools'),
            'fisher': ('Utilities', 'Fishing Tools'),
            'food': ('Consumables', 'Food'),
            'fuels': ('Utilities', 'Fuel Containers'),
            'fuel': ('Utilities', 'Fuel Containers'),
            'glasses': ('Clothing', 'Glasses'),
            'glass': ('Clothing', 'Glasses'),
            'grips': ('Attachments', 'Grips'),
            'grip': ('Attachments', 'Grips'),
            'growers': ('Workshops', 'Fertilizer'),
            'grower': ('Workshops', 'Fertilizer'),
            'guns': (None, None),
            'gun': (None, None),
            'hats': ('Clothing', 'Helmets'),
            'hat': ('Clothing', 'Helmets'),
            'magazines': (None, None),
            'magazine': (None, None),
            'maps': (None, None),
            'map': (None, None),
            'masks': ('Clothing', 'Masks'),
            'mask': ('Clothing', 'Masks'),
            'medical': ('Consumables', 'Medicals'),
            'melees': ('Melee Weapons', 'Melee'),
            'melee': ('Melee Weapons', 'Melee'),
            'optics': ('Utilities', 'Optics'),
            'optic': ('Utilities', 'Optics'),
            'pants': ('Clothing', 'Pants'),
            'pant': ('Clothing', 'Pants'),
            'refills': ('Utilities', 'Water Containers'),
            'refill': ('Utilities', 'Water Containers'),
            'shirts': ('Clothing', 'Shirts'),
            'shirt': ('Clothing', 'Shirts'),
            'sights': ('Attachments', 'Sights'),
            'sight': ('Attachments', 'Sights'),
            'structures': (None, None),
            'structure': (None, None),
            'supplies': (None, None),
            'supply': (None, None),
            'tacticals': ('Attachments', 'Tacticals'),
            'tactical': ('Attachments', 'Tacticals'),
            'throwables': (None, None),
            'throwable': (None, None),
            'tools': (None, None),
            'tool': (None, None),
            'vests': ('Clothing', 'Vests'),
            'vest': ('Clothing', 'Vests'),
            'water': ('Consumables', 'Water'),
            'spawn': ('Spawns', None),
            'spawns': ('Spawns', None),
        }

        def record_target_and_source_and_sub(r: ItemRecord) -> tuple[str, str, str]:
            # (function body copied as before to keep sorting rules) 
            target = 'Unsorted'
            source = ''
            subcat = ''

            EFFECT_SUBCATS = ['Ambience', 'Explosions', 'General', 'Guns', 'Impacts', 'TireMotion', 'Volumes', 'Weather']
            ASSET_SUBCATS = ['Airdrops', 'Landscapes', 'Levels', 'Material_Palettes', 'PhysicsMaterials', 'Roads', 'Songs', 'Tags', 'VehiclePhysicsProfiles', 'Weather', 'Zombie_Difficulty']

            try:
                if r.dat_path:
                    dat_path = Path(r.dat_path)
                    try:
                        dat_path.relative_to(DEFAULT_BUNDLES_FOLDER)
                        source = 'Vanilla'
                    except Exception:
                        try:
                            rel = dat_path.relative_to(DEFAULT_WORKSHOP_FOLDER)
                            parts = rel.parts
                            if parts:
                                mod_id = parts[0]
                                source = WORKSHOP_SOURCE_MAP.get(mod_id, mod_id)
                        except Exception:
                            source = ''

                    try:
                        t_val = None
                        behavior_val = None
                        has_name_or_desc = False
                        with open(dat_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                            for raw in f:
                                line = raw.strip()
                                if not t_val:
                                    m = TYPE_RE.match(line)
                                    if m:
                                        t_val = m.group(1).lower()
                                if not behavior_val:
                                    mb = BEHAVIOR_RE.match(line)
                                    if mb:
                                        behavior_val = mb.group(1).lower()
                                if NAME_RE.match(line) or DESCRIPTION_RE.match(line):
                                    has_name_or_desc = True

                        is_in_items = False
                        try:
                            dat_path.relative_to(DEFAULT_BUNDLES_FOLDER / 'Items')
                            is_in_items = True
                        except Exception:
                            is_in_items = False

                        english_exists = (dat_path.parent / 'English.dat').exists()

                        if is_in_items and not english_exists and not has_name_or_desc:
                            return 'Skins', source, ''

                        if t_val:
                            lookup = t_val.replace(' ', '_')
                            mapped = TYPE_SORT_MAP.get(lookup)
                            if mapped is None:
                                if lookup.endswith('s'):
                                    mapped = TYPE_SORT_MAP.get(lookup[:-1])
                                else:
                                    mapped = TYPE_SORT_MAP.get(lookup + 's')
                            if mapped:
                                cat, sub = mapped
                                if cat:
                                    return cat, source, sub or ''
                                else:
                                    return 'Unsorted', source, ''

                        if t_val == 'resource':
                            target = 'Resources'
                        elif t_val == 'vehicle':
                            target = 'Vehicles'
                        elif t_val in ('large', 'medium', 'small'):
                            target = 'Objects'
                            subcat = t_val.capitalize()
                        elif t_val in ('npc', 'dialogue', 'quest', 'vendor'):
                            target = 'NPCs'
                            subcat = t_val.capitalize()
                        elif t_val == 'effect':
                            target = 'Effects'

                        if t_val == 'animal':
                            target = 'Animals'
                            if behavior_val:
                                if behavior_val.startswith('off'):
                                    subcat = 'Hostile'
                                elif behavior_val.startswith('igno'):
                                    subcat = 'Passive'
                                elif behavior_val.startswith('def'):
                                    subcat = 'Afraid'

                        if target == 'Effects' and r.dat_path:
                            try:
                                ancestor = Path(r.dat_path).parent
                                while True:
                                    name = ancestor.name
                                    for candidate in EFFECT_SUBCATS:
                                        if name.lower() == candidate.lower():
                                            subcat = candidate
                                            raise StopIteration
                                    if ancestor == ancestor.parent:
                                        break
                                    ancestor = ancestor.parent
                            except StopIteration:
                                pass

                        if target == 'Unsorted' and r.dat_path:
                            try:
                                ancestor = Path(r.dat_path).parent
                                found_assets = False
                                while True:
                                    name = ancestor.name
                                    if name.lower() == 'assets':
                                        found_assets = True
                                    for candidate in ASSET_SUBCATS:
                                        if name.lower() == candidate.lower():
                                            subcat = candidate.replace('_', ' ')
                                            if found_assets:
                                                target = 'Assets'
                                            raise StopIteration
                                    if ancestor == ancestor.parent:
                                        break
                                    ancestor = ancestor.parent
                            except StopIteration:
                                pass

                    except Exception:
                        pass

            except Exception:
                pass

            return target, source, subcat

        # Group missing records by target sheet
        sheet_groups: dict[str, list[tuple[ItemRecord, str, str]]] = {}
        for rec in missing_records:
            sheet_name, source_tag, subcategory = record_target_and_source_and_sub(rec)
            sheet_groups.setdefault(sheet_name, []).append((rec, source_tag, subcategory))

        # Ensure sheets exist (create any missing)
        for sheet_name in sheet_groups.keys():
            if sheet_name not in existing_sheet_names:
                service.spreadsheets().batchUpdate(
                    spreadsheetId=SPREADSHEET_ID,
                    body={'requests': [{'addSheet': {'properties': {'title': sheet_name}}}]}
                ).execute()
                existing_sheet_names.append(sheet_name)

        # Build append requests and track formatting in one batch
        batch_appends: list[tuple[str, list[list[str]], int]] = []  # (sheet_name, values, start_row_index)
        for sheet_name, items in sheet_groups.items():
            existing_rows = int(sheet_row_counts.get(sheet_name, 0))
            values: list[list[str]] = []
            for rec, source_tag, subcat in items:
                if source_tag:
                    source_cell = source_tag
                else:
                    try:
                        Path(rec.dat_path).relative_to(DEFAULT_BUNDLES_FOLDER)
                        source_cell = 'Vanilla'
                    except Exception:
                        source_cell = ''
                values.append(["", str(rec.item_id).strip(), rec.name.strip(), rec.description.strip() if rec.description else "", source_cell, str(rec.guid).strip(), subcat])
            if values:
                batch_appends.append((sheet_name, values, existing_rows))

        total_added = 0
        # Perform appends (one API call per sheet append), then collect formatting requests
        formatting_requests: list[dict] = []
        for sheet_name, values, start_row_index in batch_appends:
            service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{sheet_name}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={'values': values}
            ).execute()

            sheet_id = sheet_name_to_id.get(sheet_name)
            end_row_index = start_row_index + len(values)
            formatting_requests.append({
                'repeatCell': {
                    'range': {
                        'sheetId': sheet_id,
                        'startRowIndex': start_row_index,
                        'endRowIndex': end_row_index,
                        'startColumnIndex': 0,
                        'endColumnIndex': 7,
                    },
                    'cell': {
                        'userEnteredFormat': {
                            'textFormat': {
                                'fontFamily': 'Arial',
                                'fontSize': 10,
                                'bold': False
                            }
                        }
                    },
                    'fields': 'userEnteredFormat.textFormat'
                }
            })

            total_added += len(values)

        if formatting_requests:
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': formatting_requests}).execute()

        # Update GUID cache with newly added GUIDs
        try:
            for sheet_name, values, _ in batch_appends:
                for row in values:
                    if len(row) > 5:
                        existing_guids.add(str(row[5]).strip().lower())
            import json
            cache = {'timestamp': int(__import__('time').time()), 'guids': sorted(existing_guids), 'row_counts': sheet_row_counts}
            with open(GUID_INDEX_FILE, 'w', encoding='utf-8') as cf:
                json.dump(cache, cf)
        except Exception:
            pass

        print(f"Google Sheets sync complete: {total_added} records added across {len(sheet_groups)} sheets")

    except Exception as e:
        print(f"Error syncing to Google Sheets: {e}")


# ===========================
# GITHUB INTEGRATION
# ===========================

def collect_asset_files(asset_folders: Sequence[Path], ignored_folders: set[str]) -> dict[str, Path]:
    """Collect all .dat and .asset files from item folders."""
    files_to_upload = {}
    
    for folder_path in sorted(asset_folders):
        if should_ignore(folder_path, ignored_folders) or not is_item_folder(folder_path):
            continue
        
        for file_path in find_asset_files(folder_path):
            relative_path = f"{folder_path.name}/{file_path.name}"
            files_to_upload[relative_path] = file_path
    
    return files_to_upload


def upload_files_to_github(files_to_upload: dict[str, Path]) -> int:
    """Reliable, binary-safe upload to GitHub in per-mod batches.

    Groups changed files by their top-level module (Data/Workshop/ModFolder or Data/Vanilla/Pack)
    and creates one commit per group (batch size limit configurable). This avoids huge single-tree
    updates and ensures progress continues if one group fails.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
                print("GitHub upload disabled: missing token or repo config")
                return 0

    if not files_to_upload:
                print("No files to upload to GitHub")
                return 0

    # Build modmap
    modmap: dict[str, str] = {}
    if SPREADSHEET_ID and CREDENTIALS_FILE.exists():
                service = get_google_sheets_service()
                if service:
                    try:
                        modlist_res = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="'Modlist'!A2:B1000").execute()
                        for row in modlist_res.get('values', []):
                            if len(row) >= 2:
                                left = row[0].strip()
                                right = row[1].strip()
                                if right.isdigit():
                                    modmap[right] = left
                                elif left.isdigit():
                                    modmap[left] = right
                    except Exception:
                        pass

    try:
                gh = Github(GITHUB_TOKEN)
                repo = gh.get_repo(GITHUB_REPO)

                # Get base commit reference
                ref = repo.get_git_ref(f"heads/{repo.default_branch}")
                base_commit = repo.get_commit(ref.object.sha)

                # Fetch repository tree once
                remote_tree: dict[str, str] = {}
                try:
                    tree = repo.get_git_tree(base_commit.commit.tree.sha, recursive=True)
                    for item in tree.tree:
                        if item.type == 'blob':
                            remote_tree[item.path] = item.sha
                    print(f"Repository tree fetched: {len(remote_tree)} blobs known")
                except Exception as e:
                    print(f"Warning fetching repository tree: {e}")

                def git_blob_sha(content_bytes: bytes) -> str:
                    header = f"blob {len(content_bytes)}\0".encode('utf-8')
                    return hashlib.sha1(header + content_bytes).hexdigest()

                # Build changed items grouped by group_key (Data/Workshop/ModFolder or Data/Vanilla/Pack)
                groups: dict[str, list[tuple[str, bytes]]] = {}
                skipped = 0
                for rel_path, file_path in files_to_upload.items():
                    if not file_path.exists():
                        print(f"File not found: {file_path}")
                        continue
                    try:
                        with open(file_path, 'rb') as bf:
                            content_bytes = bf.read()
                    except Exception:
                        try:
                            with open(file_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
                                content_bytes = f.read().encode('utf-8')
                        except Exception:
                            print(f"Unable to read file: {file_path}")
                            continue

                    # decide github path
                    github_path = None
                    try:
                        rel = file_path.relative_to(DEFAULT_BUNDLES_FOLDER)
                        github_path = f"Data/Vanilla/{str(rel).replace('\\', '/')}"
                    except Exception:
                        try:
                            rel = file_path.relative_to(DEFAULT_WORKSHOP_FOLDER)
                            parts = rel.parts
                            if parts:
                                mod_id = parts[0]
                                mod_name = modmap.get(mod_id, mod_id)
                                mod_folder = f"{mod_name} - {mod_id}"
                                rest = Path(*parts[1:]) if len(parts) > 1 else Path("")
                                if str(rest):
                                    github_path = f"Data/Workshop/{mod_folder}/{str(rest).replace('\\', '/')}"
                                else:
                                    github_path = f"Data/Workshop/{mod_folder}/{file_path.name}"
                            else:
                                github_path = f"Data/Workshop/{file_path.name}"
                        except Exception:
                            github_path = f"Data/Other/{file_path.name}"

                    if not github_path:
                        print(f"Unable to determine destination for {file_path}")
                        continue

                    local_blob = git_blob_sha(content_bytes)
                    remote_blob = remote_tree.get(github_path)
                    if remote_blob and remote_blob == local_blob:
                        skipped += 1
                        continue

                    # derive group key (Data/Workshop/ModFolder or Data/Vanilla/Pack)
                    parts = github_path.split('/')
                    if len(parts) >= 3:
                        group_key = '/'.join(parts[:3])
                    else:
                        group_key = parts[0]

                    groups.setdefault(group_key, []).append((github_path, content_bytes))

                if not groups:
                    print(f"No changes to upload. {skipped} files were unchanged.")
                    return 0

                total_uploaded = 0
                # Commit per group, with batching inside each group to limit tree size
                MAX_BATCH = 400
                for group_key, items in groups.items():
                    print(f"Uploading group {group_key} ({len(items)} files)")
                    # process in batches
                    for i in range(0, len(items), MAX_BATCH):
                        batch = items[i:i+MAX_BATCH]
                        # refresh base commit to avoid conflicts
                        ref = repo.get_git_ref(f"heads/{repo.default_branch}")
                        base_commit = repo.get_commit(ref.object.sha)

                        blob_map: dict[str, str] = {}
                        tree_elements: list[dict] = []
                        # create blobs
                        for gpath, content_bytes in batch:
                            try:
                                b64 = base64.b64encode(content_bytes).decode('ascii')
                                blob = repo.create_git_blob(b64, 'base64')
                                blob_map[gpath] = blob.sha
                                tree_elements.append({'path': gpath, 'mode': '100644', 'type': 'blob', 'sha': blob.sha})
                            except Exception as e:
                                print(f"Failed to create blob for {gpath}: {e}")
                                continue

                        if not tree_elements:
                            print(f"No tree elements for batch {i//MAX_BATCH} of group {group_key}, skipping.")
                            continue

                        # create tree and commit
                        try:
                            new_tree = repo.create_git_tree(tree_elements, base_tree=base_commit.commit.tree.sha)
                            commit_message = f"Update {len(tree_elements)} files in {group_key}"
                            new_commit = repo.create_git_commit(commit_message + "\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>", new_tree, [base_commit.sha])
                            # update ref
                            ref.edit(new_commit.sha)
                            print(f"Committed {len(tree_elements)} files in group {group_key}")
                            total_uploaded += len(tree_elements)

                            # update remote_tree for these paths
                            for gpath, blob_sha in blob_map.items():
                                remote_tree[gpath] = blob_sha

                        except Exception as e:
                            print(f"Failed to commit batch for group {group_key}: {e}")
                            continue

                print(f"\nGitHub upload complete: {total_uploaded} files uploaded, {skipped} skipped (unchanged)")
                return total_uploaded

    except Exception as e:
                print(f"Error connecting to GitHub: {e}")
                return 0


# ===========================
# MAIN FUNCTION
# ===========================

def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(
        description="Scan Unturned bundles, sync to Google Sheets, and upload to GitHub."
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the checkbox GUI for scanning and syncing.",
    )
    parser.add_argument(
        "--bundles-folder",
        type=Path,
        default=DEFAULT_BUNDLES_FOLDER,
        help="Path to the Unturned Bundles folder.",
    )
    parser.add_argument(
        "--workshop-folder",
        type=Path,
        default=DEFAULT_WORKSHOP_FOLDER,
        help="Path to the Steam Workshop content folder.",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="Folder name to ignore. Can be used multiple times.",
    )
    parser.add_argument(
        "--include-default-ignores",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include built-in ignored folders like Mythics.",
    )
    parser.add_argument(
        "--skip-google-sheets",
        action="store_true",
        help="Skip Google Sheets sync.",
    )
    parser.add_argument(
        "--skip-github",
        action="store_true",
        help="Skip GitHub file upload.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only print the folders that would be scanned; do not sync.",
    )
    return parser.parse_args(argv)


def ignored_folders_from_args(args: argparse.Namespace) -> set[str]:
    """Build the effective ignored-folder set from CLI options."""
    ignored_folders = normalize_names(args.ignore)
    if args.include_default_ignores:
        ignored_folders.update(normalize_names(DEFAULT_IGNORED_FOLDERS))
    return ignored_folders


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    # GUI mode (interactive)
    if getattr(args, 'gui', False):
        launch_gui(args.bundles_folder, args.workshop_folder)
        return

    root_folders = [args.bundles_folder, args.workshop_folder]
    ignored_folders = ignored_folders_from_args(args)
    export_folders = discover_export_folders(root_folders, ignored_folders)

    if args.scan_only:
        print("Folders that would be exported:")
        for folder in export_folders:
            print(folder)
        print(f"Total: {len(export_folders)}")
        return

    # Collect all records for syncing
    print("\n=== Collecting items ===")
    all_records = []
    for root_folder in export_folders:
        category_ref = root_folders[0]
        for root in root_folders:
            try:
                root_folder.relative_to(root)
                category_ref = root
                break
            except ValueError:
                continue

        records = collect_item_records(root_folder, category_ref, ignored_folders)
        all_records.extend(records)

    print(f"Found {len(all_records)} total items")

    # Sync with Google Sheets
    if not getattr(args, 'skip_google_sheets', False):
        print("\n=== Syncing with Google Sheets ===")
        sync_to_google_sheets(all_records)

    # Upload files to GitHub
    if not getattr(args, 'skip_github', False):
        print("\n=== Uploading to GitHub ===")
        files_to_upload = collect_asset_files(export_folders, ignored_folders)
        upload_files_to_github(files_to_upload)

    print("\nAll tasks completed successfully!")


if __name__ == "__main__":
    main()
