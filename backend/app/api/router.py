from fastapi import APIRouter

from app.api import auth, me

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(me.router)
