"""Placeholder router for /ai endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["ai"])


@router.get("/__stub__/ai")
def not_implemented_ai():
    raise HTTPException(status_code=501, detail="ai endpoints not implemented yet")
