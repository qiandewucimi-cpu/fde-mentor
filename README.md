# fde-mentor

`fde-mentor` 是一个面向 Codex 的 FDE（Forward Deployed Engineer）教学 skill。它会根据学习者的能力、目标和时间设计路线，也能按已有路线交付某一天的讲义、可运行练习与客户现场场景题。

它强调三件事：

- **先路由再产出**：普通概念问答直接回答，只有完整课程请求才进入课程交付流程。
- **用证据更新**：用户主动调整路线不受招聘趋势门槛限制；市场驱动更新必须有可审计的独立证据。
- **真实验证**：Python 练习从两个隔离工作目录运行，必须出现约定的行为标记，静默脚本或“退出码碰巧是 0”不会通过。

## 适用范围

适合：

- 为零基础或初级学习者制定 FDE 学习路线；
- 生成、复习或重做指定课次；
- 审查已有 FDE 讲义、练习和客户场景题；
- 在证据充分时更新路线中的市场能力要求。

不适合：

- 普通 Python 学习计划或未指明 FDE 的 `dayX` 请求；
- 简历润色、通用职业咨询；
- 未经授权安装模型、创建云资源或操作生产系统。

## 安装

把本仓库复制或克隆到 Codex skills 目录，并确保最终目录名是 `fde-mentor`：

```text
<CODEX_HOME>/skills/fde-mentor/
```

未设置 `CODEX_HOME` 时，Codex 通常使用 `~/.codex/skills/fde-mentor/`。重新启动或刷新 Codex 后即可自动发现；也可以显式调用 `$fde-mentor`。

## 使用示例

```text
用 $fde-mentor 为一个会 ERP 实施、不会 Python、每周有 10 小时的人设计 6 周制造业 FDE 路线。只在聊天里回答，不联网。
```

```text
用 $fde-mentor 读取现有学习总览，生成 W2D3 的完整课程。今天只讲 HTTP 超时排障，不进入容器编排；我没有本地模型环境。
```

```text
用 $fde-mentor 只读审查这份练习是否真的能从不同工作目录运行，不要修改文件。
```

## 仓库结构

```text
fde-mentor/
|-- SKILL.md
|-- agents/
|   `-- openai.yaml
|-- references/
|   |-- curriculum.md
|   |-- daily-delivery.md
|   `-- quality-gates.md
|-- scripts/
|   `-- selfcheck.py
|-- tests/
|   |-- test_selfcheck.py
|   `-- test_skill_package.py
|-- evals/
|   `-- behavior-cases.md
|-- .github/workflows/ci.yml
|-- LICENSE
`-- README.md
```

主入口只保留路由、权限和时效边界；课程设计、每日交付和质量门槛按需加载，减少无关上下文。

## 验证练习

检查器只依赖 Python 3.10+ 标准库。它会执行传入文件，因此只应用于当前任务内可信的生成代码。

```bash
python -I -S -B -X utf8 scripts/selfcheck.py \
  --practice <path-to-practice.py> \
  --answer <path-to-answer.py> \
  --scenario-answer <optional-scenario-answer.py> \
  --project-root <delivery-code-directory>
```

PowerShell 使用相同参数写成一行即可；路径中有空格时用引号包住对应路径。

默认会为每个目标、每次运行创建全新的源码快照和工作目录。练习版必须输出 `PRACTICE_INCOMPLETE`，答案版必须输出 `ANSWER_OK`，可选的 Python 场景答案必须输出 `SCENARIO_OK`。语法错误、非零退出、超时、traceback 文本、缺少标记、跨目标残留或依赖启动目录都会失败。

必须保留命令中的 `-I -S -B -X utf8`；检查器会拒绝普通 Python 启动，因为解释器可能在脚本加载前执行环境中的 site hook。省略 `--project-root` 时，每份快照只包含当前目标文件；只要代码会导入同目录模块或读取同目录数据，就应传入最窄的交付目录作为 `--project-root`。检查器还会清除子进程的 `PYTHON*` 环境变量，并在每次运行结束时收割后代进程；它仍会执行可信代码，不是恶意代码沙箱。

## 开发与测试

```bash
python -I -S -B -X utf8 -m unittest discover -s tests -v
```

行为回归用例和发布门槛见 [evals/behavior-cases.md](evals/behavior-cases.md)。CI 在 Windows 与 Ubuntu 上运行测试矩阵。

## License

[MIT](LICENSE)
