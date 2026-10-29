from fastapi import APIRouter

from app.api import auth, billing, dev, invitations, me, projects, tasks, workspace
from app.config import get_settings

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(me.router)
api_router.include_router(workspace.router)
api_router.include_router(invitations.router)
api_router.include_router(invitations.ws_router)
api_router.include_router(projects.router)
api_router.include_router(tasks.router)
api_router.include_router(billing.router)
api_router.include_router(billing.public_router)
if get_settings().dev_endpoints:
    api_router.include_router(dev.router)
