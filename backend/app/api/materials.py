"""Placeholder router for /materials endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["materials"])


@router.get("/__stub__/materials")
def not_implemented_materials():
    raise HTTPException(status_code=501, detail="materials endpoints not implemented yet")
