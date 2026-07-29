# Telegram → 115 一键部署器：小白使用说明

> 正式版：1.5.0；适用电脑：Windows 10 / Windows 11 64 位；适用 VPS：Ubuntu 或
> Debian 64 位；推荐 VPS：2 核 CPU、4GB 内存、50GB 硬盘。

---

## 一、它能自动做什么

填写资料后点击“一键部署基础环境”，程序会自动：

1. 通过密码或 SSH 私钥连接 VPS；
2. 显示并确认 VPS 主机密钥指纹；
3. 检查 Linux 系统、CPU 架构、内存和磁盘；
4. 上传完整部署包；
5. 安装 Docker、Docker Compose、FUSE 等运行环境；
6. 安装并启动 CloudDrive2 容器；
7. 构建并启动 Telegram Bot；
8. 建立 SQLite 持久化任务队列；
9. 配置 20GB 本地任务预算和磁盘安全线；
10. 单文件超过预算时自动切换为不完整落盘的流式传输；
11. 启用 CPU、内存、磁盘、网络和错误率动态调度；
12. 配置异常自动重启、VPS 开机自动启动和日志轮转；
13. 执行 Bot 容器基础健康检查并显示结果。

以下两件事必须由你本人完成：

- 登录 CloudDrive2；
- 在 CloudDrive2 中添加并挂载自己的 115 网盘、开启 WebDAV。

部署器不会要求 Cursor 账号密码，也不会替你登录 115。

---

## 二、使用前必须准备

### VPS

- Ubuntu 22.04 / 24.04，或 Debian 12 64 位；
- 个人基础使用推荐 2 核 CPU、4GB 内存、50GB SSD；
- 大量或长期批量传输推荐 4 核 CPU、8GB 内存、80～100GB SSD；
- 公网 IP 或域名；
- SSH 端口、用户名；
- VPS 登录密码，或者 SSH 私钥；
- 如果登录用户不是 root：准备 sudo 密码，或者确保该用户可以免密 sudo；
- VPS 必须支持 `/dev/fuse`。部分容器型 VPS 需要服务商在控制台开启 FUSE；
- VPS 应能稳定访问 Telegram 和 Docker 镜像仓库，端口带宽建议 100Mbps 或以上，并准备
  足够的月流量。实际速度仍受 Telegram、VPS 线路、CloudDrive2 和 115 状态共同影响。

50GB 硬盘配合默认的 20GB 本地任务预算和 20GB 磁盘安全线可以使用，但 CloudDrive2
可能另外占用缓存。单文件超过 20GB 时 Bot 自动使用流式模式，不会先完整写入 VPS；
经常批量处理大文件时仍应优先选择更大的硬盘，因为 CloudDrive2 缓存不受 Bot 额度直接控制。

### Telegram

- Bot Token：从 `@BotFather` 获取；
- API ID：从 `https://my.telegram.org` 获取；
- API Hash：从 `https://my.telegram.org` 获取；
- 你自己的 Telegram 数字 ID。

### CloudDrive2 / 115

- CloudDrive2 会员账号；当前方案按会员环境设计，并要求 115 挂载和 WebDAV 功能可用；
- CloudDrive2 WebDAV 用户名；
- CloudDrive2 WebDAV 密码；
- 计划挂载的 115 网盘；
- 足够的 115 剩余空间；115 会员不是程序代码的硬性要求；
- WebDAV 根目录后的相对子目录；WebDAV 根目录已经选中目标文件夹时留空。

CloudDrive2 会员和 115 账号由你本人在 CloudDrive2 管理页登录。部署器不需要这些账号的
登录密码，只需要你另外设置的 WebDAV 用户名和密码。

---

## 三、第一次部署

### 第 1 步：运行

推荐直接双击：

```text
TG115-Deployer.exe
```

也可以双击：

```text
一键部署-Telegram到115.cmd
```

Windows 可能显示“未知发布者”，原因是本程序没有购买商业代码签名证书。你可以先在
`SHA256SUMS.txt` 中核对文件校验值。

### 第 2 步：填写 VPS

