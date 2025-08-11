#!/usr/bin/env python3
"""
Universal File Organizer — Rich UI + Threaded Duplicate Detection
Features:
 - Deep-merge config
 - MIME-aware categories + MIME-based subfolders (e.g., Videos/MP4)
 - ThreadPoolExecutor for parallel operations
 - Chunked hashing for large files (configurable chunk size)
 - Threaded duplicate detection (parallel hashing) and threaded duplicate processing
 - Rich live progress dashboard (organize, hash, process)
 - Dry-run support, logging, ignore lists, interactive duplicate resolution
"""

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
        "checksum_types": ["md5"],  # md5, sha1, sha256
        "action": "delete"          # delete / move_to_duplicates
    }
}

# ========= GLOBALS ========= #
console = Console()
log_lock = threading.Lock()

# ========= LOGGING ========= #
def setup_logging(log_file: Optional[str] = None) -> None:
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        handlers=[RichHandler(rich_tracebacks=True, markup=True)]
    )
    fh = logging.FileHandler(log_file or DEFAULT_CONFIG["log_file"])
    fh.setLevel(logging.INFO)
    logging.getLogger().addHandler(fh)

def thread_safe_log(level: str, msg: str):
    with log_lock:
        getattr(logging, level)(msg)

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
        thread_safe_log("warning", f"[yellow]Config {path} missing/invalid. Using defaults.[/yellow]")
        return DEFAULT_CONFIG

# ========= FILE UTILITIES ========= #
def should_ignore(path: Path, ignore_list: List[str]) -> bool:
    return any(part in ignore_list for part in path.parts)

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
        if cat in ("Image", "Video", "Audio", "Text", "Application"):
            category_name = cat + ("s" if not cat.endswith("s") else "")
        else:
            category_name = "Others"
        return category_name, (sub.upper() if sub else "UNKNOWN")
    return "Others", "UNKNOWN"

def sync_move_file(src: Path, dest_dir: Path, dry_run: bool):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dry_run:
        thread_safe_log("info", f"[cyan][DRY-RUN][/cyan] Move {src} → {dest}")
        return
    try:
        shutil.move(str(src), str(dest))
        thread_safe_log("info", f"Moved {src} → {dest}")
    except Exception as e:
        thread_safe_log("error", f"[red]Failed to move {src}: {e}[/red]")

def sync_hash_file_chunked(file_path: Path, algorithms: List[str], chunk_size: int) -> Optional[str]:
    try:
        if not file_path.exists() or not file_path.is_file():
            return None
        # initialize hashers
        hashers = {algo: hashlib.new(algo) for algo in algorithms}
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                for h in hashers.values():
                    h.update(chunk)
        parts = [f"{algo}:{hashers[algo].hexdigest()}" for algo in algorithms if algo in hashers]
        return "|".join(parts)
    except Exception as e:
        thread_safe_log("error", f"Hash error {file_path}: {e}")
        return None

# ========= ORGANIZE PHASE ========= #
def organize_all_files(directory: Path, config: Dict, dry_run: bool, workers: int, progress: Progress) -> int:
    ignore_list = config.get("folders", {}).get("Projects", {}).get("ignore", [])
    files = [p for p in directory.rglob("*") if p.is_file() and not should_ignore(p, ignore_list)]
    total = len(files)
    task = progress.add_task("[bold blue]Organizing[/bold blue]", total=total)
    if total == 0:
        progress.update(task, advance=0)
        return 0

    def worker(fp: Path):
        cat, sub = detect_category_and_subfolder(fp, config)
        dest = directory / cat / sub
        sync_move_file(fp, dest, dry_run)
        progress.advance(task)

    with ThreadPoolExecutor(max_workers=workers) as exc:
        list(exc.map(worker, files))
    return total

