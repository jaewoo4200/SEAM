"""Placeholder router for /compile endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["compile"])


@router.get("/__stub__/compile")
def not_implemented_compile():
    raise HTTPException(status_code=501, detail="compile endpoints not implemented yet")
