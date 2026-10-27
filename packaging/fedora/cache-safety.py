"""Reject writable cache aliases before package managers or copies touch them."""
from pathlib import Path
import sys


def check_cache(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Cache root must not be a symlink: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f"Cache root must be a directory: {path}")
    base = path.resolve()
    for child in path.rglob("*"):
        if child.is_symlink() and not child.resolve().is_relative_to(base):
            raise ValueError(f"Cache symlink escapes its root: {child}")


if __name__ == "__main__":
    for argument in sys.argv[1:]:
        check_cache(Path(argument))
