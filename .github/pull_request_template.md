<!--
提交前自查（详见 CONTRIBUTING.md）：
- [ ] `python -m pytest backend/tests -q` 零红（用 venv 里那份解释器）；改过 android-native/core 再加跑 `python tools/shell_jvm_tests.py`
- [ ] 新接口带鉴权（/v1/* 必须出现在 current_principal/require_admin 依赖树里，契约测试会自动红）
- [ ] 双端改文案的话，跑过双端契约测试（test_schedule_ui_contract 那族）
- [ ] commit message 按 R5 说人话：干了什么、为什么，不写"enhance/optimize"空话
- [ ] 没有把任何 Key、token、真实用户数据带进 diff
-->

**这个 PR 解决什么**

**怎么验的**
