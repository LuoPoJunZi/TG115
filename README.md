# TG115

TG115 是一个自建的个人文件转存工具：把文件转发给自己的 Telegram Bot，由 VPS 持久排队，
再通过 rclone 和 CloudDrive2 WebDAV 写入自己挂载的 115 网盘。

本仓库是在原作者 [whyhhh20/TG115](https://github.com/whyhhh20/TG115) 基础上的二次开发版本，
重点加强了任务恢复、磁盘保护、流式传输、状态语义、升级回退、VPS 资源建议和日常诊断。

> 仅转存你有权保存、备份和使用的内容，并遵守 Telegram、115、CloudDrive2、内容来源平台及
> 所在地的法律法规和服务条款。

## 目录

- [工作原理](#工作原理)
- [主要能力](#主要能力)
- [重要边界](#重要边界)
- [部署前准备](#部署前准备)
- [Windows 图形化部署](#windows-图形化部署)
- [CloudDrive2 与 115 配置](#clouddrive2-与-115-配置)
- [真实 WebDAV 验收](#真实-webdav-验收)
- [日常使用](#日常使用)
- [Bot 命令](#bot-命令)
- [VPS 运维与升级](#vps-运维与升级)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [本地开发与测试](#本地开发与测试)
- [致谢与项目来源](#致谢与项目来源)

## 工作原理

```text
Telegram 私聊 Bot
        ↓
SQLite 持久任务队列
        ↓
普通文件：先完整下载到 VPS
大文件：Telegram → rclone 流式管道
        ↓
CloudDrive2 WebDAV
        ↓
用户挂载的 115 网盘
```

TG115 的定位是“手动选择、自动处理”：你在 Telegram 中挑选文件并转发给私人 Bot，后续排队、
下载、上传、校验和本地清理由 VPS 自动完成。当前版本不自动监听频道，也不批量抓取频道历史。

## 主要能力

- Windows 10/11 图形化部署器，支持密码和 SSH 私钥登录；
- 首次连接显示 VPS 主机密钥指纹，确认后才保存；
- 只读探测 CPU、内存、安装盘、Docker 数据盘、inode 和 FUSE；
- 根据当前 VPS 提供“均衡模式”和“流式优先”两套存储建议；
- Telegram 私聊 Bot 鉴权，只允许配置的数字 ID 使用；
- SQLite 持久队列、原子磁盘额度预留、去重和重启恢复；
- 普通下载与流式任务共享真实上传并发窗口；
- 按 CPU、内存、磁盘、阶段吞吐、错误率和队列积压动态调节并发；
- 单文件超过本地任务预算时自动使用流式模式；
- WebDAV 临时文件、大小校验、安全改名、正式文件复验和本地清理；
- 上传失败保留普通模式的完整本地文件；
- 持久暂停、批量重试、任务进度订阅、只读诊断和遗留临时文件巡检；
- 部署前预检、SQLite 一致性快照、代码和配置指纹核验、失败自动回退；
- 历史备份只读盘点和显式保留，不在部署时自动删除回退点；
- Bot 容器以非 root、只读根文件系统、无 capabilities 方式运行。

## 重要边界

- 当前只支持配置用户与 Bot 的一对一私聊；群组和频道消息不会创建任务。
- TG115 只验证 CloudDrive2 WebDAV 已收到同样大小的文件，不接入 115 官方完成接口。
- Bot 日常界面统一显示“Bot 完成”，不再要求或提示用户逐个执行人工确认。
- 远端目前以文件大小为主要完整性依据，不提供内容哈希保证。
- 流式模式不在 VPS 保存完整副本，传输中断后通常需要从头重试。
- CloudDrive2 是第三方闭源软件，其内部缓存不受 Bot 本地任务预算直接控制。

## 当前版本

当前正式版为 `v1.6.0`。本地完整回归与 GitHub Actions 已通过；真实 VPS、Telegram、
CloudDrive2 和 115 仍应在部署后按验收记录分层核对。

详细改动和验证边界见 [v1.6.0 优化与验收记录](docs/v1.6.0-优化与验收.md)。

## 部署前准备

### 推荐环境

| 项目 | 基础建议 | 批量使用建议 | 说明 |
| --- | --- | --- | --- |
| VPS | 2 核、4GB 内存、50GB SSD | 4 核、8GB 内存、80～100GB SSD | 最终以部署器实时探测为准 |
| 系统 | Ubuntu 22.04/24.04 或 Debian 12 64 位 | 同左 | 支持 x86_64、ARM64 |
| 权限 | root 或可用 sudo | 同左 | 受管 CloudDrive2 需要 `/dev/fuse` |
| 网络 | 可访问 Telegram、Docker 镜像仓库和目的端 | 100Mbps 以上并准备足够流量 | 公网测速不能代表到 115 的真实速度 |
| 本地电脑 | Windows 10/11 64 位 | 同左 | 用于运行部署器和 SSH 隧道 |

安装脚本会硬检查约 1.8GB 内存和安装文件系统至少 8GB 可用空间；50GB 是推荐值，不是简单的
硬门槛。部署器还会结合已有下载、Docker 分区、inode 和最终填写的预算做更严格复检。

### 需要准备的信息

VPS：

- IP 地址或域名、SSH 端口、SSH 用户名；
- VPS 密码，或者 SSH 私钥和可选私钥口令；
- 非 root 用户的 sudo 密码，免密 sudo 时可留空。

Telegram：

- 从 `@BotFather` 获取的 Bot Token；
- 从 [my.telegram.org](https://my.telegram.org) 获取的 API ID 和 API Hash；
- 你自己的 Telegram 数字 ID。

CloudDrive2 / 115：

- 可用的 CloudDrive2 环境；
- 在 CloudDrive2 中单独创建的 WebDAV 用户名和密码；
- 已准备好由本人登录或扫码挂载的 115 账号；
- 足够的 115 剩余空间。

部署器不需要 115 密码、CloudDrive2 会员密码、Telegram 私人账号密码或验证码。
更完整的准备清单见 [一键部署前填写信息清单](docs/填写信息清单.md)。

## Windows 图形化部署

### 1. 获取并校验部署器

正式版本优先从本仓库的 [GitHub Releases](https://github.com/LuoPoJunZi/TG115/releases) 下载
EXE 和 `SHA256SUMS.txt`。也可在 Windows PowerShell 中从源码构建：

```powershell
git clone https://github.com/LuoPoJunZi/TG115.git
cd TG115
.\build.ps1
```

构建结果位于：

```text
dist/TG115-Deployer.exe
```

运行部署器前应核对 Release 提供的 SHA-256；未签名的 PyInstaller 单文件程序可能触发部分
安全软件的启发式提示。

### 2. 填写 VPS 页面

1. 填写 VPS IP 或域名、SSH 端口和用户名；
2. 选择密码或 SSH 私钥登录；
3. 非 root 且不是免密 sudo 时填写 sudo 密码；
4. 点击“测试 SSH”；
5. 首次连接时，将弹出的主机密钥指纹与 VPS 服务商控制台核对。

SSH 测试成功后，部署器会同时读取 VPS 资源，并生成当前实例的存储建议。

### 3. 填写 Telegram 页面

填写 Bot Token、API ID、API Hash 和本人 Telegram 数字 ID。只有这个数字 ID 能在私聊中使用
Bot；同一个人从群组或频道发送消息也不会被接受。

### 4. 填写 CloudDrive2 / 115 页面

由部署器在同一 VPS 管理 CloudDrive2 时，保持 WebDAV 地址为：

```text
http://clouddrive2:19798/dav
```

第 3 页各项按下表填写：

| 项目 | 是否必填 | 填写方法 |
| --- | --- | --- |
| WebDAV 地址 | 是 | 同机受管 CloudDrive2 保持默认内网地址；不要填写 VPS 公网地址 |
| WebDAV 用户名 | 是 | 填写准备在 CloudDrive2 中创建的 TG115 专用用户名 |
| WebDAV 密码 | 是 | 填写准备给上述 WebDAV 用户设置的密码 |
| WebDAV 根目录后的子目录 | 否 | WebDAV 用户根目录已经指向目标文件夹时留空；根目录在更上层时才填相对路径 |

这里不是填写 CloudDrive2 会员密码或 115 账号密码。部署前先确定一组 WebDAV 用户名和密码，
基础环境部署完成后，再到 CloudDrive2 管理页创建完全相同的 WebDAV 用户。Bot 与同机
CloudDrive2 通过 Docker 内网通信。

如果取消第 4 页的“在 VPS 上安装并管理 CloudDrive2 容器”，表示使用已经存在的外部
CloudDrive2；此时第 3 页必须填写 Bot 容器实际能够访问的 WebDAV 地址和现有账号。

### 5. 选择存储方案

第 4 页各项按下表确认：

| 项目 | 建议 |
| --- | --- |
| 在 VPS 上安装并管理 CloudDrive2 容器 | CloudDrive2 与 Bot 部署在同一台 VPS 时保持勾选 |
| 安装目录 | 一般保持 `/opt/tg115` |
| 本地任务预算 | 先检测 VPS，再应用均衡或流式优先建议 |
| 磁盘最少保留 | 采用检测结果，不要为了放入更多文件盲目调低 |
| 时区 | 中国大陆一般保持 `Asia/Shanghai` |

源码默认值保持为：

```text
安装目录：/opt/tg115
本地任务预算：20GB
磁盘最少保留：20GB
时区：Asia/Shanghai
```

可以点击：

- “检测 VPS 并推荐”：只读重新探测，不修改表单；
- “应用均衡值”：给普通落盘和失败保留留出更多空间；
- “应用流式优先值”：减少完整落盘额度，更适合小硬盘 VPS。

建议不会静默覆盖默认值。安装目录或 CloudDrive2 管理方式变化后必须重新检测。程序不会自动
分区、格式化、扩容、挂载或迁移 Docker 数据目录。

例如检测结果为约 2 核、1.9GB 内存、33GB 可用磁盘，并推荐“流式优先 8GB/8GB”时，点击
“应用流式优先值”，确认本地任务预算和磁盘最少保留都变成 `8`。这种配置属于保守运行，
不要再改回默认 `20GB/20GB`。

### 6. 开始部署

点击“一键部署基础环境”。正常可能需要 5～15 分钟，期间会安装或检查 Docker、构建 Bot 镜像、
启动服务并执行健康检查。正式写入前会再次检查资源；条件不安全时会停止，不会强行部署。

日志显示正在下载依赖或构建 Docker 镜像时只需等待，不要重复点击“一键部署基础环境”、
“修复 CloudDrive2 网络”或“WebDAV 验收”。必须等到部署器明确提示“部署成功”后再继续。

重新部署会保留 SQLite 数据库、下载目录、日志以及 CloudDrive2 配置和挂载数据。升级会先在
隔离目录构建候选镜像，并为旧代码、配置和数据库创建回退点；新版本未通过健康核验时自动恢复。

### 7. 完整按钮顺序

首次安装可直接按下面顺序操作：

1. 第 1 页填写 VPS 连接信息，点击“测试 SSH”，首次连接时核对主机密钥指纹；
2. 第 2 页填写 Bot Token、Telegram API ID、API Hash 和本人数字 ID；
3. 第 3 页填写计划使用的 WebDAV 用户名、密码，按目标目录规则决定是否填写子目录；
4. 第 4 页确认 CloudDrive2 管理方式，点击“检测 VPS 并推荐”，再主动应用合适的存储建议；
5. 点击“一键部署基础环境”，等待“部署成功”；
6. 点击“打开 CloudDrive2 管理页”，由本人登录 CloudDrive2、挂载 115、开启 WebDAV；
7. 在 CloudDrive2 中创建与第 3 页完全相同的 WebDAV 用户，关闭只读并授予读取、写入、
   改名和删除权限；
8. 点击“WebDAV 验收”，等待出现 `TG115_DESTINATION=OK`；
9. 只有验收因容器网络问题失败时，才点击“修复 CloudDrive2 网络”，修复后重新验收；
10. 在 Telegram 中私聊自己的 Bot，先发送一个小文件验证完整链路。

## CloudDrive2 与 115 配置

基础部署完成后，点击“打开 CloudDrive2 管理页”。部署器会建立 SSH 隧道并打开类似地址：

```text
http://127.0.0.1:19798
```

该地址只通过当前 SSH 隧道访问，CloudDrive2 的 19798 端口默认不会暴露到公网。部署器会先用
该隧道完成一次真实 HTTP 请求，确认管理页有响应后才打开浏览器；已有隧道失效时会自动重建。
管理入口固定使用本机 `127.0.0.1:19798`，不再生成随机端口；如果本机端口已被其他程序占用，
部署器会停止并提示先释放端口。

在 CloudDrive2 中依次完成：

1. 登录 CloudDrive2；
2. 添加并登录自己的 115 网盘；
3. 确认可以浏览 115 文件；
4. 开启 WebDAV；
5. 创建 TG115 专用 WebDAV 用户；
6. 关闭“只读”，确保它拥有读取、写入、改名和删除权限。

目标目录有两种配置方式，二选一：

| CloudDrive2 WebDAV 用户根目录 | 部署器“根目录后的子目录” | 最终位置示例 |
| --- | --- | --- |
| `/115open/Telegram` | 留空 | `/115open/Telegram/文件名` |
| `/` 或更上层目录 | `115open/Telegram` | `/115open/Telegram/文件名` |

不要同时在 WebDAV 根目录和部署器子目录中重复填写 `115open/Telegram`，否则会出现套娃目录。

## 真实 WebDAV 验收

CloudDrive2 和 115 配置完成后，点击“WebDAV 验收”。该操作会从 Bot 容器内使用实际运行配置：

```text
生成 256 字节随机文件
→ 写入 CloudDrive2 WebDAV
→ 检查远端大小
→ 远端改名
→ 再次检查
→ 删除远端与本地测试文件
```

只有出现 `TG115_DESTINATION=OK` 才算通过。该结果证明当前 Bot 配置具备 WebDAV 写入、读取、
改名和删除能力，但仍应在 115 官方客户端验证真实文件大小及可打开性。

建议正式批量使用前依次测试：

1. 一个 5～20MB 小文件；
2. 一个普通落盘文件；
3. 一个超过本地预算或手动 `/stream` 的流式文件；
4. 传输后的 Bot 重启恢复；
5. 一个测试任务的 `/cancel` 远端清理。

## 日常使用

1. 在 Telegram 中选择有权保存的文件；
2. 转发到自己与 TG115 Bot 的一对一私聊；
3. Bot 返回任务编号并持久化排队；
4. 普通文件完整下载到 VPS 后再上传；
5. 超过预算的单文件自动流式写入；
6. WebDAV 临时文件验证成功后改成正式名称；
7. Bot 先记录清理状态，再删除本地副本；
8. Bot 显示“Bot 传输已完成（CloudDrive2 已接收）”；
9. 按自己的使用需要在 CloudDrive2 或 115 客户端查看文件。

### 状态含义

| 状态 | 含义 |
| --- | --- |
| 在排队 | 任务已保存，等待资源和目的端允许放行 |
| 正在从 Telegram 下载 | 普通模式正在写入 VPS 临时文件 |
| 正在从 Telegram 流式写入 CloudDrive2 | 文件直接进入上传管道，进度不是 115 入库进度 |
| 正在写入 CloudDrive2 | VPS 本地完整文件正在上传 WebDAV |
| CloudDrive2 已接收，正在清理 VPS 本地文件 | 远端已经复验，本地清理尚未完成 |
| Bot 传输已完成（CloudDrive2 已接收） | Bot 流程结束，不代表 115 官方端已核验 |

## Bot 命令

| 命令 | 用途 |
| --- | --- |
| `/start`、`/help` | 查看帮助 |
| `/queue [页码]` | 每页查看 5 个最近任务；省略页码时打开第 1 页 |
| `/status`、`/performance` | 查看同一份单页状态、性能和任务统计 |
| `/doctor` | 只读诊断，不创建远端测试文件 |
| `/task <编号>` | 查看单个任务详情 |
| `/watch <编号>` | 每 5 秒编辑同一条消息展示进度，重启后需重新订阅 |
| `/pause`、`/resume` | 持久暂停或恢复新任务调度；不打断活动传输 |
| `/retry <编号>` | 重试一个失败任务 |
| `/retry all` | 一次重排最多 100 个可重试任务 |
| `/cancel <编号>` | 先清理并复查本任务远端文件，再删除本地副本 |
| `/stream <编号>` | 将排队或下载失败任务改成流式模式 |
| `/orphans` | 只读巡检疑似遗留的 `.uploading-*` 临时文件 |
| `/orphans clean` | 获取一次性确认码后显式清理遗留临时文件 |

### 单页快捷菜单

发送 `/start` 或 `/help` 会显示一张四字标签对齐的帮助页，并附带快捷按钮：

```text
使用帮助

提交文件：直接发送或转发视频、文档等文件
状态查看：/status、/queue [页码]
任务查看：/task <编号>
进度跟踪：/watch <编号>
失败重试：/retry <编号|all>
取消任务：/cancel <编号>
流式传输：/stream <编号>
调度控制：/pause、/resume
运行诊断：/doctor
临时巡检：/orphans

[系统状态] [最近任务]
[暂停调度] [恢复调度]
[运行诊断] [临时巡检]
```

按钮会编辑当前菜单消息，减少会话刷屏；文字命令全部保留。按钮回调再次校验配置用户 ID 和
私聊会话，转发到群组或由其他账号点击都不能执行操作。

`/queue` 默认每页显示 5 项，长文件名按显示宽度截断，下方可以直接上一页、下一页或查看某个
任务。任务详情提供“刷新进度”“重新排队”“切换流式”“取消任务”等适用于当前状态的按钮。
按钮取消不会立刻删除内容：Bot 会先展示影响说明，只有在 5 分钟内再次点击“确认取消”才执行；
远端无法确认安全删除时仍会失败关闭并保留本地数据。明确输入 `/cancel <编号>` 的旧流程保持
兼容。快捷“临时巡检”始终只读；真正清理仍需使用 `/orphans clean` 取得一次性确认码。

`/status` 和 `/performance` 返回同一份完整单页信息。字段统一使用四字标签，避免 Telegram
客户端在不同位置自动折行；任务统计只显示数量不为零的分类，并把历史兼容终态统一计入
“Bot 完成”。示例：

```text
系统状态

目的状态：目标目录可访问（只读检查，4 秒前）
队列调度：运行中
当前传输：下载/流式 1，上传 0
并发窗口：下载 4，上传 1
本地额度：已用 1.82GB / 10.00GB
磁盘可用：29.22GB
资源使用：CPU 6.5%，内存 1.39GB
网络总速：2.22MB/s
来源下载：Telegram 2.12MB/s
远端上传：WebDAV 0B/s
流式送入：0B/s
任务统计：下载中 1，Bot 完成 4
```

`/cancel` 采用失败关闭策略：只有确认本任务的临时和正式远端路径均已删除，才会删除 VPS 本地
副本并标记取消。清理失败时会保留数据，便于再次取消或人工处理。

## VPS 运维与升级

默认安装目录为 `/opt/tg115`。通过 SSH 登录 VPS 后可使用：

| 命令 | 用途 |
| --- | --- |
| `sudo /opt/tg115/manage.sh status` | 查看容器状态 |
| `sudo /opt/tg115/manage.sh logs` | 查看近期 Bot 日志 |
| `sudo /opt/tg115/manage.sh check` | 核对配置、代码指纹和基础心跳 |
| `sudo /opt/tg115/manage.sh verify` | 执行真实 WebDAV 写入、改名和删除验收 |
| `sudo /opt/tg115/manage.sh restart` | 仅重启 Bot，不应用配置变化 |
| `sudo /opt/tg115/manage.sh stop` | 停止 Bot |
| `sudo /opt/tg115/manage.sh start` | 启动并等待 Bot 健康 |
| `sudo /opt/tg115/manage.sh backups` | 只读统计升级备份数量和占用 |
| `sudo /opt/tg115/manage.sh prune-backups 5` | 明确删除旧回退点，每类保留最新 5 份 |

### 修改配置

把新配置文件放在安装目录之外，再执行：

```bash
sudo /opt/tg115/manage.sh apply-config /absolute/new-config.env
sudo /opt/tg115/manage.sh check
sudo /opt/tg115/manage.sh verify
```

`apply-config` 会先备份和预检，再重建 Bot 并核对实际环境；失败时恢复旧配置。更换目的账号或
目标目录前，应先处理完已有队列和失败保留任务。

### 升级程序

推荐使用新版 Windows 部署器重新执行“一键部署基础环境”。`manage.sh update` 只会使用 VPS
已经安装的 payload 重建 Bot，不会自动从 GitHub 拉取新代码。

升级备份位于 `/opt/tg115-backups`。部署只统计占用，超过 5GB 时提醒，不自动删除。第一次升级
并完成真实文件验证之前，不要急于清理旧回退点。`prune-backups` 只处理程序生成的
`config-*`、`database-*` 和 `env-*` 普通文件，不删除下载、日志或 CloudDrive2 数据。

## 常见问题

### SSH 连接失败

检查 VPS 地址、端口、用户名、密码或私钥，以及服务商安全组。首次连接应核对主机密钥指纹，
不要在指纹变化原因不明时直接接受。

如果提示 `Host key for server ... does not match`，表示部署器保存的旧 SSH 主机密钥与服务器
本次返回的密钥不同。这不是用户名或密码错误，常见于 VPS 重装、恢复快照、SSH 主机密钥
重新生成或 IP 被重新分配；原因不明时也可能存在连接到错误服务器或中间人攻击的风险。

确认 VPS 确实发生过上述变化后，按以下步骤更新记录：

1. 优先从服务商网页控制台／VNC 登录 VPS，查看当前 SSH 主机指纹：

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
   sudo ssh-keygen -lf /etc/ssh/ssh_host_rsa_key.pub -E sha256
   ```

2. 关闭部署器，在 Windows PowerShell 中打开部署器自己的主机记录：

   ```powershell
   notepad "$env:APPDATA\TG115-Deployer\known_hosts"
   ```

3. 只删除当前 VPS 对应的一行并保存。默认 22 端口通常以主机名或 IP 开头；非默认端口通常以
   `[主机名或IP]:端口` 开头。不要清空整个文件，也不要删除其他 VPS 的记录。
4. 重新打开部署器并点击“测试 SSH”。首次密钥确认框再次出现后，将其中的密钥类型和
   `SHA256:...` 指纹与第 1 步核对，完全一致才接受。

如果 VPS 没有重装、换机、恢复快照或调整 SSH 配置，不要直接删除旧记录，应先在服务商控制台
确认当前 IP 和服务器身份。不要把错误窗口中的真实 IP、主机公钥或登录资料提交到 Issue。

### 提示没有 `/dev/fuse`

受管 CloudDrive2 Docker 挂载需要 FUSE。请在 VPS 控制台开启，或联系服务商确认虚拟化类型
是否允许 `/dev/fuse`。

### Bot 一直排队

依次检查：

1. `/status` 的资源采样是否新鲜；
2. 磁盘是否接近安全线；
3. CloudDrive2 是否已登录并挂载 115；
4. WebDAV 是否开启且用户不是只读；
5. 用户名、密码和目标目录是否与部署器一致；
6. 执行 `sudo /opt/tg115/manage.sh verify`。

### 日志出现 `401 Unauthorized`

通常表示运行中的 Bot WebDAV 凭据与 CloudDrive2 用户不一致。修改部署器中的值后，必须重新
执行基础部署，或者使用 `apply-config`；单纯 `restart` 不会应用新配置。之后运行 `check` 和
`verify`，不要只根据历史日志判断当前状态。

### 管理页显示 `ERR_EMPTY_RESPONSE`

新版部署器会在打开浏览器前验收 SSH 隧道，不再把没有响应的本地地址显示为成功；重复点击时
也会先检查旧隧道并在失效后自动重建。如果提示 VPS 禁止 TCP 端口转发，检查 SSH 服务的
`AllowTcpForwarding`，并确认 `PermitOpen` 允许 `127.0.0.1:19798`。修改 sshd 配置前应保留
当前 SSH 会话，先执行 `sshd -t` 验证配置，再安全地 reload，避免把自己锁在 VPS 外。

管理页隧道错误和 `401 Unauthorized` 是两件事：前者发生在 SSH 转发层，后者表示 CloudDrive2
中的 WebDAV 用户名、密码或权限与 Bot 当前配置不一致。“修复 CloudDrive2 网络”不能修复
401；应先进入管理页完成 WebDAV 配置，再执行“WebDAV 验收”。

### 日志出现 `lookup clouddrive2`

先在部署器中点击“修复 CloudDrive2 网络”。该操作会保留 CloudDrive2 登录和挂载数据，修复
容器网络别名后执行真实 WebDAV 验收。若 19798 端口由未知程序占用，脚本会拒绝自动修改。

### Bot 显示 healthy，为什么仍不能上传

健康状态只检查当前容器的调度和资源心跳，不代表 WebDAV 可写，更不代表 115 已完成。
`manage.sh verify` 才会用实际配置测试 WebDAV 写入、大小检查、改名和删除。

### 单文件超过本地预算

系统会自动使用流式模式，不需要把预算调整到文件大小。流式任务仍受共享上传窗口和真实磁盘
安全线限制，中断后通常从头重传。对于因总预算不足而长期等待、但本身没有超过单文件预算的
任务，可以使用 `/stream <编号>` 手动切换。

### 磁盘空间看起来足够，部署器仍拒绝

Docker 数据目录可能和 `/opt/tg115` 位于不同文件系统。部署器会分别检查安装盘、Docker 盘、
已有下载、备份、保留线和 inode；任一关键文件系统不安全都会停止部署。

### 文件很多，是否必须逐个确认

不需要。当前 Bot 不再要求或提示逐个确认；完成传输并通过 WebDAV 远端大小复验后，状态统一
显示为“Bot 完成”。`/status` 会把历史兼容状态一并合并统计，不增加额外操作。

## 安全说明

- 不要把 VPS 密码、SSH 私钥、Bot Token、API Hash、WebDAV 密码、115 登录信息、日志、数据库
  或 Telegram Session 提交到仓库、Issue、截图和聊天记录。
- 部署器不保存表单密码，只保存用户确认过的 SSH 主机密钥。
- Base64 和 rclone obscure 只是编码或混淆，不是加密；VPS root 能读取服务配置。
- CloudDrive2 为使用 FUSE 需要较高容器权限，建议部署在不承载钱包、数据库等重要业务的独立
  VPS 上。
- CloudDrive2 管理端口默认只绑定 `127.0.0.1`，推荐通过部署器建立的 SSH 隧道访问。
- Bot 容器使用 UID/GID 10001、只读根文件系统、`no-new-privileges` 并丢弃 capabilities。
- 发布产物应附 SHA-256；不要运行来源不明或无法核对版本的 EXE、脚本和镜像。

更多说明见 [安全政策](SECURITY.md)和[第三方组件说明](docs/第三方组件说明.md)。

## 本地开发与测试

### 项目结构

```text
installer.py                 Windows Tkinter 部署器
vps_resources.py             VPS 资源探测和实例建议
payload/app/main.py          Bot 调度及普通/流式传输状态机
payload/app/db.py            SQLite 队列、额度和恢复
payload/app/rclone_client.py WebDAV/rclone 适配层
payload/manage.sh            VPS 日常运维入口
payload/remote_install.sh    VPS 安装与升级回退
tests/                       单元、场景、部署和 WebDAV 集成测试
```

### 完整 Python 回归

```powershell
uv run --with-requirements requirements-build.txt `
  --with-requirements payload/requirements.txt `
  python -m unittest discover -s tests -v
```

### 静态和依赖检查

```powershell
uv run --with ruff==0.16.0 ruff check installer.py vps_resources.py payload/app tests
uv run --with bandit==1.9.4 bandit -q -r payload/app installer.py vps_resources.py
uv run --with pip-audit==2.10.1 pip-audit -r payload/requirements.txt
uv run --with pip-audit==2.10.1 pip-audit -r requirements-build.txt
```

CI 还会在 Linux 上运行 ShellCheck、Compose 配置校验、Python 3.12 回归和 Bot 镜像构建，并在
Windows 上构建部署器和执行打包后自检。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

如果仓库由 GitHub Fork 创建，首次使用时需进入仓库的 **Actions** 页面，按提示启用工作流。
本仓库同时支持 `main` 推送、Pull Request 和页面中的 “Run workflow” 手动触发。GitHub 默认
不会在新 Fork 中自动运行工作流，详见
[GitHub 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflows-in-forked-repositories)。

## 更多文档

- [小白使用说明](docs/README-小白使用说明.md)
- [部署前填写信息清单](docs/填写信息清单.md)
- [v1.6.0 优化与验收记录](docs/v1.6.0-优化与验收.md)
- [验收与复核报告](docs/验收与复核报告.md)
- [第三方组件说明](docs/第三方组件说明.md)
- [更新记录](CHANGELOG.md)
- [发布说明](RELEASE_NOTES.md)

## 致谢与项目来源

感谢原作者 **whyhhh20** 创建并公开 TG115：

- 原始项目仓库：[whyhhh20/TG115](https://github.com/whyhhh20/TG115)

本仓库的 v1.6.0 二次开发以原项目 v1.5.0 代码为基础，保留 MIT 许可证，并在原有
Telegram → rclone → CloudDrive2 → 115 链路上继续完善可靠性、部署安全和运维体验。

同时感谢 Telethon、rclone、Docker、Paramiko 等开源项目，以及 CloudDrive2 提供的 WebDAV
和网盘挂载能力。第三方组件分别受各自许可证与服务条款约束。

## 许可证与免责声明

本项目采用 [MIT License](LICENSE)。

本项目与 Telegram、115、CloudDrive2 及其运营方没有隶属、授权或官方合作关系。使用者应自行
评估账号、数据、网络、VPS 权限和第三方闭源组件风险。
