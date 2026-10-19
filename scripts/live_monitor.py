"""Support desk brain. Rules and storage live in support_system.mongodb.js. Open http://127.0.0.1:8765"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

PAGE = Path(__file__).resolve().parent / "monitor" / "index.html"
STATUSES = ["Open", "In Progress", "Pending", "Resolved", "Closed"]
PRIORITY_SCORE = {"Low": 1, "Medium": 2, "High": 3, "Critical": 5}
RULE_MESSAGES = {  # database rule that failed -> message for the page
    "email": "Enter a valid email.", "role": "Pick customer or agent.",
    "firstName": "First and last name are needed.", "lastName": "First and last name are needed.",
    "subject": "Subject and description are needed.", "description": "Subject and description are needed.",
    "status": "Pick a status from the list.", "priority": "Pick a priority from the list.",
    "message": "Write a comment first.",
}

db = MongoClient(os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017"),
                 serverSelectionTimeoutMS=3000)["support_system"]
state = {"connected": False, "names": {}, "people": [], "counts": {}}


def clean(text):
    return " ".join((text or "").split())


def oid(value):
    if not value or not ObjectId.is_valid(value):
        raise ValueError("Pick an item from the list.")
    return ObjectId(value)


def explain(exc):
    if isinstance(exc, DuplicateKeyError):
        return "That email is already used." if "email_1" in str(exc) else "There is already an agent. This desk has one agent."
    text = str(exc.details)
    return next((msg for rule, msg in RULE_MESSAGES.items() if f"'{rule}'" in text), "Could not save. Please try again.")


def check():
    try:
        users = list(db.users.find())
        counts = {r["_id"]: r["count"] for r in db.ticket_counts.find()}
    except PyMongoError as exc:
        print(f"cannot reach database: {exc}", flush=True)
        state["connected"] = False
        return
    names = {u["_id"]: f"{u['firstName']} {u['lastName']}" for u in users}
    state.update(connected=True, names=names,
                 people=[{"id": str(u["_id"]), "name": names[u["_id"]], "role": u["role"]} for u in users],
                 counts={"total": sum(counts.values()), "open": counts.get("Open", 0), "resolved": counts.get("Resolved", 0)})


def ticket_list(status):
    return [{
        "id": str(t["_id"]), "no": t["ticket_no"], "subject": t["subject"], "description": t["description"],
        "status": t["status"], "priority": t.get("priority"), "customer": t["customer"],
        "comments": [{"who": state["names"].get(c["author_id"], "unknown"), "message": c["message"],
                      "when": c["timestamp"].strftime("%d %b %H:%M")} for c in t.get("comments", [])],
    } for t in db.ticket_board.find({"status": status} if status else {})]


def customer_id(typed):
    wanted = clean(typed)
    if not wanted:
        raise ValueError("Type a full name.")
    found = [p for p in state["people"] if p["role"] == "customer" and p["name"].lower() == wanted.lower()]
    if not found:
        raise ValueError(f'No customer called "{wanted}". Check the spelling.')
    if len(found) > 1:
        raise ValueError(f'More than one person called "{wanted}".')
    return ObjectId(found[0]["id"])


def agent_id():
    agent = db.users.find_one({"role": "agent"})
    if not agent:
        raise ValueError("Add yourself under People with the role Agent first.")
    return agent["_id"]


def add_person(d):
    try:
        db.users.insert_one({"firstName": clean(d.get("firstName")), "lastName": clean(d.get("lastName")),
                             "email": (d.get("email") or "").strip().lower(), "role": "customer"})
    except PyMongoError as exc:
        raise ValueError(explain(exc))
    check()


def create_ticket(d):
    customer = customer_id(d.get("customer"))
    priority = d.get("priority")
    last = db.tickets.find_one(sort=[("ticket_no", -1)])
    number = (last["ticket_no"] if last else 0) + 1
    try:
        db.tickets.insert_one({
            "ticket_no": number, "subject": clean(d.get("subject")), "description": (d.get("description") or "").strip(),
            "status": "Open", "customer_id": customer,
            "priority": {"level": priority, "score": PRIORITY_SCORE.get(priority, 0)}, "comments": [],
        })
    except PyMongoError as exc:
        raise ValueError(explain(exc))


def update_ticket(d):
    field, value = d.get("field"), d.get("value")
    if field == "status":
        change = {"status": value}
    elif field == "priority":
        change = {"priority": {"level": value, "score": PRIORITY_SCORE.get(value, 0)}}
    else:
        raise ValueError("Unknown change.")
    try:
        result = db.tickets.update_one({"_id": oid(d.get("id"))}, {"$set": change})
    except PyMongoError as exc:
        raise ValueError(explain(exc))
    if not result.matched_count:
        raise ValueError("That ticket no longer exists.")


def add_comment(d):
    _id = oid(d.get("id"))
    ticket = db.tickets.find_one({"_id": _id})
    if not ticket:
        raise ValueError("That ticket no longer exists.")
    if d.get("side") not in ("customer", "agent"):
        raise ValueError("Pick who is writing.")
    author = ticket["customer_id"] if d["side"] == "customer" else agent_id()
    try:
        db.tickets.update_one({"_id": _id}, {"$push": {"comments": {
            "author_id": author, "message": (d.get("message") or "").strip(), "timestamp": datetime.now(timezone.utc),
        }}})
    except PyMongoError as exc:
        raise ValueError(explain(exc))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self.reply(PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/data":
            return self.send({"connected": state["connected"], "counts": state["counts"],
                              "people": state["people"], "statuses": STATUSES})
        if url.path == "/tickets":
            return self.send({"tickets": ticket_list(parse_qs(url.query).get("status", [""])[0])})
        self.send_error(404)

    def do_POST(self):
        actions = {"/ticket": create_ticket, "/update": update_ticket, "/comment": add_comment, "/person": add_person}
        if self.path not in actions:
            return self.send_error(404)
        try:
            actions[self.path](json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))))
            self.send({"ok": True})
        except ValueError as exc:
            self.send({"error": str(exc)}, 400)
        except Exception as exc:
            print(f"save failed: {exc}", flush=True)
            self.send({"error": "Could not save. Please try again."}, 500)

    def send(self, data, status=200):
        self.reply(json.dumps(data, default=str).encode(), "application/json", status)

    def reply(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def watch():
    while True:
        check()
        time.sleep(1)


if __name__ == "__main__":
    if "ticket_board" not in db.list_collection_names():
        raise SystemExit("Run support_system.mongodb.js in mongosh first.")
    threading.Thread(target=watch, daemon=True).start()
    print("Open http://127.0.0.1:8765 in your browser", flush=True)
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
