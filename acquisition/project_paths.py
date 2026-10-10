"""Resolve relocated evidence without changing historical database rows."""
import os
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'acquisition'

def resolve_project_path(value):
    if not value:
        return Path(value or '')
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    if path.exists():
        return path
    parts = PureWindowsPath(str(value)).parts
    for i, part in enumerate(parts):
        if part.casefold() == 'papers':
            candidate = ROOT.joinpath(*parts[i+1:])
            if candidate.exists():
                return candidate
    return path

def pointer_path(pointer):
    return resolve_project_path(Path(pointer).read_text(encoding='utf-8').strip())
