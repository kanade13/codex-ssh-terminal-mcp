# SSH Terminal for Codex

在 Windows 版 Codex App 的原生插件面板中操作 SSH 终端。支持本地密码自动填写、私密输入动态验证码、跳板机，以及供 Codex 执行命令的独立通道。

An SSH terminal plugin for Codex App on Windows with a WSL backend, password autofill, private 2FA input, and isolated agent command channels. This is a community project, based on [NullPointerChef/codex-ssh-terminal-mcp](https://github.com/NullPointerChef/codex-ssh-terminal-mcp), and is not an official OpenAI plugin.

## 功能

- xterm.js 终端，支持方向键、Ctrl+C、交互式程序、调整尺寸、右键粘贴与 Ctrl+V；底部输入框可作为备用输入渠道。
- 从 Windows 的 SSH 配置导入服务器；支持密码、密钥和动态验证码登录。
- 可配置四机联动：一台自动使用密码登录，另外三台共享一次动态验证码。三台必须实际使用相同验证码，第一台作为另两台的跳板机。
- SSH 主连接保留 12 小时，后台检查并尝试恢复掉线连接。服务器重新要求 2FA 时，仍需用户提供新验证码；网络、休眠及服务器策略会影响连接持续时间。
- Codex 使用独立命令通道，终端会话与 SSH 主连接由用户控制，多个聊天可共享连接。
- 插件启动时自动启动所需本地服务，无需另开网页或手动运行常驻命令。

## 新电脑安装

需要 Windows 10/11、Codex App、Git、Windows OpenSSH Client、Windows Node.js 20+，以及已完成首次初始化的 WSL Ubuntu 24.04 或更新版本。WSL 中需要 Python 3.11+；Ubuntu 24.04 自带的 Python 可用。

如果尚未安装 WSL 或 Node，可先在 PowerShell 中执行：

```powershell
wsl --install -d Ubuntu-24.04
winget install OpenJS.NodeJS.LTS
```

按系统提示重启并初始化 Ubuntu 用户，再打开新的 PowerShell 窗口：

```powershell
git clone https://github.com/kanade13/codex-ssh-terminal-mcp.git
cd codex-ssh-terminal-mcp
powershell -NoProfile -ExecutionPolicy Bypass -File .\Install.ps1
```

也可从 GitHub 下载 ZIP，解压后在目录中运行最后一条命令。安装脚本会创建独立的 Python 虚拟环境、安装依赖、导入 SSH 别名，并向 Codex 注册本地插件市场及插件。它不会修改 Windows 的原始 SSH 配置。

安装脚本自动读取 Windows 当前启用的 HTTP 代理。克隆仓库时，Git 的代理可通过单次参数指定，例如 `git -c http.proxy=http://127.0.0.1:10808 clone ...`。安装时也可指定 `-ProxyUrl http://127.0.0.1:10808`；指定 `-ProxyUrl ''` 可禁用代理。WSL 需要能访问该代理地址；如果使用 NAT 网络，请在代理软件允许的范围内提供 WSL 可访问的地址。

安装完成后，重新打开 Codex App，在插件中打开“SSH 终端”。正在运行任务时，可等任务结束后再重开 App。

## 导入服务器和填写密码

默认导入 `%USERPROFILE%\.ssh\config` 中直接声明的 `Host` 别名。支持导入 `HostName`、`User`、`Port`、`ProxyJump`、已存在的 `IdentityFile`、`PubkeyAuthentication` 和 `PreferredAuthentications`。不会复制私钥内容。使用 `Include` 的配置可通过 `-Hosts` 显式选择别名；需要同时包含所引用的跳板机别名。自定义 `ProxyCommand` 和其他高级 SSH 配置需要自行配置，不会直接迁移。

指定 WSL 发行版、配置文件或服务器列表：

```powershell
.\Install.ps1 -Distro Ubuntu-24.04 -SshConfig "$env:USERPROFILE\.ssh\config" -Hosts bastion,gpu-a,gpu-b,password-host -MfaHosts bastion,gpu-a,gpu-b -PasswordHost password-host
```

以上数组参数请在 PowerShell 中直接调用脚本。如果执行策略阻止脚本，可先对当前进程运行 `Set-ExecutionPolicy -Scope Process Bypass`。

安装后在 **WSL 的本地文件**中填写密码：

```text
~/.config/codex-ssh-terminal-mcp/passwords.json
```

该文件由安装器生成，保留每台服务器的 `host`、`user` 等信息，只需填写对应的 `password` 值。例如下面的密码字段初始为空：

```json
{
  "enabled": true,
  "profiles": {
    "password-host": {
      "password": "",
      "host": "192.0.2.10",
      "user": "alice",
      "jump_profiles": []
    }
  }
}
```

可以在 Windows 文件资源管理器打开 `\\wsl.localhost\Ubuntu-24.04\home\你的WSL用户名\.config\codex-ssh-terminal-mcp\` 来编辑。密码以明文保存在本机，WSL 目录权限为 700、密码文件权限为 600。请将密码留在本地文件中；验证码仅在面板的私密认证区域输入，不要发给 Codex。

若没有可导入的 SSH 配置，可以在面板中单独添加连接，再按上面的格式配置自动填写密码。四机联动只在四个别名均存在且符合该认证拓扑时使用，其余服务器可单独连接。

四机联动配置保存在同目录的 `batch-profiles.json`。首次安装默认使用 `8HFA40`、`hf6-A800`、`hf7-A40` 和 `HF3090` 四个别名；其他环境建议通过安装参数替换。示例见 [examples](examples)。

## 日常使用

1. 打开“SSH 终端”，选择服务器并连接，或使用四机联动按钮。
2. 首次连接时核对服务器指纹；密码由本地文件自动填写，动态验证码在认证区域输入。
3. 登录后直接点击终端输入命令。右键或 Ctrl+V 粘贴；若 App 限制剪贴板权限，可使用底部备用输入框。
4. 让 Codex 执行命令时，指定服务器别名并要求使用 SSH 插件的 `send_command`，随后通过 `command_status` 读取结果。每次命令使用新 shell，请在同一条命令中写明工作目录和环境激活步骤。

示例提示：`在 gpu-a 上通过 SSH 插件运行 nvidia-smi，使用独立命令通道，保留我的终端和主连接。`

请勿让代理主动停止用户的主连接或终端。长时间训练建议放在远端 `tmux`、`screen` 或任务调度器中；本机休眠、断网或重新认证不能保证交互式前台任务持续运行。

## 更新与本地数据

Git 安装可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Update.ps1
```

ZIP 安装请下载新版、解压并重新运行 `Install.ps1`。更新会沿用已有发行版与运行目录，并保留已填写的密码和先前导入的服务器。修改四机分组时应显式传入 `-MfaHosts`、`-PasswordHost`。更新后如 App 仍缓存旧插件，可在方便时重开 App。

默认本地位置：

| 内容 | 位置 |
| --- | --- |
| Windows 启动器与 TCP 转发器 | `%LOCALAPPDATA%\CodexSshTerminal` |
| WSL 后端代码和虚拟环境 | `~/.local/share/codex-ssh-terminal-mcp` |
| WSL 配置、密码、主连接状态与日志 | `~/.config/codex-ssh-terminal-mcp` |

第二台电脑需要自己的 WSL、SSH 配置、密码文件和首次认证。GitHub 只分发代码，不同步账号凭据或正在运行的 SSH 连接。卸载可在 Codex 插件管理中移除本插件；确认不再需要连接后再自行删除上述本地目录。

## 开发与验证

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
node scripts/build_terminal_assets.cjs
```

JavaScript 测试位于 `tests/test_*.cjs`，可分别用 Node 运行。终端依赖已随源码提供，构建资源不需要 npm 下载。

原生插件市场的安装机制见 [OpenAI 插件开发文档](https://developers.openai.com/plugins/build/plugins)。MCP 通过 stdio 连接 Windows 启动器，后端在 WSL 中运行 OpenSSH 和 PTY；面板优先使用本地 WebSocket 实时通道，并保留兼容通道。

## 许可与来源

Apache-2.0，保留上游 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。xterm.js 和 addon-fit 使用 MIT 许可，随附于 `static/vendor`。此分支增加了 Windows/WSL 安装、原生面板、实时终端、本地密码自动填写、四机联动、连接恢复和独立代理命令通道等功能。
