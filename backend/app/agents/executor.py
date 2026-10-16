"""执行者智能体 - 用 TaskAgent 执行单个子任务"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # backend 目录

from app.agents.task_agent import TaskAgent
from app.tools.registry import tools_registry
from app.tools.registry import get_available_tools_schema


class Executor:
    """工具就取全局注册表那一份。

    这里原先挂的是 `agents/temp_tools.py`——一套平行的临时工具：calculator 是裸
    `eval`（同一件事在 builtin_tools 里已经因为 eval 修过一遍），web_search 更糟：
    它是一张写死的"马斯克/火箭回收"问答表，schema 却对模型宣称"搜索互联网获取信息"。
    也就是说模型以为自己在查资料，实际拿到的是几条编好的句子，然后一本正经地引用进
    回答里——这正是本项目最贵的那类失败：效果没了，还一声不响。
    """

    def __init__(self, model: str = None):
        # model 留空 = 走 setDefault 的那份 provider 配置（口径同 ReActAgent）。
        # 这里原先写死一个服务商名当默认值：服务商名不该刻进被跟踪的源码。
        self.model = model

    def execute_task(self, task: str, user_id: str = None,
                     provider_id: str = None, resumed_tool: dict = None) -> str:
        """执行单个子任务。user_id 与 provider_id 一路带下去：
        子任务里的每次模型调用都走发起人的池子、记发起人的账（v0.25 R1）。

        `resumed_tool`（⭐ v0.35 A3）原样透给 TaskAgent，不在这里做任何判断：这一层
        只是"用 TaskAgent 跑一个子任务"的薄壳，断点怎么接、接不接得回去，判据一处
        在执行器（tools/executor.py）、一处在编排器，中间不许再长第三份。
        工具挂起时这一层也不接 `TaskNeedsUser`——它没有 Task，接了也落不了盘，
        让它继续外飘给编排器（那才是唯一有 Task、能写 waiting_user 的地方）。

        末行那个条件展开是有意的：`resumed_tool` 为空时**一个参数都不多给**，于是
        按老签名替过 `TaskAgent.run` 的道具或调用方仍然逐字可用——多一个关键字参数的
        症状不是"某个道具没跟上"，而是线上"任务跑坏了"（TypeError 会落进编排器那个
        兜底 except，把一条正常任务标成 failed）。
        """
        agent = TaskAgent(
            name="Executor",
            model=self.model,
            tools_schema=get_available_tools_schema(),
            tools=tools_registry,
            user_id=user_id,
            provider_id=provider_id,
            system_prompt="你是一个执行助手。用可用工具完成任务，返回简洁准确的结果。"
        )
        return agent.run(task, **({"resumed_tool": resumed_tool} if resumed_tool else {}))
