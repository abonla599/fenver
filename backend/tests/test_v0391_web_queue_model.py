# backend/tests/test_v0391_web_queue_model.py
import json, shutil, subprocess, tempfile
from pathlib import Path
import pytest
from tests.test_web_pwa import _fn_text, _js

HARNESS = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
ctx.state={queues:{}};
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
const q1="s1", q2="s2";
const added = ctx.enqueueQueueItem(q1,{id:"a",text:"A",attachments:[],k:1});
const added2 = ctx.enqueueQueueItem(q1,{id:"b",text:"B",attachments:[],k:2});
const addedOther = ctx.enqueueQueueItem(q2,{id:"c",text:"C",attachments:[],k:1});
const first = ctx.nextQueueItem(q1);
const guardHidden = ctx.canRelay(q1,q1,false,"hidden");
const guardStreaming = ctx.canRelay(q1,q1,true,"visible");
const guardOther = ctx.canRelay(q1,q2,false,"visible");
const guardOk = ctx.canRelay(q1,q1,false,"visible");
ctx.finishQueueItem(q1,"a");
ctx.pauseQueue(q1,true);
const paused = ctx.queueFor(q1).paused;
ctx.resetQueuesForIdentity();
process.stdout.write(JSON.stringify({added,added2,addedOther,first,guardHidden,guardStreaming,guardOther,guardOk,paused,left:ctx.queueFor(q1).items.length}));
"""


def test_queue_state_machine():
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node")
    src = "\n".join(_fn_text(_js(), n) for n in (
        "queueFor","enqueueQueueItem","nextQueueItem","finishQueueItem",
        "pauseQueue","canRelay","resetQueuesForIdentity"))
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-q-"))
    (d/"fns.js").write_text(src, encoding="utf-8")
    (d/"harness.cjs").write_text(HARNESS, encoding="utf-8")
    r = subprocess.run([node, str(d/"harness.cjs"), str(d/"fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    out = json.loads(r.stdout)
    assert out["added"] and out["added2"] and out["addedOther"]
    assert out["first"]["id"] == "a"
    assert out["guardHidden"] is False and out["guardStreaming"] is False and out["guardOther"] is False
    assert out["guardOk"] is True
    assert out["paused"] is True and out["left"] == 0
