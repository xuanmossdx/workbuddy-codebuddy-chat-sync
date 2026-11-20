# WorkBuddy 工具台

同步 WorkBuddy / CodeBuddy 聊天记录，管理多账号实例，并把 Qoder / WorkBuddy / CodeBuddy / Trae 的用量折算成等价 API 费用。

## 快速开始

双击 exe 启动。关闭窗口会最小化到右下角托盘，右键托盘图标可以重新打开或退出。

## 三个页签

### 聊天记录同步

列出 WorkBuddy / CodeBuddy 识别到的账号和会话数。点「切换到此账号」把历史会话归到该账号，然后重启对应软件即可看到全部历史。

### 账号

查看 WorkBuddy / Trae / Qoder 的登录态和额度。点「新建 WorkBuddy 账号」会拉起一个独立实例，可以同时登录另一个账号，并在桌面生成 `WorkBuddy - <昵称>` 快捷方式。

### 用量看板

- **总览**：等价 API 总费用、总 token、请求数、缓存命中率，以及每日费用图。
- **Qoder / WorkBuddy / CodeBuddy / Trae**：分别查看各产品的模型、会话和每日消耗。

金额按真实 token × 公开 API 单价折算；credits / 积分只展示，不参与计价。Trae 没有逐会话 token，只展示积分，点「刷新 Trae 额度」才会联网查询。

首次打开用量看板会扫描本地记录，WorkBuddy 约十几秒，之后只处理新增文件。

## 构建

需要 Python 3.9+：

```bat
pip install pystray pillow pyinstaller
pyinstaller --noconfirm workbuddy-codebuddy-chat-sync.spec
```

产物在 `dist\workbuddy-codebuddy-chat-sync.exe`。也可以直接跑源码：

```bat
python src\tray.py
```

## 常见问题

**找不到窗口？** 程序还在托盘运行，点任务栏右下角 `∧`，右键蓝色图标 →「打开管理窗口」。

**Trae 页没有数据？** 点右上角「刷新 Trae 额度」，平时不会自动联网。

**切换账号后列表还是空的？** 重启对应的软件，列表是启动时读进内存的。

## 免责声明

本项目仅供学习和技术研究使用。请自行判断使用场景是否合规，使用产生的一切后果由使用者自行承担。

License: [MIT](LICENSE)
