"""#231：PUT 的 body.id 与路径 id 不一致必须响亮 400（2026-11-25 收口）。

为什么钉：两路 PUT（`/v1/providers/{id}` 与 `/v1/me/providers/{id}`）过去把路径参数塞进
`record["id"]`、**静默以路径为准**，body 里那个 id 被无声丢弃。而 MCP 面对同一件事是响亮
的 400——两种口径并存，最坏的结果是「客户端以为改的是 A、实际改的是 B」而没有任何一处
告诉它。这条锁真调两个端点，把四种输入都走一遍。
"""

BODY = {"label": "探针", "base_url": "https://api.openai.com/v1",
        "api_key": "sk-put231-0123456789abcdef", "model": "m-1"}


def _pid(client, hdr):
    r = client.post("/v1/me/providers", json=BODY, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["provider"]["id"]


def test_put_rejects_a_body_id_that_disagrees_with_the_path(client, enforced):
    hdr = enforced("put231")
    pid = _pid(client, hdr)
    r = client.put(f"/v1/me/providers/{pid}", json=dict(BODY, id="p_someone_else"),
                   headers=hdr)
    assert r.status_code == 400, f"body.id 与路径不一致却放行了：{r.status_code} {r.text[:200]}"
    assert "不一致" in r.json().get("detail", ""), r.text[:200]


def test_put_accepts_an_absent_blank_or_identical_body_id(client, enforced):
    """三种「正常形状」都必须放行：不带、纯空白、与路径相同。"""
    hdr = enforced("put231-ok")
    pid = _pid(client, hdr)
    for label, extra in (("no-id", {}), ("blank", {"id": "   "}),
                         ("identical", {"id": None})):
        body = dict(BODY, **extra)
        if label == "identical":
            body["id"] = pid
        r = client.put(f"/v1/me/providers/{pid}", json=body, headers=hdr)
        assert r.status_code == 200, f"{label} 被误伤：{r.status_code} {r.text[:200]}"
