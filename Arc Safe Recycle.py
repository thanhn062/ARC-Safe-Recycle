# -*- coding: utf-8 -*-
"""
ARC Safe Recycle Search 
Minimal UI + Preformatted Index + Fuzzy + Settings (auto-save)
Process-aware hotkey (title or exe name) + debug inspector.
"""

import json, os, threading, time, sys
import tkinter as tk
from tkinter import ttk, messagebox

import requests
import keyboard  # global hotkeys

# Win32 & process utils (title + exe name)
try:
    import win32gui
    import win32process
except ImportError:
    win32gui = None
    win32process = None

import psutil
from PIL import Image, ImageDraw
import pystray

# ------------------ Paths & URLs ------------------
APP_NAME = "ARC_Safe_Recycle"

# Determine base directory:
# - When frozen (PyInstaller), use the folder containing the .exe
# - When running from source, use the folder containing this script
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Settings live next to the script/exe
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

# Cache directory for downloaded data (keeps memory low; reuse between runs)
DATA_DIR = os.path.join(BASE_DIR, "arc_data")
HIDEOUT_DIR = os.path.join(DATA_DIR, "hideout")
PROJECTS_PATH = os.path.join(DATA_DIR, "projects.json")

PROJECTS_URL = "https://raw.githubusercontent.com/RaidTheory/arcraiders-data/main/projects.json"
HIDEOUT_FILES = (
    "equipment_bench",
    "explosives_bench",
    "med_station",
    "refiner",
    "scrappy",
    "stash",
    "utility_bench",
    "weapon_bench",
    "workbench",
)
HIDEOUT_RAW_BASE = "https://raw.githubusercontent.com/RaidTheory/arcraiders-data/main/hideout"
StopEvent = threading.Event()

# ------------------ Tuning ------------------
FUZZY_THRESHOLD = 70
MAX_RESULTS     = 5
ACTIVE_KEYWORDS = ("arc raiders", "arcraiders", "arc-raiders")  # title or process match

# Expedition project key used in Settings["projects"][EXPEDITION_PROJECT_KEY]
EXPEDITION_PROJECT_KEY = "Expedition Project"

# ------------------ Index state ------------------
ItemIdToUsages = {}   # item_id -> {label: qty} (filtered by settings)
AllItemNames   = []   # ["Cat Bed", ...]
NameToLines    = {}   # "Cat Bed" -> ["❌ Cat Bed", "• Scrappy 4 – ×1", "", ...]

ModulesMeta    = []   # [{"name": <str>, "maxLevel": <int>}]
ProjectsMeta   = []   # [{"name": <str>, "maxStage": <int>}]
Settings       = {"workstations": {}, "projects": {}}

# ------------------ Utilities ------------------
def ensure_dir():
    # Ensure the directory for settings exists (usually the base dir).
    settings_dir = os.path.dirname(SETTINGS_PATH)
    if settings_dir:
        os.makedirs(settings_dir, exist_ok=True)

def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(HIDEOUT_DIR, exist_ok=True)

def refresh_data_startup():
    """
    Download JSONs on first need, cache to disk, load into memory for indexing,
    then let callers drop references to keep RAM low.
    """
    ensure_data_dirs()
    modules = ensure_hideout_cached()
    projects = ensure_projects_cached()
    return modules, projects

def ensure_projects_cached():
    if not os.path.exists(PROJECTS_PATH):
        download_projects_json(save_to_disk=True)
    try:
        return load_json(PROJECTS_PATH)
    except Exception:
        # If cache is corrupt, re-download.
        download_projects_json(save_to_disk=True)
        return load_json(PROJECTS_PATH)

def ensure_hideout_cached():
    modules = []
    missing = []
    for name in HIDEOUT_FILES:
        path = os.path.join(HIDEOUT_DIR, f"{name}.json")
        if os.path.exists(path):
            try:
                modules.append(load_json(path))
                continue
            except Exception:
                missing.append(name)
        else:
            missing.append(name)

    if missing:
        download_split_hideout_modules(save_to_disk=True)
        modules = [load_json(os.path.join(HIDEOUT_DIR, f"{name}.json")) for name in HIDEOUT_FILES]
    return modules

def download_projects_json(save_to_disk=False):
    resp = requests.get(PROJECTS_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError("projects.json is not a list")
    if save_to_disk:
        save_json(PROJECTS_PATH, data)
    return data

def download_split_hideout_modules(save_to_disk=False):
    modules = []
    errors = []
    for name in HIDEOUT_FILES:
        url = f"{HIDEOUT_RAW_BASE}/{name}.json"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if save_to_disk:
                save_json(os.path.join(HIDEOUT_DIR, f"{name}.json"), data)
            modules.append(data)
        except Exception as e:
            errors.append(f"{name}.json ({e})")
    if errors:
        raise RuntimeError("Failed to download modules: " + ", ".join(errors))
    if not modules:
        raise RuntimeError("No hideout module files were downloaded.")
    return modules

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, obj):
    # Ensure target directory exists
    dirpath = os.path.dirname(path)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def pick_en_name(name_field):
    if isinstance(name_field, dict):
        if "en" in name_field and isinstance(name_field["en"], str):
            return name_field["en"]
        for v in name_field.values():
            if isinstance(v, str):
                return v
        return "Unknown"
    if isinstance(name_field, str):
        return name_field
    return "Unknown"

