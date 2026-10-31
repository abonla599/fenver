# backend/tests/test_v0391_web_queue_model.py
import json, shutil, subprocess, tempfile
from pathlib import Path
import pytest
from tests.test_web_pwa import _fn_text, _js

# queueUiRefresh 是 Task 4 给四个变更函数加的刷新出口（typeof 守卫，无 DOM 环境
# 里等于不存在）：它现在是状态机的一部分，摘函数必须一起摘，否则 vm 里 ReferenceError。
FUNCS = ("queueKey", "queueFor", "enqueueQueueItem", "nextQueueItem",
         "finishQueueItem", "pauseQueue", "canRelay", "resetQueuesForIdentity",
         "queueUiRefresh")

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

# Fix Round 1 取证一：空会话归一。调用方混用 ""/null 时必须落同一桶、
# canRelay 归一比较后为真；哨兵 "__draft__" 不许和普通 id（含字面串 "draft"）混桶；
# canRelay 还必须纯读——查不存在的会话不许建桶。
# 口径固有局限（钉住它，改口径必须让这条变红）：按"真 id 用 String()"的归一口径，
# 字面值恰为 "__draft__" 的真 id 与空会话同桶——现网会话 id 非此形状，接受。
HARNESS_DRAFT_KEY = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
ctx.state={queues:{}};
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
ctx.enqueueQueueItem(null,{id:"d1",text:"D",attachments:[],k:1});
ctx.enqueueQueueItem("draft",{id:"l1",text:"L",attachments:[],k:1});
const draftCanRelay=[
  ctx.canRelay("","",false,"visible"),
  ctx.canRelay(null,"",false,"visible"),
  ctx.canRelay("",null,false,"visible"),
  ctx.canRelay(null,null,false,"visible"),
];
const crossBucket=[
  ctx.canRelay("","draft",false,"visible"),
  ctx.canRelay("draft","",false,"visible"),
];
const literalDraftOk=ctx.canRelay("draft","draft",false,"visible");
const sentinelSharesDraft=ctx.canRelay("__draft__","__draft__",false,"visible");
const before=Object.keys(ctx.state.queues).sort().join(",");
ctx.canRelay("never-seen","never-seen",false,"visible");
ctx.canRelay("","",false,"visible");
const readOnly=Object.keys(ctx.state.queues).sort().join(",")===before;
process.stdout.write(JSON.stringify({draftCanRelay,crossBucket,literalDraftOk,sentinelSharesDraft,readOnly}));
"""

# Fix Round 1 取证二：入队上限的边界语义——第 21 条拒、恰好 20000 字收、
# 20001 字拒，且拒绝不动已有条目。
HARNESS_LIMITS = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
ctx.state={queues:{}};
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
let ok=0;
for(let i=0;i<20;i++) if(ctx.enqueueQueueItem("cnt",{id:"i"+i,text:"n",attachments:[],k:1})) ok++;
const twentyOne=ctx.enqueueQueueItem("cnt",{id:"x",text:"n",attachments:[],k:1});
const countLen=ctx.queueFor("cnt").items.length;
const a=ctx.enqueueQueueItem("chars",{id:"a",text:"A".repeat(19999),attachments:[],k:1});
const b=ctx.enqueueQueueItem("chars",{id:"b",text:"B",attachments:[],k:2});
const c=ctx.enqueueQueueItem("chars",{id:"c",text:"C",attachments:[],k:3});
const charsLen=ctx.queueFor("chars").items.length;
process.stdout.write(JSON.stringify({ok,twentyOneRejected:twentyOne===false,countLen,a,b,cRejected:c===false,charsLen}));
"""

