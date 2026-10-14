from fastapi import FastAPI

app = FastAPI(title="Dockhand")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
