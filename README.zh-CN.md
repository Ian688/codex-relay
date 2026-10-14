# Codex Relay

面向 Codex 普通会话与 Goal 的额度监控、暂停与恢复工具。作者：[Ian](https://github.com/Ian688)。独立社区项目，不代表 OpenAI，也不绕过账号限额。

英文为默认界面，支持菜单 **Language / 语言 → 简体中文**，退出重开后生效。[English](README.md)

## 安装

在使用本工具的 Mac 上安装 Python 3.11+、Xcode 命令行工具与 Codex CLI，并登录 Codex Desktop。当前适配 `/Applications/ChatGPT.app`。

```sh
git clone https://github.com/Ian688/codex-relay.git
cd codex-relay
python3 scripts/build_macos.py
open 'dist/Codex Relay 0.6.0.app'
```

这是使用本机 Python/Codex 的本地构建，非自带运行时的通用安装包。仅本地签名，未经 Apple 公证。

## 使用

1. 勾选要监控的会话，选择策略并保存。
2. Goal 控制须单独启用，按 macOS 提示授予辅助功能权限。
3. 点击开始监控。勾选会话、显示 Goal 控制就绪，并不表示已开始监控。

策略包括“额度耗尽后自动续接”与“提前保留额度”。所有阈值按剩余额度判断。普通会话发送继续消息；Goal 使用核验过的真实恢复按钮，并确认新执行轮次。人工暂停不会自动恢复。

关闭窗口后菜单栏仍运行；退出应用停止监控，但不停止 Codex 任务。恢复需要电脑保持唤醒、Relay 与 Codex 运行。

## 验证范围

已在专用真实 Goal 中验证暂停、辅助功能恢复和新轮次启动。额度触发读数为隔离测试中的受控值，不是真实额度耗尽；未等待完整刷新窗口。版本变化、权限不足、目标变化、待处理输入或控件无法核验时停止操作。

详见英文 [验证说明](docs/VALIDATION.md) 和 [信息标准](docs/ARCHITECTURE.md)。MIT 许可。
