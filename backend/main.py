import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from database import init_db
from tasks import scheduler, load_active_monitors
from oss_tasks import load_active_oss_monitors
from ws_manager import manager
from api.monitors import router as monitors_router
from api.alerts import router as alerts_router
from api.settings import router as settings_router
from api.oss import router as oss_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.start()
    await load_active_monitors()
    await load_active_oss_monitors()
    yield
    scheduler.shutdown()


app = FastAPI(title="API Monitor", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(monitors_router, prefix="/api")
app.include_router(alerts_router, prefix="/api")
app.include_router(settings_router, prefix="/api")
app.include_router(oss_router, prefix="/api")


@app.get("/")
async def root():
    frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "index.html")
    if os.path.exists(frontend_path):
        # No-cache so updates to index.html are picked up on next page load
        # without requiring a hard refresh. CDN-cached libraries inside the
        # HTML still get their own browser-level caching.
        return FileResponse(
            frontend_path,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "API Monitor is running. Place frontend/index.html to serve the UI."}


@app.get("/slides")
async def slides():
    slides_path = os.path.join(os.path.dirname(__file__), "..", "frontend", "slides.html")
    if os.path.exists(slides_path):
        return FileResponse(slides_path)
    return {"message": "Slides not found"}


@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception:
        await manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)