- `VPS IP 或域名`：服务商提供的公网地址；
- `SSH 端口`：通常是 22；
- `SSH 用户名`：通常是 root、ubuntu 或 debian；
- `登录方式`：选择“密码”或“SSH 密钥”；
- 密码登录：填写 VPS 登录密码；
- 密钥登录：选择私钥文件，有口令时再填写私钥口令；
- 非 root 用户：填写 sudo 密码；如果已经配置免密 sudo，可以留空。

先点击“测试 SSH”。

首次连接会出现 VPS 主机密钥指纹。应当与 VPS 服务商控制台显示的指纹核对，一致后才能点击“是”。

### 第 3 步：填写 Telegram

- Bot Token；
- Telegram API ID；
- Telegram API Hash；
- 你的 Telegram 数字 ID。

API Hash、Bot Token 都是秘密信息，不要发到群聊或公开网页。

### 第 4 步：填写 CloudDrive2

如果由部署器安装 CloudDrive2，保持默认 WebDAV 地址：

```text
http://clouddrive2:19798/dav
```

填写：

- WebDAV 用户名；
- WebDAV 密码；
- WebDAV 根目录后的子目录。

如果 WebDAV 根目录已经选中 `115open/Telegram`，子目录必须留空，文件会直接保存到
这个 `Telegram` 文件夹。不要再填写 `115/Telegram`，否则会产生
`Telegram/115/Telegram` 套娃。

如果勾选“在 VPS 中安装并管理 CloudDrive2”，Bot 会固定使用容器内网地址
`http://clouddrive2:19798/dav`。不要填写 VPS 公网 IP，也不要把 19798 端口写成
`https://`；该端口本身是 HTTP，管理页面通过 SSH 隧道安全访问。

### 第 5 步：保持推荐选项

```text
安装目录：/opt/tg115
本地任务预算：20GB
磁盘最少保留：20GB
时区：Asia/Shanghai
```

### 第 6 步：一键部署

点击：

```text
一键部署基础环境
```

正常需要约 5～15 分钟，主要时间用于：

- VPS 安装 Docker；
- 下载 CloudDrive2 镜像；
- 构建 Bot 镜像；
- 安装 Python 依赖；
- 等待健康检查。

不要在部署过程中关闭部署器。

---

## 四、登录 CloudDrive2 并挂载 115

部署成功后点击：

```text
打开 CloudDrive2 管理页
```

部署器会建立 SSH 安全隧道，然后在浏览器打开类似地址：

```text
http://127.0.0.1:随机端口
```

这不是公网地址，只有你的电脑通过当前 SSH 隧道才能访问。

在 CloudDrive2 中完成：

1. 登录 CloudDrive2 会员账号；
2. 添加 115；
3. 按 CloudDrive2 提示登录或扫码；
4. 确认能浏览 115 文件；
5. 在设置中开启 WebDAV；
6. 如果 WebDAV 根目录已经是目标 Telegram 文件夹，部署器中的子目录保持空白。

完成后点击：

```text
WebDAV 验收（写入测试文件）
```

该验收不是只看容器是否运行，而是会自动执行：

```text
在 VPS 生成 256 字节随机测试文件
→ rclone 上传到 CloudDrive2 WebDAV
→ CloudDrive2 WebDAV 临时文件大小校验
→ 远端改名
→ 再次校验
→ 删除远端和 VPS 测试文件
```

只有输出 `TG115_DESTINATION=OK`，部署器才会显示“验收通过”。这证明文件已经写入
CloudDrive2 WebDAV，但不能单独证明 115 官方端已经保存完成。最终应在 115 官方客户端
确认文件大小正常，并能打开或播放。

Bot 每 30 秒自动重新检查 CloudDrive2，不需要重新部署。

---

## 五、以后怎么使用

