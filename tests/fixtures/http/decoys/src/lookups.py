"""decoys: HTTP-shaped calls that are not HTTP (scenario J)."""

from mylib import FastAPI

cache = {}
mapping = {"/hello": 1}
app = FastAPI()


def lookups(arbitraryApp):
    cache.get("/hello")
    mapping.get("/hello")
    arbitraryApp.get("/hello")


@app.get("/hello")
def not_a_fastapi_route():
    return None
