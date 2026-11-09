"""移动端屏幕上下文的上传 / 撤销端点（规划书 §9.4 的 B 轨）。

两条都挂身份依赖：`/v1/` 前缀本就受 authz 中间件保护，这里再从 `CurrentPrincipal`
取服务端算出来的 user_id 做隔离键——请求体里没有、也不许有归属人字段。
旧 APK 不认这两个端点，纯新增，不改动任何既有接口。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core import device_context
from app.core.authz import CurrentPrincipal, Principal

router = APIRouter(tags=["设备上下文"])


@router.post("/v1/device/context")
def post_device_context(payload: device_context.DeviceContextPayload,
                        principal: Principal = CurrentPrincipal):
    """收一份设备侧已脱敏的快照。回包只报结果与 TTL，绝不回显屏幕文本。"""
    ok, reason, status = device_context.context_store.ingest(payload, principal.user_id)
    body = device_context.ingest_response_shape(ok, reason)
    if not ok:
        return JSONResponse(status_code=status, content=body)
    return body


@router.delete("/v1/device/context")
def delete_device_context(device_id: str | None = None,
                          principal: Principal = CurrentPrincipal):
    """用户随时停止共享：撤自己名下一台或全部设备的快照（§9.8 可达性 ≤2 步）。"""
    revoked = device_context.context_store.revoke(principal.user_id, device_id)
    return {"ok": True, "revoked": revoked}
