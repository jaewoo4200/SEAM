"""Placeholder router for /simulate endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["simulate"])


@router.get("/__stub__/simulate")
def not_implemented_simulate():
    raise HTTPException(status_code=501, detail="simulate endpoints not implemented yet")
