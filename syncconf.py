"""Machine-specific settings live in config.json (gitignored); see config.example.json."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONF = json.loads((BASE / 'config.json').read_text())
