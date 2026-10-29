from fastapi import APIRouter

from app.api import auth, invitations, me, workspace

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(workspace.router)
api_router.include_router(invitations.router)
api_router.include_router(invitations.ws_router)
