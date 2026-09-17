"""api-b: Flask provider behind a Swagger 2 base path (scenario C)."""

from flask import Flask

app = Flask(__name__)


@app.route("/users")
def list_users():
    return []
