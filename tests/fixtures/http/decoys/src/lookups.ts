// decoys: HTTP-shaped calls that are not HTTP (scenario J).
import express from "express";

const app = express();
const router = express.Router();
const map = new Map<string, number>();

export function lookups(arbitraryApp: { get(path: string): unknown }) {
    map.get("/hello");
    arbitraryApp.get("/hello");
    return app.get("title");
}

router.get("/unmounted", (req, res) => res.send("never mounted here"));
