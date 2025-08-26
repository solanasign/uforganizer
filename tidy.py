import os
import sys
import shutil
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from rich.progress import (
    Progress, BarColumn, TimeRemainingColumn, MofNCompleteColumn,
    TaskProgressColumn, SpinnerColumn
)
from rich.console import Console
from rich.logging import RichHandler

# ========= DEFAULT CONFIG ========= #
DEFAULT_CONFIG = {
    "folders": {
        "Projects": {
            "extensions": ["html", "jsx", "tsx", "vue", "svelte"],
            "ignore": ["node_modules", ".venv", "venv", ".env", "__pycache__", ".git", "dist", "build"]
        },
        "Images": ["png", "jpg", "jpeg", "webp", "gif"],
        "Code": ["py", "js", "ts", "css", "java", "cpp", "go", "rb"],
        "Documents": ["pdf", "md", "txt", "docx", "xlsx", "pptx"],
        "Videos": ["mp4", "mov", "avi", "mkv"],
        "Audio": ["mp3", "wav", "flac", "aac"],
        "Archives": ["zip", "tar", "gz", "rar", "7z"]
    },
    "big_file_threshold_mb": 100,
    "hash_chunk_size_bytes": 4 * 1024 * 1024,  # 4 MiB
    "log_file": "organizer.log",
    "duplicates": {
        "strategy": "interactive",  # interactive / keep_first / keep_latest
        "checksum_types": ["md5"],
        "action": "delete"          # delete / move_to_duplicates
    }
}

console = Console()
log_lock = threading.Lock()

# ========= LOGGING ========= #
def setup_logging(log_file: Optional[str] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)]
    )
    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.INFO)
        logging.getLogger().addHandler(fh)

def thread_safe_log(level: str, msg: str):
    with log_lock:
        getattr(logging, level, logging.info)(msg)

# ========= CONFIG HELPERS ========= #
def deep_merge(default: dict, custom: dict) -> dict:
    merged = dict(default)
    for k, v in custom.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged

def load_config(path: str) -> Dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_conf = json.load(f)
            return deep_merge(DEFAULT_CONFIG, user_conf)
    except (FileNotFoundError, json.JSONDecodeError):
        thread_safe_log("warning", f"Config {path} missing/invalid. Using defaults.")
        return DEFAULT_CONFIG

# ========= PROJECT DETECTION ========= #
def detect_project_root(path: Path) -> bool:
    """Detect if directory qualifies as a project (vite, tailwind, server)."""
    if not path.is_dir():
        return False
    config_files = ["vite.config.js", "vite.config.ts", "tailwind.config.js", "tailwind.config.ts"]
    has_config = any((path / cfg).exists() for cfg in config_files)
    has_server = (path / "server").is_dir()
    return has_config or has_server

def should_ignore(path: Path, ignore_list: List[str]) -> bool:
    return any(part in ignore_list for part in path.parts)

# ========= FILE UTILITIES ========= #
def detect_category_and_subfolder(file_path: Path, config: Dict) -> Tuple[str, str]:
    ext = file_path.suffix.lower().lstrip(".")
    for category, exts in config["folders"].items():
        if isinstance(exts, dict):
            exts = exts.get("extensions", [])
        if isinstance(exts, list) and ext in exts:
            return category, ext.upper() or "UNKNOWN"
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type:
        main, _, sub = mime_type.partition('/')
        cat = main.capitalize()
        category_name = f"{cat}s" if not cat.endswith("s") else cat
        return category_name, (sub.upper() if sub else "UNKNOWN")
    return "Others", "UNKNOWN"

def sync_move_file(src: Path, dest_dir: Path, dry_run: bool):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dry_run:
        thread_safe_log("info", f"[DRY-RUN] Move {src} → {dest}")
        return
    try:
        shutil.move(str(src), str(dest))
        thread_safe_log("info", f"Moved {src} → {dest}")
    except Exception as e:
        thread_safe_log("error", f"Failed to move {src}: {e}")

def sync_hash_file_chunked(file_path: Path, algorithms: List[str], chunk_size: int) -> Optional[str]:
    try:
        if not file_path.exists() or not file_path.is_file():
            return None
        hashers = {algo: hashlib.new(algo) for algo in algorithms}
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                for h in hashers.values():
                    h.update(chunk)
        return "|".join(f"{algo}:{h.hexdigest()}" for algo, h in hashers.items())
    except Exception as e:
        thread_safe_log("error", f"Hash error {file_path}: {e}")
        return None

# ========= ORGANIZE PHASE ========= #
def organize_all_files(directory: Path, config: Dict, dry_run: bool, workers: int, progress: Progress) -> int:
    ignore_list = config.get("folders", {}).get("Projects", {}).get("ignore", [])
    files = [p for p in directory.rglob("*") if p.is_file() and not should_ignore(p, ignore_list)]
    total = len(files)
    task = progress.add_task("Organizing", total=total)
    if total == 0:
        return 0

    def worker(fp: Path):
        # If inside a valid project root, skip moving
        if detect_project_root(fp.parent):
            return
        cat, sub = detect_category_and_subfolder(fp, config)
        dest = directory / cat / sub
        sync_move_file(fp, dest, dry_run)
        progress.advance(task)

    with ThreadPoolExecutor(max_workers=workers) as exc:
        list(exc.map(worker, files))
    return total

# ========= MAIN ========= #
def main():
    parser = argparse.ArgumentParser(description="Universal File Organizer")
    subparsers = parser.add_subparsers(dest="command", required=True)

    org_parser = subparsers.add_parser("organize", help="Organize files into categories")
    org_parser.add_argument("--directory", default=".", help="Target directory")
    org_parser.add_argument("--dry-run", action="store_true")
    org_parser.add_argument("--workers", type=int, default=8)

    dedupe_parser = subparsers.add_parser("dedupe", help="Detect and handle duplicates")
    dedupe_parser.add_argument("--directory", default=".", help="Target directory")
    dedupe_parser.add_argument("--dry-run", action="store_true")
    dedupe_parser.add_argument("--workers", type=int, default=8)

    args = parser.parse_args()
    config = load_config("organizer_config.json")
    setup_logging(config.get("log_file"))

    target_dir = Path(args.directory).resolve()
    thread_safe_log("info", f"Target: {target_dir}  Dry-run: {args.dry_run}")

    with Progress(SpinnerColumn(), "[blue]{task.description}[/blue]", BarColumn(),
                  TaskProgressColumn(), MofNCompleteColumn(), TimeRemainingColumn(),
                  console=console) as progress:
        if args.command == "organize":
            organize_all_files(target_dir, config, args.dry_run, args.workers, progress)
        elif args.command == "dedupe":
            # call threaded_duplicate_detection here
            pass

    thread_safe_log("info", "=== Operation Complete ===")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        thread_safe_log("info", "Operation cancelled by user")
        sys.exit(1)