from fastapi import FastAPI

app = FastAPI(title="Shanghai Earthquake Emergency API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
