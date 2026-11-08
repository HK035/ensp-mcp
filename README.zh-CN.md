# ensp-mcp · 让 AI agent 直接操作华为 eNSP

[English](README.md) · **简体中文**

eNSP 没有任何 API、没有插件接口、也不能远程控制。但实测发现：1.3 版本会给已加载拓扑里的**每一台设备**在 `127.0.0.1:2000+i` 上开一个**原生 VRP 控制台，且不鉴权**，同时把这个端口号以 `com_port` 属性写进保存的 `.topo` 文件。本项目就从这个门进去：一个 stdio MCP 服务器（8 个工具）、一份教 agent 安全使用它的 Qoder 技能，外加一个走同一套逻辑的命令行兜底。

全部结论基于 eNSP **1.3.00.100** + Windows 10 + VirtualBox 5.2.30 的实测。哪些已被真机验证、哪些只是合理推断，见[验证状态](#验证状态)。

```
ensp-mcp/
├── ensp_console.py         控制台通道：按端口复用的长连接、分页处理、GBK 解码、.topo 清单
├── ensp_mcp_server.py      MCP stdio 服务器（JSON-RPC 2.0，只用标准库）
├── install.py              注册 MCP 服务器 + 安装技能（默认 dry-run，不写任何东西）
├── selftest.py             14 项回归：握手、门禁、真机读写
└── skill/ensp-lab/
    ├── SKILL.md            agent 遵循的工作流
    ├── references/         console-facts.md、vrp-quirks.md（实测结论，不是网上抄的）
    └── scripts/ensp_cli.py 不依赖 MCP 的等价命令行
```

## 运行前提

- Windows，eNSP 1.3 已启动且**已加载拓扑**（设备上电）。
- Python 3.10+（PATH 上有，或用完整路径）。**不依赖任何 PyPI 包**，整套只用标准库，所以断网的机房机器也能直接跑。
- 想在 agent 会话里用工具，需要 Qoder 或任意 MCP 客户端。

## 安装

```bash
git clone https://github.com/HK035/ensp-mcp.git
cd ensp-mcp
python install.py            # dry-run：只打印将要改什么，不落盘
python install.py --apply    # 真正写入
```

`install.py` 计算的是**你自己机器上的路径**，不含任何写死的家目录。它做两件事：把一条记录合并进 `%USERPROFILE%\.qoder-cn\settings.json` 顶层的 `mcpServers`（先备份原文件，`providers`、`enabledPlugins` 等键原样保留），再把 `skill/ensp-lab` 以 junction 链到 `%USERPROFILE%\.qoder-cn\skills\ensp-lab`。脚本可重复执行：已经装好就说"无事可做"，仓库挪了位置就自动改写路径；如果技能目录位子上是**别人的真实目录**，它会拒绝，`--force` 也只是把原目录挪开而不是删除。

**装完请新开一个会话。** MCP 服务器只在会话建立时挂载，所以安装用的那个会话里不会出现 `ensp_*` 工具。

不想用脚本的话，走 **设置 → MCP → Add server** 也行，它写的是同一个文件。手工编辑时注意 IDE 校验器的规则（这些是从 IDE 包里读出来的，写错会静默失败）：

| 规则 | 违反时报错 |
| --- | --- |
| stdio 条目 = `command` + 可选 `args` / `env` / `cwd` | `MCP_CONFIG_TRANSPORT_REQUIRED` |
| `type` 省略或只能写 `"stdio"` | `MCP_CONFIG_TYPE_INVALID` |
| **不能写 `enabled`**，只认 `disabled: true` | `MCP_CONFIG_ENABLED_UNSUPPORTED` |
| 用户作用域下 `cwd` 必须是绝对路径 | `MCP_CONFIG_CWD_ABSOLUTE_REQUIRED` |
| 配置文件必须是真实文件，不能是符号链接 | `MCP_CONFIG_SYMLINK_FORBIDDEN` |
| 服务器名：Unicode 字母数字与 `._`、空格、连字符 | `MCP_CONFIG_ENTRY_KEY_INVALID` |

## 验证

```bash
python selftest.py --device 2001
```

14 项检查：JSON-RPC 握手、工具清单、设备清单、只读与 `confirm` 两道门禁、按名寻址、真机 `display clock`、备份结果**不得残留** `---- More ----`、以及一个**净零变化**的配置批次（把设备自己的 `sysname` 原样重下一遍，跑完运行配置必须不变）。

eNSP 没开时，5 项依赖设备的检查会明确报 SKIP 而不是误判 FAIL，目标端口也从 `ensp_status` 实际在听的端口里取，不写死 2001。

不经过 MCP 也能用同一套处理函数：

```bash
python skill/ensp-lab/scripts/ensp_cli.py status
python skill/ensp-lab/scripts/ensp_cli.py devices
python skill/ensp-lab/scripts/ensp_cli.py read 2001 "display vlan"
python skill/ensp-lab/scripts/ensp_cli.py exec 2001 "system-view" "vlan batch 100"
```

## 工具一览

| 工具 | 作用 | 是否写设备 |
| --- | --- | --- |
| `ensp_status` | eNSP 进程、在听的控制台端口、匹配到的 `.topo`、VirtualBox 状态，并给出下一步该修什么 | 否，只读 `tasklist`/`netstat` |
| `ensp_list_devices` | 端口 → 名字/型号/sysname/当前视图，并判定 online-offline-silent | 否 |
| `ensp_read` | 执行一条 `display`/`ping`/`tracert`，自动翻页并清洗输出 | 否 |
| `ensp_exec` | 下发一批 VRP 命令，逐条返回输出与错误标记 | 是，受门禁 |
| `ensp_backup` | 运行配置存到 `~/.ensp-mcp/backups/<名字>-<时间戳>.cfg` | 只写本地文件 |
| `ensp_save_config` | 执行 VRP `save` 并应答确认提示 | 是，需 `confirm=true` |
| `ensp_wait_for` | 轮询只读命令直到某个正则出现或消失（等 AP 上线这类异步状态） | 否 |
| `ensp_release` | 把控制台槽位交还给 GUI | 否 |

## 设计要点

每条都是拿实测换来的，不是想当然：

- **每个端口一条长连接、复用、加锁串行。** 探索阶段大约十次快速建连/断开之后，`eNSP_VBoxServer.exe` 和所有 `VBoxHeadless.exe` 一起退出，一台 AC 和两个 AP 整场再也连不上。诱因没被严格证明，但结论足够硬：工具绝不"建连风暴"——连接复用、失败端口 5 秒冷却、端口清单靠 `netstat` 而不是靠探测。
- **`##\x00` 表示"没上电"**，不是坏了。对已关机 VBox 设备只发回车会**毫无回音**，所以判活要补发一条无害命令（`display clock`）。
- **console 线关不掉分页。** `screen-length 0 temporary` 在控制台上回 `Error: The type of the included user interface must be VTY.`，只有 VTY 支持。因此分页必须**带内发空格**处理并清洗残留，否则抓下来的配置是脏的。
- **回复是 GBK、命令会回显、提示符常常连打两遍。** 三者都归一化了，"命令结束"的判据是提示符之后陷入静默，而不是固定等多少毫秒。
- **设备名只有在保存拓扑后才存在**，因为 `com_port`/`name`/`model` 是 eNSP 存盘时才写的。没存也能用：工具会从提示符里学 sysname 当别名，并按端口去重——同一台机器在三种视图里被看到三次，不该算"歧义"。
- **写操作有两道闸**：`ensp_read` 只接受 `display`/`ping`/`tracert`；`ensp_exec`/`ensp_save_config` 对会落盘或破坏性的命令必须显式 `confirm=true`。
- **抓到的配置不进仓库。** 设备配置含口令，所以备份默认写到 `~/.ensp-mcp/backups`，`*.cfg` 也在 `.gitignore` 里。

## 隐私与安全

这条通道**不鉴权且与 GUI 共享**：本机任何进程只要能连 2000+ 端口，就拿到了那些仿真设备的完全控制权。对 eNSP 这种离线仿真器来说可以接受，但不要把该端口暴露出去，也不要把 `backups/*.cfg` 提交到仓库。批量下发期间别同时在 eNSP 的 CLI 窗口里敲命令（`ensp_release` 会把槽位交还）；让 agent 无人值守驱动实验前，先读一遍 [skill/ensp-lab/SKILL.md](skill/ensp-lab/SKILL.md)。

## 验证状态

把话说清楚，因为这是个会去动"你的网络"的工具：

- **已在仿真设备上验证**：进程内模拟的 S5700 交换机（VRP 5.110 V200R001C00）——读取、备份、分页清洗、按名寻址、净零配置批次、以及对三台已关机 VBox 设备的离线判定与各类门禁拒绝。
- **未验证**：VBox 承载的 AR/AC/AP 控制台只在早期探索时短暂连通过（那几台随后就关机了），所以**写路径只在进程内交换机上验过**；`ensp_save_config` 从未在真设备上跑过（`save` 会把别人未保存的手工改动一并固化），它的应答流程是按 VRP 行为推断的，不是实测。
- **单机单版本**：2000 端口分配、`##\x00` 哨兵、console 拒绝 `screen-length`，都是本机 eNSP 1.3.00.100 的特性。

## 不覆盖的范围

- 搭建或修改拓扑：eNSP 无对应 API，手写的 `.topo` 不会热加载，而 `VBoxManage` 会和 eNSP 抢同一批虚机。拓扑请手工画。
- PC / Server / 移动台这类端点：它们是 GUI 插件，没有 VRP 控制台，只会返回 `silent`。要验证连通性，请在拓扑里用路由器或交换机去打。

## 许可

MIT，见 [LICENSE](LICENSE)。
