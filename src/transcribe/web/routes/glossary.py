from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
import re
from typing import Literal

from pydantic import BaseModel, model_validator
import yaml

from ...config import AppConfig
from ..deps import get_config

router = APIRouter()
ConfigDep = Annotated[AppConfig, Depends(get_config)]

GLOSSARY_PATH = Path(__file__).parent.parent.parent.parent.parent / "glossary.yaml"


class SubstitutionEntry(BaseModel):
    pattern: str
    replacement: str
    type: Literal["literal", "regex"] = "literal"

    @model_validator(mode="after")
    def _validate_regex(self) -> "SubstitutionEntry":
        if self.type == "regex":
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"正規表現が不正です: {self.pattern!r} ({e})") from e
        return self


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
    # 書き込み途中の中断やジョブ実行中の読み込みで壊れたファイルを掴まないよう、一時ファイル経由で置換する
    tmp_path = GLOSSARY_PATH.with_suffix(".yaml.tmp")
    tmp_path.write_text(
        yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(GLOSSARY_PATH)
    return {"ok": True}
