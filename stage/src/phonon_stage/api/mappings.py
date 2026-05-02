"""Mapping CRUD endpoints — create, read, update, delete audio routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from phonon_stage.mappings.store import MappingStoreError

router = APIRouter(prefix="/mappings", tags=["mappings"])


class MappingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source_node_id: int
    source_port_ids: list[int]
    sink_node_id: int
    sink_port_ids: list[int]
    link_ids: list[int]
    gain_db: float
    pan: float
    mute: bool
    created_at: str


class CreateMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_id: int
    source_port_ids: list[int]
    sink_node_id: int
    sink_port_ids: list[int]
    gain_db: float = Field(default=0.0, ge=-90.0, le=12.0)
    pan: float = Field(default=0.0, ge=-1.0, le=1.0)
    mute: bool = False


class UpdateMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gain_db: float | None = Field(default=None, ge=-90.0, le=12.0)
    pan: float | None = Field(default=None, ge=-1.0, le=1.0)
    mute: bool | None = None


def _to_response(m: object) -> MappingResponse:
    from phonon_stage.mappings.models import Mapping

    assert isinstance(m, Mapping)
    return MappingResponse(**m.to_dict())  # type: ignore[arg-type]


@router.get("", response_model=list[MappingResponse])
async def list_mappings(request: Request) -> list[MappingResponse]:
    return [_to_response(m) for m in request.app.state.mapping_service.mappings]


@router.post("", response_model=MappingResponse, status_code=201)
async def create_mapping(request: Request, body: CreateMappingRequest) -> MappingResponse:
    try:
        mapping = await request.app.state.mapping_service.create_mapping(
            source_node_id=body.source_node_id,
            source_port_ids=body.source_port_ids,
            sink_node_id=body.sink_node_id,
            sink_port_ids=body.sink_port_ids,
            gain_db=body.gain_db,
            pan=body.pan,
            mute=body.mute,
        )
    except MappingStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_response(mapping)


@router.patch("/{mapping_id}", response_model=MappingResponse)
async def update_mapping(
    request: Request, mapping_id: str, body: UpdateMappingRequest
) -> MappingResponse:
    try:
        mapping = await request.app.state.mapping_service.update_mapping(
            mapping_id=mapping_id,
            gain_db=body.gain_db,
            pan=body.pan,
            mute=body.mute,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _to_response(mapping)


@router.delete("/{mapping_id}", status_code=204)
async def delete_mapping(request: Request, mapping_id: str) -> None:
    try:
        await request.app.state.mapping_service.delete_mapping(mapping_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
