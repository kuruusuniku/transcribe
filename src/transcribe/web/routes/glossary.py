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

from ..glossary_path import GLOSSARY_PATH
from ...state import get_job_by_id


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
    _write_glossary(raw)
    return {"ok": True}


def _write_glossary(raw: dict) -> None:
    # 書き込み途中の中断やジョブ実行中の読み込みで壊れたファイルを掴まないよう、一時ファイル経由で置換する
    tmp_path = GLOSSARY_PATH.with_suffix(".yaml.tmp")
    tmp_path.write_text(
        yaml.dump(raw, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    tmp_path.replace(GLOSSARY_PATH)


class AddSubstitutionBody(SubstitutionEntry):
    apply_to_job_id: int | None = None


@router.post("/glossary/substitutions")
async def add_substitution(body: AddSubstitutionBody, cfg: ConfigDep):
    """誤認識パターンを 1 件追加する。apply_to_job_id 指定時はそのジョブの transcript.md にも即時反映する。"""
    if not body.pattern:
        raise HTTPException(status_code=422, detail="pattern is empty")
    raw = yaml.safe_load(GLOSSARY_PATH.read_text(encoding="utf-8")) if GLOSSARY_PATH.exists() else {}
    raw = raw or {}
    subs = raw.get("substitutions") or []
    entry = {"pattern": body.pattern, "replacement": body.replacement, "type": body.type}
    # 同じパターンは置き換え、新規は先頭に追加（長いパターンを先に適用するため先頭が有利）
    subs = [s for s in subs if s.get("pattern") != body.pattern]
    raw["substitutions"] = [entry, *subs]
    _write_glossary(raw)

    replaced = 0
    if body.apply_to_job_id is not None:
        row = get_job_by_id(cfg.state_db, body.apply_to_job_id)
        if row is None or not row["output_dir"]:
            raise HTTPException(status_code=404, detail="Job not found")
        path = Path(row["output_dir"]) / "transcript.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if body.type == "regex":
                text, replaced = re.subn(body.pattern, body.replacement, text)
            else:
                replaced = text.count(body.pattern)
                text = text.replace(body.pattern, body.replacement)
            path.write_text(text, encoding="utf-8")
    return {"ok": True, "replaced": replaced}