1. 在 Telegram 找到视频或文件；
2. 在与自己的私人 Bot 的一对一聊天中转发；
3. Bot 自动审核并返回任务编号；
4. 暂未开始的任务显示“在排队”；
5. VPS 根据 CPU、内存、磁盘、网络和服务状态动态放行；
6. 不超过本地预算的文件先下载到 VPS，再由 rclone 上传；
7. 超过本地预算的单个文件直接执行 Telegram → rclone → CloudDrive2 流式传输；
8. CloudDrive2 WebDAV 大小验证通过；
9. VPS 删除本地临时文件并释放额度；
10. Bot 显示“Bot 传输已完成，115 官方端待确认”，并自动处理后续任务；
11. CloudDrive2 继续处理网盘写入；这一步不一定出现在“上传任务”列表中；
12. 在 115 官方客户端看到文件大小正常且可以打开或播放；
13. 向 Bot 发送 `/confirm <任务编号>`，把该任务记为“115 官方端已由你确认”。

`/confirm` 只记录你的人工确认，不会重新上传，也不会修改或删除 115 中的文件。Bot
当前没有接入可信的 115 官方完成接口，因此不能根据 CloudDrive2 文件列表或历史任务状态
自动断言“115 官方端已完成”。

你不需要手动分批，也不需要重新转发已经获得任务编号的文件。当前正式支持范围是
“你本人和 Bot 的一对一私聊”；不要把 Bot 放进私人频道或群组代替私聊，否则频道身份
可能与配置的个人数字 ID 不同而被安全规则拒绝。

文件直接保存在“WebDAV 根目录 + 可选子目录”中，不再按年份和月份建立子目录。
例如 WebDAV 根目录已经是 `115open/Telegram` 且子目录留空时，最终路径为：

```text
115open/Telegram/视频文件名.mp4
```

---

## 六、Bot 命令

```text
/start
/help
/queue
/status
/performance
/task <任务编号>
/confirm <任务编号>
/retry <任务编号>
/cancel <任务编号>
```

---

## 七、重新运行部署器会怎样

重新运行“一键部署基础环境”用于：

- 修改 Telegram 或 WebDAV 配置；
- 更新 Bot 程序；
- 修复损坏的部署；
- 重新执行基础健康检查。

程序会保留：

- SQLite 任务数据库；
- 下载目录；
- 日志；
- CloudDrive2 配置；
- CloudDrive2 挂载数据。

重新部署后，Bot 会把已有 `rclone.conf` 中的 `cd2` 配置更新为本次填写的
WebDAV 地址、用户名和密码，不会继续使用旧凭据。

注意：只在输入框中改值后直接点击“WebDAV 验收”不会更新 VPS；必须先重新执行
“一键部署基础环境”，看到部署成功后再验收。

如果日志出现 `lookup clouddrive2`，点击“修复 CloudDrive2 网络”。部署器会保留现有
CloudDrive2 登录、115 挂载和文件，只刷新 Docker 网络别名，并自动执行真实 WebDAV 验收。

更新前，旧程序配置会备份到：

```text
/opt/tg115-backups/
```

---

## 八、安全设计

- VPS 密码、私钥口令和 sudo 密码不会写入本地配置文件；
- 本地临时配置在部署结束后自动删除；
- 上传到 VPS 的临时目录为随机名称，权限为 700，结束后自动删除；
- VPS 正式 `.env` 权限设置为 600；
- rclone 将 WebDAV 密码转换为 obscure 格式；
- CloudDrive2 和 Python 基础镜像锁定到经过复核的不可变 SHA-256 摘要；
- 普通重新部署不会自动拉取未经复核的新基础镜像；
- CloudDrive2 的 19798 端口只绑定 VPS 的 `127.0.0.1`；
- 管理页通过 SSH 隧道访问，不直接暴露公网；
- 只有配置的 Telegram 数字 ID 能够使用 Bot；
- Bot 容器使用专用非 root 用户运行，并启用只读根文件系统、移除 Linux capabilities；
- VPS 的配置、下载、日志和备份目录仅允许对应服务用户或 root 访问；
- 安装目录经过完整校验和 shell 参数转义，不能把异常路径当成远程命令执行；
- Docker 构建上下文使用白名单，只包含 Dockerfile、依赖清单和 Bot 源码，不包含
  `.env`、下载文件、日志或 CloudDrive2 挂载内容；
- 普通落盘模式在远端验证成功前不会删除本地完整文件；
- 普通落盘模式上传失败会保留本地完整文件；
- 流式模式使用准确文件大小和背压控制，不在 VPS 保留完整副本；中断后会从头重试；
- `/cancel` 必须先确认本任务远端文件已经清理，才会删除 VPS 本地副本并标记取消。

