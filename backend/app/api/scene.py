"""Placeholder router for /scene endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["scene"])


@router.get("/__stub__/scene")
def not_implemented_scene():
    raise HTTPException(status_code=501, detail="scene endpoints not implemented yet")
