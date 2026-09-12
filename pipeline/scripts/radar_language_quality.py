"""Load the shared language policy in repository and mirrored Legacy runtimes."""
from __future__ import annotations

import os
import sys
from pathlib import Path

repository = Path(__file__).resolve().parents[2]
if not (repository / "v2/packages/contracts/russian_prose.py").is_file():
    repository = Path(os.environ.get("RADAR_ROOT", "/mnt/vdd/Radar"))
sys.path.insert(0, str(repository / "v2"))
from packages.contracts.russian_prose import (  # noqa: E402,F401
    RUSSIAN_PROSE_PROMPT, foreign_script_fragments, require_russian_script,
    brand_words, foreign_latin_words, require_russian_prose,
)
