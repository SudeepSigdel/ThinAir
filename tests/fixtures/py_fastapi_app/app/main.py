from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/add")
def add(a: int, b: int) -> dict[str, int]:
    return {"sum": a + b}
