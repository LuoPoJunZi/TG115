# 参与贡献

1. 不得提交任何真实账号、Token、密码、Cookie、私钥、个人 ID、文件名或服务器地址；
2. 修改代码后运行全部自动化测试；
3. 涉及安装脚本、远程命令或路径处理时，必须同时补充异常和恶意输入测试；
4. 不要把 CloudDrive2 WebDAV 接收误写成 115 官方端已经完成；
5. 不要提交构建目录、缓存、日志、数据库、下载文件或本地配置。

测试命令：

```powershell
uv run --with-requirements requirements-build.txt `
  --with-requirements payload/requirements.txt `
  python -m unittest discover -s tests -v
```