def pretty_from_item_id(item_id: str) -> str:
    parts = [p for p in item_id.split("_") if p]
    return " ".join(w.capitalize() for w in parts) if parts else item_id

def get_qty(obj) -> int:
    for k in ("quantity", "count", "qty", "amount", "requiredCount"):
        if isinstance(obj, dict) and k in obj:
            try:
                return int(obj[k])
            except Exception:
                pass
    return 1

# ------------------ Settings ------------------
def load_settings():
    global Settings
    if os.path.exists(SETTINGS_PATH):
        try:
            Settings = load_json(SETTINGS_PATH)
        except Exception:
            Settings = {"workstations": {}, "projects": {}}
    if "workstations" not in Settings: Settings["workstations"] = {}
    if "projects" not in Settings: Settings["projects"] = {}
    # ensure Expedition Phase key exists
    Settings["projects"].setdefault(EXPEDITION_PROJECT_KEY, 0)

def save_settings():
    save_json(SETTINGS_PATH, Settings)

# ------------------ Index build (applies Settings) ------------------
def build_indexes_from_local(modules, projects):
    global ItemIdToUsages, AllItemNames, NameToLines, ModulesMeta, ProjectsMeta
    ItemIdToUsages = {}
    AllItemNames = []
    NameToLines = {}
    ModulesMeta = []
    ProjectsMeta = []

    # Workstations
    if isinstance(modules, list):
        for mod in modules:
            module_name = pick_en_name(mod.get("name"))
            max_level = int(mod.get("maxLevel", 0))
            ModulesMeta.append({"name": module_name, "maxLevel": max_level})
            cur_level = int(Settings["workstations"].get(module_name, 0))

            for lvl in (mod.get("levels") or []):
                lvl_num = int(lvl.get("level", 0))
                if lvl_num <= cur_level:
                    continue  # skip completed
                label = f"{module_name} {lvl_num}" if lvl_num else module_name
                for r in (lvl.get("requirementItemIds") or []):
                    item_id = str(r.get("itemId") or "").strip()
                    if not item_id:
                        continue
                    qty = get_qty(r)
                    _add_usage(item_id, label, qty)

    # Expedition phases (single spinner): exclude phases <= selected; include > selected
    max_phase = _add_expedition_phases(projects)
    ProjectsMeta.append({"name": "Expedition Phase", "maxStage": max_phase})

    # Preformat NameToLines (human names)
    for item_id, uses in ItemIdToUsages.items():
        name = pretty_from_item_id(item_id)
        AllItemNames.append(name)

        lines = []
        if uses:
            lines.append(f"❌ {name}")
            for label in sorted(uses.keys()):
                qty = uses[label]
                lines.append(f"• {label} – ×{qty}")
        else:
            lines.append(f"✅ {name}")
        lines.append("")  # blank line between results
        NameToLines[name] = lines

    AllItemNames.sort()

def _add_usage(item_id: str, label: str, qty: int):
    bucket = ItemIdToUsages.setdefault(item_id, {})
    bucket[label] = bucket.get(label, 0) + qty

def _add_expedition_phases(projects) -> int:
    """
    Reads projects.json which contains root 'Expedition Project' with 'phases'.
    Filters by Settings["projects"][EXPEDITION_PROJECT_KEY] (completed phase).
    Only phases with phase > current are included in the index.
    """
    current_phase = int(Settings["projects"].get(EXPEDITION_PROJECT_KEY, 0))
    max_phase = 0

    if isinstance(projects, list) and projects:
        root = projects[0]
        phases = root.get("phases") or []
        for ph in phases:
            try:
                phase_num = int(ph.get("phase", 0))
            except Exception:
                phase_num = 0
            if phase_num > max_phase:
                max_phase = phase_num

            if phase_num <= current_phase:
                continue  # treat as completed; exclude

            pname = pick_en_name(ph.get("name", ""))
            label = f"Expedition – {pname} {phase_num}".strip()
            for r in (ph.get("requirementItemIds") or []):
                iid = str(r.get("itemId") or "").strip()
                if not iid:
                    continue
                qty = get_qty(r)
                _add_usage(iid, label, qty)

    return max_phase

