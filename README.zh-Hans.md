# callbluff

[English](README.md) · 简体中文

callbluff 是给 Claude Code 和 Codex 用的插件：AI 说「做完了」之前，必须先拿出证据。

AI 最后交差时说的经常和实际干的对不上，我做 callbluff 就是为了拦住这些情况。它由十个小钩子组成，每个盯一种脱节：留下跑不起来的代码、测试失败了就悄悄改松、改了至少 4 个文件只说「全做完了」却不列清单、说快了三倍却没跑计时、说测试全过却一条命令都没跑、没写文件也没 commit 就说修好了。

钩子拦下时会返回退出码 2（或让你确认），直接告诉 AI 缺的是什么。只用 Python 3 标准库，所有东西都只在你自己的电脑上，不往外发。

## 安装

需要 `python3`（`consumer_sweep` 还需要 `git`）。在 macOS 上用 Python 3.9、3.11、3.12、3.13、3.14 测过，Windows 和 Linux 没测过。

不想安装直接试跑一次会话：`claude --plugin-dir /path/to/callbluff`。

Claude Code：

```
/plugin marketplace add ninondev/callbluff
/plugin install callbluff@callbluff
```

Codex：

```
codex plugin marketplace add ninondev/callbluff
codex plugin add callbluff@callbluff
```

在 Codex CLI 0.159.2 里实测过：AI 在两行代码的文件里写了 `return a + c`（`c` 未定义）就说「Done.」，被 `execution_check` 拦下后它自己把 `c` 改成 `b` 并跑了一遍检查。还没从 GitHub 装过。

DeepSeek Harness：仓库里有配置 `hooks/dsh.json`，接到它的 Claude Code 钩子桥上用，还没实测过。那边的钩子拿不到 AI 最后那条回复，所以十个里只有五个能用。

常用命令与配置：

- `/callbluff:report` 看最近 7 天哪些钩子响过（支持 `--days=N` 或 `--all`）。
- `/callbluff:audit` 扫一遍当前仓库的语法错误和未定义的名字。
- 触发记录保存在本地 `~/.callbluff/firelog.jsonl`，设 `CALLBLUFF_NO_FIRELOG=1` 可关闭，或在根目录加 `.callbluff.json` 配置 `protected_globs` 改受保护的测试路径。

## 两种模式

默认按会话用的模型来分：opus、sonnet、fable 这几个系列的模型，读字面的那几个钩子和 `pilot_first` 只记录、不拦截，`acceptance_file_guard` 只在测试刚失败就被改松时确认；别的模型和认不出的模型，所有钩子都直接拦，Codex 里的 GPT 模型也算在这一类。这是我手动定的默认规则，还没有实测数据支撑，可以在 `gates.json` 里改。

## 实测数据与修复

我拿自己 6 月到 10 月的使用记录核查了这套钩子，装之前建议先看。下面这些数是修之前那一版测的：

- 逐条判过的 84 次触发里（从 306 次未判记录里分层抽样 59 次，加上更早判过的 25 次），只有 5 次抓到真问题（抽样命中率 8.5%，加权约 4.8%），而且都是小问题，比如文档没跟着改。没有一次抓到「谎称测试通过」或坏掉的代码。
- 40 次拦截模式的触发后，AI 改了做法的有 13 次，拿出证据反驳的有 3 次，重申原话 8 次，重新解释已知误报 14 次，2 次不明确。
- 误报最多的是同一个问题重复了 35 次：一个没人碰过的文件用了 Python 3.12 的新语法（`class C[T]`），检查器看不懂，还算到了当前会话头上。`acceptance_file_guard` 抽样的 16 次记录也全是新建测试文件或临时目录。这一版把这两处和 `delivery_inventory` 的误判都修好了，`execution_check` 现在只检查当前会话写过的文件。
- 1,075 条「做完了」里，873 条和代码有关。严格算，约 1.2% 没验证；宽一点算，约 9.2%。测试失败了却隐瞒不说的约 0.1%。

真正的大问题在别处：转述子 agent 的结果自己不重跑，还有测试都过了、在自己电脑上却跑不起来。

测量局限：一个人、一台机器、一个评判者（一个 Claude 模型，照写明的标注规则判，没有双盲）。修好以后还没重新测误报率。完整核查报告见 [agent-done-audit](https://github.com/ninondev/agent-done-audit)。

## 局限

- 读字面的钩子靠正则，认英文和中文，只匹配字面、不懂上下文意思，会有误报。
- 只看一轮对话最后那条回复。`execution_check` 只看得到用编辑工具写的文件，看不到 shell 命令写的；静态检查也会把运行时注入的名字报成未定义。
- `consumer_sweep` 和 `anchor_regression` 只对 Python 项目有效。

## 许可

MIT，见 [LICENSE](LICENSE)。欢迎提 PR，先看 [CONTRIBUTING.md](CONTRIBUTING.md)，issue 我都会看。

## 下一步

在修好误报的这一版上重新测一遍触发率和准确率，再决定默认开哪些钩子。增加更多语言的检查规则。