# Fix Round 1 取证三：领取锁用 !==null 判——条目 id 为 0 或空串时锁照样生效；
# finishQueueItem 只在 id 匹配时释放，不匹配不许把别人的锁清掉。
HARNESS_SENDING_LOCK = r"""
const fs=require("fs"), vm=require("vm");
const ctx={}; vm.createContext(ctx);
ctx.state={queues:{}};
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), ctx);
ctx.enqueueQueueItem("z",{id:0,text:"one",attachments:[],k:1});
ctx.enqueueQueueItem("z",{id:"two",text:"two",attachments:[],k:2});
const zeroFirst=ctx.nextQueueItem("z");
const zeroLocked=ctx.nextQueueItem("z");
ctx.finishQueueItem("z","wrong");
const zeroStillLocked=ctx.nextQueueItem("z");
ctx.finishQueueItem("z",0);
const zeroReleased=ctx.nextQueueItem("z");
ctx.enqueueQueueItem("es",{id:"",text:"one",attachments:[],k:1});
ctx.enqueueQueueItem("es",{id:"two",text:"two",attachments:[],k:2});
ctx.nextQueueItem("es");
const esLocked=ctx.nextQueueItem("es");
ctx.finishQueueItem("es","x");
const esStillLocked=ctx.nextQueueItem("es");
ctx.finishQueueItem("es","");
const esReleased=ctx.nextQueueItem("es");
process.stdout.write(JSON.stringify({
  zeroFirstId:zeroFirst&&zeroFirst.id,zeroLocked,zeroStillLocked,
  zeroReleasedId:zeroReleased&&zeroReleased.id,
  esLocked,esStillLocked,esReleasedId:esReleased&&esReleased.id}));
"""


def _run(harness: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("这台机器上没有 node")
    src = "\n".join(_fn_text(_js(), n) for n in FUNCS)
    d = Path(tempfile.mkdtemp(prefix="model-cockpit-q-"))
    (d/"fns.js").write_text(src, encoding="utf-8")
    (d/"harness.cjs").write_text(harness, encoding="utf-8")
    r = subprocess.run([node, str(d/"harness.cjs"), str(d/"fns.js")],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    return json.loads(r.stdout)


def test_queue_state_machine():
    out = _run(HARNESS)
    assert out["added"] and out["added2"] and out["addedOther"]
    assert out["first"]["id"] == "a"
    assert out["guardHidden"] is False and out["guardStreaming"] is False and out["guardOther"] is False
    assert out["guardOk"] is True
    assert out["paused"] is True and out["left"] == 0


def test_queue_key_normalizes_draft_and_stays_pure_read():
    out = _run(HARNESS_DRAFT_KEY)
    assert out["draftCanRelay"] == [True, True, True, True], \
        "空会话的 \"\"/null 混用必须归一后仍能接力（Fix R1：canRelay 恒 false 的那条）"
    assert out["crossBucket"] == [False, False], \
        "哨兵桶不许和普通 id（字面串 \"draft\" 也是普通 id）串桶"
    assert out["literalDraftOk"] is True
    assert out["sentinelSharesDraft"] is True, \
        "固有局限已登记：真 id 恰为 \"__draft__\" 与空会话同桶（现网会话 id 非此形状）"
    assert out["readOnly"] is True, "canRelay 是纯读：查询不许建空桶"


def test_queue_limits_reject_21st_item_and_over_20000_chars():
    out = _run(HARNESS_LIMITS)
    assert out["ok"] == 20 and out["countLen"] == 20 and out["twentyOneRejected"] is True
    assert out["a"] is True and out["b"] is True, "恰好 20000 字必须收"
    assert out["cRejected"] is True and out["charsLen"] == 2, "超 20000 字拒，已有条目不动"


def test_queue_sending_lock_survives_falsy_ids_and_finish_needs_match():
    out = _run(HARNESS_SENDING_LOCK)
    assert out["zeroFirstId"] == 0
    assert out["zeroLocked"] is None and out["zeroStillLocked"] is None, \
        "id 为 0/空串的条目领取后锁不许失效（真值判据 q.sendingId 会把 0 当没锁）"
    assert out["zeroReleasedId"] == "two", "finishQueueItem id 匹配才清锁"
    assert out["esLocked"] is None and out["esStillLocked"] is None
    assert out["esReleasedId"] == "two"
