"""api-a: FastAPI provider (scenarios A, I and a composed router route)."""

from fastapi import APIRouter, FastAPI

app = FastAPI()
users = APIRouter(prefix="/users")


@app.get("/hello")
def hello():
    return {"hello": "world"}


@app.get("/users/{user_id}")
def show_user(user_id: str):
    return {"id": user_id}


@users.get("/me")
def me():
    return {"id": "me"}


app.include_router(users, prefix="/api")