# ========= DUPLICATE DETECTION PHASE ========= #
def threaded_duplicate_detection(directory: Path, config: Dict, dry_run: bool, workers: int, progress: Progress):
    ignore_list = config.get("folders", {}).get("Projects", {}).get("ignore", [])
    files = [p for p in directory.rglob("*") if p.is_file() and not should_ignore(p, ignore_list)]
    if not files:
        thread_safe_log("info", "[green]No files found for duplicate detection.[/green]")
        return

    algorithms = config.get("duplicates", {}).get("checksum_types", ["md5"])
    action = config.get("duplicates", {}).get("action", "delete")
    strategy = config.get("duplicates", {}).get("strategy", "interactive")
    chunk_size = int(config.get("hash_chunk_size_bytes", DEFAULT_CONFIG["hash_chunk_size_bytes"]))

    # Hashing progress
    hash_task = progress.add_task("[bold magenta]Hashing (detect duplicates)[/bold magenta]", total=len(files))
    sig_map: Dict[str, List[Path]] = {}

    with ThreadPoolExecutor(max_workers=workers) as exc:
        future_to_file = {exc.submit(sync_hash_file_chunked, f, algorithms, chunk_size): f for f in files}
        for future in as_completed(future_to_file):
            fpath = future_to_file[future]
            sig = future.result()
            if sig:
                sig_map.setdefault(sig, []).append(fpath)
            progress.advance(hash_task)

    # Filter groups with duplicates
    dup_groups = {s: fls for s, fls in sig_map.items() if len(fls) > 1}
    if not dup_groups:
        thread_safe_log("info", "[green]No duplicates found.[/green]")
        return

    # Report duplicate groups to user
    groups = list(dup_groups.items())
    process_count = sum(len(files) - 1 for _, files in groups)  # worst-case items to process
    proc_task = progress.add_task("[bold red]Processing duplicates[/bold red]", total=process_count)

    to_process: List[Path] = []

    for sig, group in groups:
        thread_safe_log("info", f"[yellow]Duplicate group (signature={sig}):[/yellow]")
        for idx, f in enumerate(group, 1):
            thread_safe_log("info", f"  {idx}. {f}")
        if strategy == "interactive":
            # synchronous prompt per group
            console.print(f"\nDuplicate files (signature={sig}):")
            for i, f in enumerate(group, 1):
                console.print(f"{i}. {f}")
            choice = console.input("Keep which file? (number/all/none) [default: all]: ").strip().lower() or "all"
            if choice == "none":
                to_process.extend(group)
            elif choice == "all":
                # keep all, nothing to process
                continue
            else:
                try:
                    keep_idx = int(choice) - 1
                    for i, f in enumerate(group):
                        if i != keep_idx:
                            to_process.append(f)
                except Exception:
                    thread_safe_log("warning", "[yellow]Invalid choice, skipping this group.[/yellow]")
        else:
            # keep_first or keep_latest
            if strategy == "keep_latest":
                keep = max(group, key=lambda x: x.stat().st_mtime)
            else:  # keep_first
                keep = group[0]
            for f in group:
                if f != keep:
                    to_process.append(f)

    # Deduplicate to_process list
    to_process = list(dict.fromkeys(to_process))  # preserve order, unique

    # Execute actions on duplicates in parallel
    if not to_process:
        thread_safe_log("info", "[green]No duplicate files chosen for processing.[/green]")
        return

    duplicates_dir = directory / "Duplicates"
    if action == "move_to_duplicates":
        duplicates_dir.mkdir(parents=True, exist_ok=True)

    def process_file(fp: Path):
        try:
            if dry_run:
                thread_safe_log("info", f"[cyan][DRY-RUN][/cyan] Would {action}: {fp}")
            else:
                if action == "delete":
                    fp.unlink()
                    thread_safe_log("info", f"[red]Deleted[/red]: {fp}")
                elif action == "move_to_duplicates":
                    dest = duplicates_dir / fp.name
                    shutil.move(str(fp), str(dest))
                    thread_safe_log("info", f"[yellow]Moved to Duplicates[/yellow]: {fp} -> {dest}")
        except Exception as e:
            thread_safe_log("error", f"[red]Failed processing duplicate {fp}: {e}[/red]")
        finally:
            progress.advance(proc_task)

    with ThreadPoolExecutor(max_workers=workers) as exc:
        list(exc.map(process_file, to_process))

# ========= MAIN ========= #
def main():
    parser = argparse.ArgumentParser(description="Universal File Organizer — Rich UI + Threaded Duplicates")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without making changes")
    parser.add_argument("--config", default="organizer_config.json", help="Config file path")
    parser.add_argument("--directory", help="Target directory (default: current)")
    parser.add_argument("--workers", type=int, default=8, help="Number of worker threads")
    parser.add_argument("--skip-duplicates", action="store_true", help="Skip duplicate detection phase")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_file", DEFAULT_CONFIG["log_file"]))

    target_dir = Path(args.directory) if args.directory else Path.cwd()
    thread_safe_log("info", "[bold green]=== Universal File Organizer ===[/bold green]")
    thread_safe_log("info", f"Target: {target_dir}  Workers: {args.workers}  Dry-run: {args.dry_run}")

    # Shared Progress UI for both phases
    with Progress(
        SpinnerColumn(),
        "[bold blue]{task.description}[/bold blue]",
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console
    ) as progress:
        # Phase 1: Organize
        organize_all_files(target_dir, config, args.dry_run, args.workers, progress)
        # Phase 2: Duplicates
        if not args.skip_duplicates:
            threaded_duplicate_detection(target_dir, config, args.dry_run, args.workers, progress)

    thread_safe_log("info", "[bold green]=== Operation Complete ===[/bold green]")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        thread_safe_log("info", "[yellow]\nOperation cancelled by user[/yellow]")
        sys.exit(1)
