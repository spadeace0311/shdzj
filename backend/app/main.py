from fastapi import FastAPI

from app.events.router import router as events_router

app = FastAPI(title="Shanghai Earthquake Emergency API")
app.include_router(events_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