# ------------------ Fuzzy scoring ------------------
def levenshtein(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0: return lb
    if lb == 0: return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0]*lb
        ai = a[i-1]
        for j in range(1, lb + 1):
            cost = 0 if ai == b[j-1] else 1
            cur[j] = min(cur[j-1] + 1, prev[j] + 1, prev[j-1] + cost)
        prev = cur
    return prev[lb]

def fuzzy_score(q: str, s: str) -> int:
    pref = 40 if s.startswith(q) else (20 if q in s else 0)
    if not s and not q:
        return 100
    dist = levenshtein(q, s)
    maxlen = max(len(q), len(s)) or 1
    sim = int((1 - dist / maxlen) * 100)
    score = pref + sim
    return 100 if score > 100 else score

# ------------------ Search (names only; multi-result) ------------------
def title_case(s: str) -> str:
    """Convert a string to Title Case safely."""
    return " ".join(w.capitalize() for w in s.split()) if s else ""

def build_results(query: str):
    q = (query or "").strip()
    if not q:
        return []

    ql = q.lower()
    hits = []

    # 1) Prefix
    for name in AllItemNames:
        if name.lower().startswith(ql):
            hits.append(name)

    # 2) Contains
    if not hits:
        for name in AllItemNames:
            if ql in name.lower():
                hits.append(name)

    # 3) Fuzzy
    if not hits:
        scored = []
        for name in AllItemNames:
            s = fuzzy_score(ql, name.lower())
            if s >= FUZZY_THRESHOLD:
                scored.append((s, name))
        scored.sort(reverse=True, key=lambda t: t[0])
        hits = [n for _, n in scored]

    if not hits:
        return [f"✅ {title_case(q)}", ""]

    # MULTIPLE result blocks (up to MAX_RESULTS)
    lines, seen = [], set()
    for name in hits[:MAX_RESULTS]:
        if name in seen:
            continue
        seen.add(name)
        lines.extend(NameToLines[name])  # preformatted (includes blank line)
    return lines

# ------------------ UI (Tk) ------------------
class SearchUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ARC Safe Recycle")
        self.geometry("400x360")
        self.resizable(False, False)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.entry = ttk.Entry(self, font=("Segoe UI", 12))
        self.entry.grid(row=0, column=0, sticky="ew", padx=10, pady=(12, 6))
        self.entry.bind("<KeyRelease>", self.on_type)

        self.listbox = tk.Listbox(self, height=14, activestyle="dotbox", font=("Segoe UI", 11))
        self.listbox.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.bind("<Escape>", lambda e: self.hide())

    def show(self, preset_clear=True):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(150, lambda: self.attributes("-topmost", False))
        if preset_clear:
            self.clear_results()
            self.entry.delete(0, tk.END)
        self.entry.focus_set()
        self.entry.icursor(tk.END)

    def hide(self):
        self.withdraw()

    def clear_results(self):
        self.listbox.delete(0, tk.END)

    def set_results(self, lines):
        self.listbox.delete(0, tk.END)
        for line in lines:
            self.listbox.insert(tk.END, line)
        if lines:
            self.listbox.see(0)

    def on_type(self, event=None):
        q = self.entry.get()
        lines = build_results(q)
        self.set_results(lines)

