"""Placeholder router for /projects endpoints - replaced during Phase B build."""

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["projects"])


@router.get("/__stub__/projects")
def not_implemented_projects():
    raise HTTPException(status_code=501, detail="projects endpoints not implemented yet")
