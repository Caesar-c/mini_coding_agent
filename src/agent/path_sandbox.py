import os
from pathlib import Path
from typing import Union, List

class PathSandbox:
    def __init__(self, root_dir: str = "."):
        self.root = Path(root_dir).resolve()

    def validate(self, requested_path: str) -> Path:
        """Resolve and validate that a path stays within the sandbox root."""
        full_path = (self.root / requested_path).resolve()
        # Check if the resolved path starts with the root directory
        try:
            full_path.relative_to(self.root)
        except ValueError:
            raise ValueError(
                f"Path traversal detected: '{requested_path}' "
                f"would escape sandbox root {self.root}"
            )
        return full_path

    def read_file(self, path: str) -> str:
        resolved = self.validate(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"File does not exist: {resolved}")
        return resolved.read_text(encoding="utf-8")

    def write_file(self, path: str, content: str) -> None:
        resolved = self.validate(path)
        # Ensure parent dir exists
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")

    def list_directory(self, path: str = ".", recursive: bool = False) -> List[str]:
        resolved = self.validate(path)
        if not resolved.is_dir():
            raise NotADirectoryError(f"Not a directory: {resolved}")

        if recursive:
            paths = [p for p in resolved.rglob("*") if p.is_file()]
        else:
            paths = [p for p in resolved.iterdir() if p.is_file()]

        return [str(p.relative_to(self.root)) for p in paths]

    def create_directory(self, path: str, parents: bool = True) -> None:
        resolved = self.validate(path)
        resolved.mkdir(parents=parents, exist_ok=True)

    def file_exists(self, path: str) -> bool:
        try:
            resolved = self.validate(path)
            return resolved.is_file()
        except (ValueError, FileNotFoundError):
            return False

    def get_working_dir(self) -> str:
        return str(self.root)