"""End-to-end checks for the stateful session drill feature."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

P = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 1},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# 1. create session
r = client.post("/v1/sessions", json=P)
check("create 201", r.status_code == 201, r.status_code)
body = r.json()
sid = body.get("id", "")
check("create returns id", bool(sid))
check("create version 0", body.get("version") == 0)
check("trace empty", body.get("trace") == [])
check("enabled ids sorted", body.get("enabled_transition_ids") == ["t1"],
      body.get("enabled_transition_ids"))
check("configuration initial", body.get("configuration") == {
    "state": {"A": 0, "B": 0}, "queues": {"A_to_B": None, "B_to_A": None}})
check("protocol echoed with aliases", body["protocol"]["transitions"][0]["from"] == "A")

# 2. unknown session 404
r = client.get("/v1/sessions/deadbeef")
check("unknown session 404", r.status_code == 404)
r = client.post("/v1/sessions/deadbeef/transitions",
                json={"transition_id": "t1", "expected_version": 0})
check("unknown execute 404", r.status_code == 404)
r = client.put("/v1/sessions/deadbeef/protocol",
               json={"protocol": P, "expected_version": 0})
check("unknown replace 404", r.status_code == 404)

# 3. invalid protocol reuses validation -> 422
bad = dict(P)
bad = {**P, "transitions": P["transitions"] + [
    {"id": "t1", "from": "B", "to": "A", "action": "send", "message": "q",
     "source": 0, "target": 1}]}
r = client.post("/v1/sessions", json=bad)
check("duplicate ids 422", r.status_code == 422, r.status_code)
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": bad, "expected_version": 0})
check("replace invalid protocol 422", r.status_code == 422, r.status_code)

# 4. execute first transition
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": 0})
check("execute t1 200", r.status_code == 200, r.status_code)
b = r.json()
check("version 1", b["version"] == 1)
check("trace len 1", len(b["trace"]) == 1)
step = b["trace"][0]
check("step fields", {k: step[k] for k in
                      ("index", "transition_id", "from", "to", "action", "message")} == {
    "index": 1, "transition_id": "t1", "from": "A", "to": "B",
    "action": "send", "message": "m"})
check("step post-state", step["state"] == {"A": 1, "B": 0})
check("step post-queue", step["queues"] == {"A_to_B": "m", "B_to_A": None})
check("current config after t1", b["configuration"]["state"] == {"A": 1, "B": 0}
      and b["configuration"]["queues"] == {"A_to_B": "m", "B_to_A": None})
check("enabled after t1 = t4", b["enabled_transition_ids"] == ["t4"],
      b["enabled_transition_ids"])

# 5. stale version -> 409, session untouched
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 0})
check("stale execute 409", r.status_code == 409, r.status_code)
check("stale keeps version", client.get(f"/v1/sessions/{sid}").json()["version"] == 1)

# 6. non-enabled transition -> 409
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": 1})
check("disabled transition 409", r.status_code == 409, r.status_code)
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "nope", "expected_version": 1})
check("unknown transition execute 409", r.status_code == 409, r.status_code)

# 7. complete the protocol (t4) -> version 2, completed, no enabled transitions
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 1})
check("execute t4 200", r.status_code == 200, r.status_code)
b = r.json()
check("version 2", b["version"] == 2)
check("trace len 2", len(b["trace"]) == 2)
check("completed empty enabled", b["enabled_transition_ids"] == [],
      b["enabled_transition_ids"])
check("completed config", b["configuration"]["state"] == {"A": 1, "B": 1}
      and b["configuration"]["queues"] == {"A_to_B": None, "B_to_A": None})

# 8. completion cannot continue -> 409
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t4", "expected_version": 2})
check("completed no execute 409", r.status_code == 409, r.status_code)
check("after refused still v2", client.get(f"/v1/sessions/{sid}").json()["version"] == 2)

# 9. replace protocol at completion: same trace ids must replay against new defs.
#    New protocol: t1 sends m (A 0->0 instead of 0->1), t2 receives m on B...
#    Trace is [t1, t4]; make replay fail at step 2 by removing enablement.
#    First, a successful replacement changing definitions:
P2 = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        # t1 still sends m from A0; now loops A 0->0
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        # t4 receives m as B 0->1 (owner of a receive is the `to` endpoint!)
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P2, "expected_version": 2})
check("replace 200", r.status_code == 200, r.status_code)
b = r.json()
check("replace version 3", b["version"] == 3)
check("replace replayed trace len", len(b["trace"]) == 2)
check("replayed step1 owner-state", b["trace"][0]["state"] == {"A": 0, "B": 0})
check("replayed step2 final", b["trace"][1]["state"] == {"A": 0, "B": 1})
check("current config recomputed", b["configuration"]["state"] == {"A": 0, "B": 1},
      b["configuration"])
check("protocol definition changed",
      b["protocol"]["transitions"][0]["target"] == 0)
# A=0 not final -> not completed -> execution can continue with the new model
check("continuable after replace", b["enabled_transition_ids"] == ["t1"],
      b["enabled_transition_ids"])

# 10. failed replay -> 422 with first_failed_step, old session retained
#     P3: t1 now A 0->1 sending m (same as original), t4 receive m A-owned
#     1->0? make step 2 not enabled by requiring different message.
P3 = {
    "A": {"states": [0, 1], "initial": 0, "finals": [1]},
    "B": {"states": [0, 1], "initial": 0, "finals": [1]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "ZZ",
         "source": 0, "target": 1},
    ],
}
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P3, "expected_version": 3})
check("replay fail 422", r.status_code == 422, (r.status_code, r.text))
check("first failed step = 2",
      r.json().get("detail", {}).get("first_failed_step") == 2,
      r.json())
b = client.get(f"/v1/sessions/{sid}").json()
check("old session retained after failed replay",
      b["version"] == 3 and b["protocol"]["transitions"][1]["message"] == "m")

# missing id in new protocol -> first failed step 1
P4 = dict(P2)
P4 = {**P2, "transitions": [P2["transitions"][0]]}  # drop t4
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P4, "expected_version": 3})
check("missing id replay 422 step 2",
      r.status_code == 422 and r.json()["detail"]["first_failed_step"] == 2,
      (r.status_code, r.text))

# 11. replay cannot complete before trace end
# fresh session: execute t1,t4 to completion in original protocol, then
# replace with a protocol whose t1 already lands in a completion config.
r = client.post("/v1/sessions", json=P)
sid2 = r.json()["id"]
for tid, ver in (("t1", 0), ("t4", 1)):
    client.post(f"/v1/sessions/{sid2}/transitions",
                json={"transition_id": tid, "expected_version": ver})
# t1 sends m with A 0->1 (not completion, queue has m), t4 consumes -> completion.
# New t1: A already starts in a final state... build protocol where after step1
# (the send) BOTH are final and queues empty — impossible after a send (queue
# occupied). Instead use trace [t4? no]. Achieve "completion before trace end"
# with a 2-step trace where after step 1 queues are empty & both final: t1 would
# need to be a receive that empties... but first step starts with empty queues.
# Simpler: replace into protocol where step 1 (t1, the send) yields completion:
# a send always fills a queue, so completion is impossible immediately after a
# send. Use original 4-transition protocol with trace t1,t2,t4,t3? Instead make
# trace of length 2 where step1 is t1 send, and new t1 loops while new
# *initial* configuration is already a completion configuration (both final).
P_completed_initial = {
    "A": {"states": [0], "initial": 0, "finals": [0]},
    "B": {"states": [0], "initial": 0, "finals": [0]},
    "transitions": [
        {"id": "t1", "from": "A", "to": "B", "action": "send", "message": "m",
         "source": 0, "target": 0},
        {"id": "t4", "from": "A", "to": "B", "action": "receive", "message": "m",
         "source": 0, "target": 0},
    ],
}
r = client.put(f"/v1/sessions/{sid2}/protocol",
               json={"protocol": P_completed_initial, "expected_version": 2})
check("initial completed -> replay 422 step1",
      r.status_code == 422 and r.json()["detail"]["first_failed_step"] == 1,
      (r.status_code, r.text))
check("session2 retained", client.get(f"/v1/sessions/{sid2}").json()["version"] == 2)

# 12. stale version on replace -> 409
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"protocol": P2, "expected_version": 0})
check("stale replace 409", r.status_code == 409, r.status_code)

# 13. concurrent same-version modifications: exactly one succeeds
import threading
results = []
barrier = threading.Barrier(5)


def worker():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{sid}/transitions",
                     json={"transition_id": "t1", "expected_version": 3})
    results.append(rr.status_code)


threads = [threading.Thread(target=worker) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent: exactly one 200", results.count(200) == 1, results)
check("concurrent: rest 409", results.count(409) == 4, results)
check("version incremented once",
      client.get(f"/v1/sessions/{sid}").json()["version"] == 4)

# 14. concurrent replaces same expected_version: exactly one succeeds
b = client.get(f"/v1/sessions/{sid}").json()
ver = b["version"]
results = []
barrier = threading.Barrier(5)


def worker_replace():
    barrier.wait()
    rr = client.put(f"/v1/sessions/{sid}/protocol",
                    json={"protocol": P2, "expected_version": ver})
    results.append(rr.status_code)


threads = [threading.Thread(target=worker_replace) for _ in range(5)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent replace: exactly one 200", results.count(200) == 1, results)
check("concurrent replace version +1",
      client.get(f"/v1/sessions/{sid}").json()["version"] == ver + 1)

# 15. malformed bodies -> 422
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1", "expected_version": -1})
check("negative version 422", r.status_code == 422)
r = client.post(f"/v1/sessions/{sid}/transitions",
                json={"transition_id": "t1"})
check("missing expected_version 422", r.status_code == 422)
r = client.put(f"/v1/sessions/{sid}/protocol",
               json={"expected_version": 4})
check("missing protocol 422", r.status_code == 422)

# 16. existing offline endpoints still work
r = client.get("/healthz")
check("healthz", r.status_code == 200 and r.json() == {"status": "ok"})
r = client.post("/v1/deadlocks", json=P)
check("deadlocks endpoint intact", r.status_code == 200)
r = client.post("/v1/completion-plans",
                json={**P, "required_transition_ids": ["t1"]})
check("completion-plans endpoint intact", r.status_code == 200
      and r.json()["completable"] is True)
r = client.post("/v1/deactivation-plans", json={**P, "removable_transition_ids": []})
check("deactivation-plans endpoint intact", r.status_code == 200)
r = client.post("/v1/completion-guarantees", json=P)
check("completion-guarantees endpoint intact", r.status_code == 200)

# 17. branch a session from a trace prefix
r = client.post("/v1/sessions", json=P)
src = r.json()["id"]
client.post(f"/v1/sessions/{src}/transitions",
            json={"transition_id": "t1", "expected_version": 0})
client.post(f"/v1/sessions/{src}/transitions",
            json={"transition_id": "t4", "expected_version": 1})
# source: version 2, trace [t1, t4], completed.

r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 2, "prefix_length": 1})
check("branch 201", r.status_code == 201, r.status_code)
b = r.json()
br1 = b["id"]
check("branch new id", bool(br1) and br1 != src)
check("branch version 0", b["version"] == 0)
check("branch trace len 1", len(b["trace"]) == 1)
check("branch trace step", b["trace"][0]["transition_id"] == "t1"
      and b["trace"][0]["index"] == 1)
check("branch config recomputed from prefix", b["configuration"] == {
    "state": {"A": 1, "B": 0}, "queues": {"A_to_B": "m", "B_to_A": None}},
    b["configuration"])
check("branch enabled recomputed", b["enabled_transition_ids"] == ["t4"],
      b["enabled_transition_ids"])
check("branch protocol matches source", b["protocol"]["transitions"] == P["transitions"]
      or b["protocol"]["transitions"][0]["id"] == "t1")

# source untouched: version not consumed, trace intact
b = client.get(f"/v1/sessions/{src}").json()
check("source unchanged after branch", b["version"] == 2 and len(b["trace"]) == 2)

# branch is queryable through the existing read endpoint
b = client.get(f"/v1/sessions/{br1}").json()
check("branch queryable", b["version"] == 0 and len(b["trace"]) == 1)

# same source version branches again (prefix 0 and full prefix)
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 2, "prefix_length": 0})
check("second branch same version 201", r.status_code == 201, r.status_code)
b = r.json()
br0 = b["id"]
check("prefix 0 empty trace", b["trace"] == [] and b["version"] == 0)
check("prefix 0 initial config", b["configuration"] == {
    "state": {"A": 0, "B": 0}, "queues": {"A_to_B": None, "B_to_A": None}})
check("prefix 0 enabled", b["enabled_transition_ids"] == ["t1"])
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 2, "prefix_length": 2})
check("full-prefix branch 201", r.status_code == 201, r.status_code)
b = r.json()
check("full-prefix branch completed", b["enabled_transition_ids"] == []
      and b["configuration"]["state"] == {"A": 1, "B": 1})
check("source still version 2", client.get(f"/v1/sessions/{src}").json()["version"] == 2)
check("branch ids distinct", len({br1, br0, b["id"]}) == 3)

# 18. branch and source evolve independently
r = client.post(f"/v1/sessions/{br1}/transitions",
                json={"transition_id": "t4", "expected_version": 0})
check("branch execute 200", r.status_code == 200 and r.json()["version"] == 1,
      (r.status_code, r.text))
check("source unaffected by branch execute",
      client.get(f"/v1/sessions/{src}").json()["version"] == 2)
r = client.put(f"/v1/sessions/{br1}/protocol",
               json={"protocol": P2, "expected_version": 1})
check("branch replace 200", r.status_code == 200 and r.json()["version"] == 2,
      (r.status_code, r.text))
check("branch protocol replaced", r.json()["protocol"]["transitions"][0]["target"] == 0)
b = client.get(f"/v1/sessions/{src}").json()
check("source protocol unaffected",
      b["protocol"]["transitions"][0]["target"] == 1 and b["version"] == 2)
# source-side change does not touch the branch either
r = client.put(f"/v1/sessions/{src}/protocol",
               json={"protocol": P2, "expected_version": 2})
check("source replace 200", r.status_code == 200 and r.json()["version"] == 3)
b = client.get(f"/v1/sessions/{br1}").json()
check("branch unaffected by source replace", b["version"] == 2
      and len(b["trace"]) == 2)

# 19. branch inherits the replaced protocol definitions (not the original ones)
r = client.post("/v1/sessions", json=P)
src2 = r.json()["id"]
client.post(f"/v1/sessions/{src2}/transitions",
            json={"transition_id": "t1", "expected_version": 0})
r = client.put(f"/v1/sessions/{src2}/protocol",
               json={"protocol": P2, "expected_version": 1})
check("setup replace 200", r.status_code == 200, r.status_code)
r = client.post(f"/v1/sessions/{src2}/branches",
                json={"expected_version": 2, "prefix_length": 1})
check("branch after replace 201", r.status_code == 201, r.status_code)
b = r.json()
check("branch uses replaced definitions",
      b["protocol"]["transitions"][0]["target"] == 0)
check("branch config under replaced defs", b["configuration"] == {
    "state": {"A": 0, "B": 0}, "queues": {"A_to_B": "m", "B_to_A": None}},
    b["configuration"])
check("branch enabled under replaced defs", b["enabled_transition_ids"] == ["t4"],
      b["enabled_transition_ids"])

# 20. branch error cases
r = client.post("/v1/sessions/deadbeef/branches",
                json={"expected_version": 0, "prefix_length": 0})
check("branch unknown source 404", r.status_code == 404, r.status_code)
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 0, "prefix_length": 1})
check("branch stale version 409", r.status_code == 409, r.status_code)
check("branch 409 carries current_version",
      r.json().get("detail", {}).get("current_version") == 3, r.json())
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 3, "prefix_length": 3})
check("prefix beyond trace 422", r.status_code == 422, r.status_code)
check("prefix 422 detail", r.json().get("detail", {}).get("error")
      == "invalid_prefix_length", r.json())
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 3, "prefix_length": -1})
check("negative prefix 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 3, "prefix_length": True})
check("boolean prefix 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 3, "prefix_length": "1"})
check("string prefix 422", r.status_code == 422, r.status_code)
r = client.post(f"/v1/sessions/{src}/branches",
                json={"expected_version": 3})
check("missing prefix 422", r.status_code == 422, r.status_code)
check("source unchanged after failed branches",
      client.get(f"/v1/sessions/{src}").json()["version"] == 3)

# 21. branch concurrent with a source modification on the same version:
# the source execute always wins its own version; the branch either copies the
# complete version-1 snapshot (201) or is rejected as stale (409).
r = client.post("/v1/sessions", json=P)
src3 = r.json()["id"]
client.post(f"/v1/sessions/{src3}/transitions",
            json={"transition_id": "t1", "expected_version": 0})
outcomes = {}
barrier = threading.Barrier(2)


def racer_branch():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{src3}/branches",
                     json={"expected_version": 1, "prefix_length": 1})
    outcomes["branch"] = (rr.status_code, rr.json())


def racer_execute():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{src3}/transitions",
                     json={"transition_id": "t4", "expected_version": 1})
    outcomes["execute"] = rr.status_code


threads = [threading.Thread(target=racer_branch), threading.Thread(target=racer_execute)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent source execute 200", outcomes["execute"] == 200, outcomes)
branch_status, branch_body = outcomes["branch"]
check("concurrent branch 201 or 409", branch_status in (201, 409), outcomes)
if branch_status == 201:
    check("concurrent branch copied v1 snapshot",
          branch_body["version"] == 0
          and [s["transition_id"] for s in branch_body["trace"]] == ["t1"]
          and branch_body["configuration"]["queues"] == {"A_to_B": "m", "B_to_A": None},
          branch_body)
else:
    check("concurrent branch stale current_version",
          branch_body.get("detail", {}).get("current_version") == 2, branch_body)
check("concurrent source final version 2",
      client.get(f"/v1/sessions/{src3}").json()["version"] == 2)

# 22. two concurrent branches of the same source version both succeed
results = []
branch_ids = []
barrier = threading.Barrier(4)


def multi_branch():
    barrier.wait()
    rr = client.post(f"/v1/sessions/{src3}/branches",
                     json={"expected_version": 2, "prefix_length": 2})
    results.append(rr.status_code)
    if rr.status_code == 201:
        branch_ids.append(rr.json()["id"])


threads = [threading.Thread(target=multi_branch) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("concurrent branches all 201", results.count(201) == 4, results)
check("concurrent branch ids distinct", len(set(branch_ids)) == 4, branch_ids)
check("source version unconsumed by branches",
      client.get(f"/v1/sessions/{src3}").json()["version"] == 2)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    raise SystemExit(1)
print("ALL CHECKS PASSED")
