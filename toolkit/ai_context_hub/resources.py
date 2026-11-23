from importlib.resources import files
from pathlib import Path


def startup_rules():
    bundled = files("ai_context_hub").joinpath("startup.md")
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    return (Path(__file__).resolve().parents[1] / "rules/startup.md").read_text(encoding="utf-8")
