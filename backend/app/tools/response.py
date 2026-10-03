from typing import Any, Optional

class ToolResponse:
    def __init__(self, success: bool, data: Any = None, error: str = "", hint: str = "",
                 artifacts: Optional[dict] = None):
        self.success = success
        self.data = data
        self.error = error
        self.hint = hint
        # artifacts 是给"过程面板"用的结构化副产品（例如一次网页搜索的
        # {kind,query,results:[{title,url,snippet}]}）。它绝不进 to_string()——
        # 给模型看到的文本必须和引入这一字段之前逐字节一致，否则改的是模型输入而不
        # 只是给人看的留痕。默认 None：绝大多数工具（计算器/日程…）没有这类副产物。
        self.artifacts = artifacts

    def to_string(self) -> str:
        if self.success:
            return f"✓ {self.data}"
        return f"✗ 错误: {self.error}。建议: {self.hint}"