# ------------------ Settings window ------------------
class SettingsWin(tk.Toplevel):
    def __init__(self, master: 'SearchUI'):
        super().__init__(master)
        self.title("Settings – Your Progress")
        self.geometry("250x380")
        self.minsize(250, 380)
        self.transient(master)
        self.grab_set()

        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        # --- Workstations ---
        ttk.Label(root, text="Workstations", font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.ws_frame = ttk.Frame(root)
        self.ws_frame.grid(row=1, column=0, sticky="ew")
        self._build_ws_rows()

        ttk.Separator(root, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=10)

        # --- Expedition Phase (spinner with arrows) ---
        ttk.Label(root, text="Expedition – Phase", font=("Segoe UI", 11, "bold")).grid(row=3, column=0, sticky="w", pady=(0, 6))
        self.pr_frame = ttk.Frame(root)
        self.pr_frame.grid(row=4, column=0, sticky="ew")
        self._build_phase_row()

        self.bind("<Escape>", lambda e: self.destroy())

    def _spin(self, parent, row, name, max_val, current, on_change):
        ttk.Label(parent, text=f"{name}  (max {max_val})").grid(row=row, column=0, sticky="w", padx=4, pady=3)
        var = tk.IntVar(value=current)
        sp = ttk.Spinbox(parent, from_=0, to=max_val, textvariable=var, width=6, wrap=True, justify="right")
        sp.grid(row=row, column=1, sticky="e", padx=4, pady=3)

        def _cb(*_):
            val = var.get()
            if val < 0: val = 0
            if val > max_val: val = max_val
            var.set(val)
            on_change(name, val)

        var.trace_add("write", _cb)
        sp.bind("<FocusOut>", lambda e: _cb())

    def _build_ws_rows(self):
        f = self.ws_frame
        for w in f.grid_slaves(): w.destroy()
        f.columnconfigure(0, weight=1)
        row = 0
        for meta in ModulesMeta:
            nm = meta["name"]
            mx = int(meta["maxLevel"])
            cur = int(Settings["workstations"].get(nm, 0))
            self._spin(f, row, nm, mx, cur, self._on_ws_changed)
            row += 1

    def _build_phase_row(self):
        f = self.pr_frame
        for w in f.grid_slaves(): w.destroy()
        f.columnconfigure(0, weight=1)

        # Use ProjectsMeta to find max phase (we set it in build_indexes_from_local)
        max_phase = 0
        for meta in ProjectsMeta:
            name = str(meta.get("name", ""))
            if name == "Expedition Phase" or name == EXPEDITION_PROJECT_KEY:
                try:
                    max_phase = int(meta.get("maxStage", 0))
                except Exception:
                    max_phase = 0
                break
        if max_phase == 0:
            max_phase = 6  # fallback to 6 as per sample file

        current = int(Settings["projects"].get(EXPEDITION_PROJECT_KEY, 0))
        self._spin(f, 0, "Phase", max_phase, current, self._on_phase_changed)

    def _on_ws_changed(self, name, val):
        Settings["workstations"][name] = int(val)
        save_settings()
        try:
            modules, projects = refresh_data_startup()
            build_indexes_from_local(modules, projects)
            modules = projects = None
            self.master.on_type()
        except Exception as e:
            messagebox.showerror("Refresh error", f"Could not refresh data:\n{e}")

    def _on_phase_changed(self, _label, val):
        Settings["projects"][EXPEDITION_PROJECT_KEY] = int(val)
        save_settings()
        try:
            modules, projects = refresh_data_startup()
            build_indexes_from_local(modules, projects)
            modules = projects = None
            self.master.on_type()
        except Exception as e:
            messagebox.showerror("Refresh error", f"Could not refresh data:\n{e}")

# ------------------ Foreground app diagnostics ------------------
def maybe_show(ui: SearchUI):
    hwnd = win32gui.GetForegroundWindow()
    title = (win32gui.GetWindowText(hwnd) or "")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    exe = psutil.Process(pid).name()
    if "PioneerGame" in exe:
        ui.show()
    return

# ------------------ Tray ------------------
def create_tray(ui: SearchUI):
    # tiny cyan "feather/magnifier" icon
    img = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 12, 12), outline=(30, 180, 255, 255), width=2)
    d.line((10, 10, 15, 15), fill=(30, 180, 255, 255), width=2)

    def on_show(icon, item):
        ui.show(preset_clear=True)

    def on_settings(icon, item):
        SettingsWin(ui)

    def on_quit(icon, item):
        # Run Tk shutdown from the Tk thread; also clear hotkeys to let the
        # keyboard helper threads exit cleanly.
        try:
            StopEvent.set()
            keyboard.unhook_all_hotkeys()
            keyboard.unhook_all()
        except Exception:
            pass
        ui.after(0, ui.destroy)
        # Stop the tray loop on its own thread to avoid blocking the menu callback.
        threading.Thread(target=icon.stop, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Show", on_show),
        pystray.MenuItem("Settings", on_settings),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("arc_safe_recycle", img, "ARC Safe Recycle", menu)
    th = threading.Thread(target=icon.run, daemon=True)
    th.start()
    return icon

# ------------------ Hotkeys ------------------
def hotkey_loop(ui: SearchUI):
    # Ctrl+F => show if ARC Raiders is active
    keyboard.add_hotkey("ctrl+f", lambda: maybe_show(ui))
    keyboard.add_hotkey("escape", lambda: ui.hide())
    while not StopEvent.is_set():
        time.sleep(0.2)

# ------------------ Main ------------------
def main():
    # Always refresh JSON on startup (live content), then offline
    try:
        modules, projects = refresh_data_startup()
    except Exception as e:
        tk.Tk().withdraw()
        messagebox.showerror("Startup error", f"Could not download data:\n{e}")
        sys.exit(1)

    load_settings()

    try:
        build_indexes_from_local(modules, projects)
        modules = None
        projects = None
    except Exception as e:
        tk.Tk().withdraw()
        messagebox.showerror("Index error", f"Failed to build indexes:\n{e}")
        sys.exit(1)

    ui = SearchUI()
    ui.withdraw()

    _tray = create_tray(ui)

    th = threading.Thread(target=hotkey_loop, args=(ui,), daemon=True)
    th.start()

    ui.mainloop()

if __name__ == "__main__":
    main()
