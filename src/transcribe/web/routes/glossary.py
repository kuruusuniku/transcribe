from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import yaml

from ...config import AppConfig
from ..deps import get_config

router = APIRouter()
ConfigDep = Annotated[AppConfig, Depends(get_config)]

GLOSSARY_PATH = Path(__file__).parent.parent.parent.parent.parent / "glossary.yaml"


class SubstitutionEntry(BaseModel):
    pattern: str
    replacement: str
    type: str = "literal"


class GlossaryData(BaseModel):
    context: str
    substitutions: list[SubstitutionEntry]
    important_terms: list[str]


@router.get("/glossary")
async def get_glossary():
    if not GLOSSARY_PATH.exists():
        raise HTTPException(status_code=404, detail="glossary.yaml not found")
    raw = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8"))
    subs = [
        {"pattern": s.get("pattern", ""), "replacement": s.get("replacement", ""), "type": s.get("type", "literal")}
        for s in raw.get("substitutions", [])
    ]
    return {
        "context": raw.get("context", ""),
        "substitutions": subs,
        "important_terms": raw.get("important_terms", []),
    }


@router.put("/glossary")
async def update_glossary(body: GlossaryData):
    if not GLOSSARY_PATH.exists():
        raise HTTPException(status_code=404, detail="glossary.yaml not found")
    raw = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8"))
    raw["context"] = body.context
    raw["substitutions"] = [
        {"pattern": s.pattern, "replacement": s.replacement, "type": s.type}
        for s in body.substitutions
    ]
    raw["important_terms"] = body.important_terms
    GLOSSARY_PATH.write_text(
        yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return {"ok": True}
