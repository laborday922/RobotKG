from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Relation(BaseModel):
    source: str = Field(min_length=1)
    type: str = Field(min_length=1)
    target: str = Field(min_length=1)


class UpsertFileRequest(BaseModel):
    file_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    content: str = Field(default="")
    metadata: dict[str, Any] | None = None


class DeleteFileResponse(BaseModel):
    file_id: str
    deleted: bool


class UpsertFileResponse(BaseModel):
    file_id: str
    entities_count: int
    relations_count: int


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    message: str


class OkResponse(BaseModel):
    ok: Literal[True] = True
    message: str = "ok"
    data: Any | None = None


class QaSearchItem(BaseModel):
    doc_id: str
    file_name: str
    service_name: str | None = None
    organization: str | None = None
    address: str | None = None
    updated_at: str | None = None
    materials_count: int = 0
    steps_count: int = 0
    laws_count: int = 0
    score: float = 0.0
    matched_fields: list[str] = []


class QaDocDetail(BaseModel):
    doc_id: str
    file_name: str
    updated_at: str | None = None
    service_name: str | None = None
    organization: str | None = None
    address: str | None = None
    materials: list[str] = []
    steps: list[str] = []
    laws: list[str] = []
    entities: list[str] = []