注意：CloudDrive2 为实现 FUSE 挂载仍需特权容器和宿主机 PID 命名空间；因此建议这台 VPS
只运行本项目。服务器 root 用户始终能够读取容器配置，任何 VPS 自动化都无法防范已经取得
root 权限的攻击者。

20GB 是 Bot 普通落盘模式的下载和待上传文件预算。大于该预算的单文件使用流式模式，不按
完整文件大小占用这 20GB。CloudDrive2 是专有程序，可能为 115 后台上传建立额外缓存；
程序会监控整个 VPS 分区的真实剩余空间并在安全线触发时暂停新传输，但不能保证
CloudDrive2 的内部缓存也严格限制为 20GB。若其任务长期堆积，应暂停继续转发；需要硬隔离
时应给 CloudDrive2 使用单独数据盘或文件系统配额。

---

## 九、常见问题

### 提示 VPS 没有 `/dev/fuse`

CloudDrive2 官方 Docker 挂载方式需要 FUSE。请在 VPS 控制台开启 FUSE，或联系服务商。

### Bot 一直显示在排队

依次检查：

1. CloudDrive2 是否已经登录；
2. 115 是否已经添加；
3. WebDAV 是否开启；
4. WebDAV 用户名、密码是否正确；
5. 目标路径是否正确；
6. `/status` 中 CloudDrive2/115 是否正常；
7. VPS 是否还有足够磁盘空间。

### 单文件超过 20GB 会怎样

它不会永久排队，也不要求把本地预算调大。系统会自动标记为流式模式，一边从 Telegram
读取，一边通过 rclone 写入 CloudDrive2。流式模式不会在 VPS 保留完整副本，所以线路中断
后需要从头重试；CloudDrive2 自身仍可能使用缓存，磁盘安全线仍然有效。

### 115 官方客户端已经能看到文件，Bot 为什么还显示“待确认”

这是正常且刻意保守的状态。`Bot 传输已完成，115 官方端待确认` 表示 VPS 已经把文件写入
CloudDrive2 WebDAV，并完成远端大小校验和本地清理；它不是“仍在占用 VPS 上传带宽”。

确认 115 官方客户端中的文件大小正常并可以打开或播放后，发送：

```text
/confirm <任务编号>
```

状态会变成“115 官方端已由你确认”。如果 `/status` 显示
“Bot 当前实际传输：下载/流式 0，落盘后上传 0”，说明 Bot 此刻没有实际传输任务；
历史任务统计不代表当前网速。

### SSH 连接失败

检查 IP、SSH 端口、用户名、密码或私钥；同时检查 VPS 服务商的安全组是否放行 SSH 端口。

### 部署器被安全软件提示

单文件 EXE 由 PyInstaller 打包，部分安全软件会对未签名的自解压程序作启发式提示。请核对
`SHA256SUMS.txt`，也可以直接查看随包附带的完整源代码。

### 想查看 VPS 日志

通过 SSH 登录 VPS 后执行：

```bash
sudo /opt/tg115/manage.sh logs
```

只查看状态：

```bash
sudo /opt/tg115/manage.sh status
```

手动执行与部署器相同的 CloudDrive2 WebDAV 写入验收：

```bash
sudo /opt/tg115/manage.sh verify
```

重启 Bot：

```bash
sudo /opt/tg115/manage.sh restart
```

---

## 十、当前验证边界

交付前已经完成本地代码审查、56 项自动化测试、队列与磁盘模拟、GUI 自检、
打包自检、ShellCheck、Python 依赖漏洞扫描和容器镜像高危漏洞扫描。

由于没有你的真实 VPS、Telegram 和 CloudDrive2 凭据，交付前无法替你完成真实 VPS 的端到端上传测试。
第一次使用时，部署器会在你的 VPS 上执行真实安装和健康检查；完成 CloudDrive2 与 115 登录后，
建议先转发一个 5～20MB 的测试文件，确认 115 中出现并收到 Bot 完成通知，再开始批量使用。
