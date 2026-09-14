// api-c: Express provider, and a second declarer of /health (scenario H).
const express = require("express");

const app = express();
const items = express.Router();

items.get("/", (req, res) => res.json([]));

app.get("/status", (req, res) => res.send("ok"));
app.use("/items", items);

module.exports = app;
