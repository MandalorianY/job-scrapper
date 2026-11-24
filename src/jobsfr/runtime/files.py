from pathlib import Path
from tempfile import TemporaryDirectory


def write_text_atomically(path: Path, contents: str) -> None:
    """Replace a text file only after writing it on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as directory:
        staged = Path(directory) / path.name
        staged.write_text(contents, encoding="utf-8")
        staged.replace(path)
