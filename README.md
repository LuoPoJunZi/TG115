# TG115

TG115 是一个完全自建的个人文件传输工具：把文件转发给自己的 Telegram Bot，由 VPS
自动排队、下载并通过 CloudDrive2 WebDAV 写入自己挂载的 115 网盘。

## 主要特性

- Windows 图形化一键部署器；
- Telegram 私聊 Bot 接收文件；
- SQLite 持久化队列，重启后自动恢复；
- 按 CPU、内存、磁盘、网络和目的端状态动态调节并发；
- 20GB 本地任务预算与独立磁盘安全线；
- 单文件超过本地预算时自动使用 Telegram → rclone → CloudDrive2 流式模式；
- CloudDrive2 WebDAV 写入、大小校验、远端改名和本地清理；
- 文件直接保存到配置目录，不自动创建年份或月份文件夹；
- 仅允许配置的 Telegram 数字 ID 使用；
- 明确区分“Bot 传输完成”和“115 官方端已由用户确认”。

## 使用范围

当前正式支持本人和 Bot 的一对一私聊。部署器不会替用户登录 Telegram、CloudDrive2
或 115，也不会读取 115 账号密码。

完整部署步骤见 [小白使用说明](docs/README-小白使用说明.md)。

## 部署前准备（推荐）

| 项目 | 推荐准备 | 说明 |
| --- | --- | --- |
| VPS | 2 核 CPU、4GB 内存、50GB SSD | 适合个人使用的基础配置；Ubuntu 22.04/24.04 或 Debian 12 64 位，并支持 `/dev/fuse` |
| VPS 网络 | 稳定访问 Telegram 和 Docker 镜像仓库，端口带宽建议 100Mbps 或以上 | 实际速度仍受 Telegram、VPS 线路、CloudDrive2 和 115 状态共同影响 |
| CloudDrive2 | CloudDrive2 会员，并确认 115 挂载和 WebDAV 可用 | 会员登录由本人在 CloudDrive2 管理页完成，部署器只使用单独设置的 WebDAV 用户名和密码 |
| 115 | 可正常登录且剩余空间足够的 115 账号 | 115 会员不是程序代码的硬性要求，实际容量和服务权益以账号状态为准 |
| Telegram | Bot Token、API ID、API Hash、本人数字 ID | Bot Token 从 `@BotFather` 获取，API ID/API Hash 从 `my.telegram.org` 获取 |
| 本地电脑 | Windows 10/11 64 位 | 用于运行图形化部署器和建立 SSH 安全隧道 |

大量或长期批量传输时，建议升级到 4 核 CPU、8GB 内存、80～100GB SSD。完整填写项及默认值见
[部署前填写信息清单](docs/填写信息清单.md)。

## 快速开始

1. 在 GitHub Releases 下载 `TG115-Deployer-v1.5.0.exe`；
2. 对照 `SHA256SUMS.txt` 校验文件；
3. 双击部署器并填写 VPS、Telegram 和 WebDAV 信息；
4. 完成 CloudDrive2 登录、115 挂载和 WebDAV 验收；
5. 在 Telegram 私聊自己的 Bot 并转发文件。

不要把 VPS 密码、SSH 私钥、Bot Token、Telegram API Hash、WebDAV 密码或 115 登录信息
提交到仓库、Issue、截图或聊天记录。

## 状态含义

- `在排队`：任务已经保存，等待系统动态放行；
- `正在从 Telegram 下载`：VPS 正在接收 Telegram 文件；
- `正在从 Telegram 流式写入 CloudDrive2`：大文件不完整落盘，边读取边写入；
- `正在写入 CloudDrive2`：VPS 正在向 WebDAV 写入；
- `Bot 传输已完成，115 官方端待确认`：Bot 已完成远端校验和本地清理；
- `115 官方端已由你确认`：用户在 115 官方客户端核验后执行了 `/confirm`。

`/confirm` 只记录人工确认，不会再次上传、移动或删除文件。

普通文件先完整落盘，上传失败时可以保留本地副本；单文件超过本地预算时使用流式模式，
不会占用完整文件大小的本地额度，但中断后需要从头重新传输。CloudDrive2 自身仍可能使用
缓存，因此磁盘安全线继续生效。

## 安全边界

CloudDrive2 是第三方闭源软件，FUSE 挂载需要较高容器权限。建议使用只运行本项目的独立
VPS，不要与钱包、数据库或其他重要服务共用。详见
[安全政策](SECURITY.md)和[第三方组件说明](docs/第三方组件说明.md)。

## 项目状态

当前正式版为 `v1.5.0`。发布前验证结果见
[验收与复核报告](docs/验收与复核报告.md)。

本项目与 Telegram、115、CloudDrive2 及其运营方没有隶属、授权或官方合作关系。

## 许可证

[MIT](LICENSE)
