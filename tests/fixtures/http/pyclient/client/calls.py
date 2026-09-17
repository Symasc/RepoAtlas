"""pyclient: the Python caller."""

import httpx
import requests


def hello_dynamic(base):
    # F: a known suffix of a dynamic URL is not a route.
    return requests.get(f"{base}/hello").json()


def hello_absolute():
    return requests.get("https://api.example.com/hello").json()


def users_v1():
    return httpx.get("https://api.example.com/v1/users").json()
