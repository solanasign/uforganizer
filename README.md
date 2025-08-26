# Universal File Organizer (UFO)

A CLI tool for organizing files, detecting duplicates, and keeping projects safe (Vite/Tailwind aware).

## Features
- Organize files into category folders (Images, Code, Docs, etc.)
- Skip/ignore project folders with `vite.config.*`, `tailwind.config.*`, or `server/`
- Detect duplicates with threaded hashing
- Rich CLI interface with progress bars
- Dry-run mode for safe testing

## Installation

```bash
git clone https://github.com/yourusername/uforganizer.git
cd uforganizer
pip install .