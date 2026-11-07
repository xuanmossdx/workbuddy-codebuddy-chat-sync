# workbuddy-codebuddy-chat-sync

**WorkBuddy / CodeBuddy 换账号后，聊天记录不会消失。**

这两个软件按账号过滤会话列表。换号登录后旧会话就不显示了——数据没丢，只是被藏起来。这个小工具常驻托盘，自动把本地记录归到当前账号，几个号都能看到全部历史。

---

## 使用

下载 `workbuddy-codebuddy-chat-sync.exe`，双击。右下角出现**蓝色圆形图标**就是已经在工作了（没看到就点一下 `∧` 展开）。

换号后等两分钟，或者右键点「立即同步一次」，然后**重启那个软件**，历史就回来了。

右键菜单里可以直接指定某个软件归到哪个账号，几个号就列几个。想开机自启就勾上那一项。

不需要装 Python，不需要改配置，不需要填账号 ID。程序会自己扫出本机用过的账号，还会从软件日志里读出昵称。

---

## 常见问题

**图标找不到？** 点任务栏右下角的 `∧` 展开。想让它常驻：任务栏设置 →「选择哪些图标显示在任务栏上」。

**切了账号列表还是空的？** 先点「立即同步一次」，再重启软件。列表是启动时读进内存的，不重启不刷新。

**Agent 窗口里会话很少？** 那个窗口按工作区（项目目录）过滤，跟账号无关。从对应项目目录启动就能看到。

**Windows 提示「已保护你的电脑」？** 没有代码签名证书的程序都会这样。点「更多信息」→「仍要运行」。

---

## 说明

- 不联网，不上报，不收集任何数据
- 只改本地数据库里「会话属于哪个账号」这一个字段，不动软件本体
- 配置和数据存在 `%APPDATA%\workbuddy-codebuddy-chat-sync\`，不在 exe 旁边
- 软件本体与它自己服务器的通信，不受本工具影响

**使用前建议备份**（先把软件关掉）：

```bat
copy "%USERPROFILE%\.workbuddy\workbuddy.db" "%USERPROFILE%\Desktop\workbuddy.db.bak"
copy "%APPDATA%\CodeBuddy CN\codebuddy-sessions.vscdb" "%USERPROFILE%\Desktop\codebuddy.vscdb.bak"
```

---

## 免责声明

**本项目仅供学习和技术研究使用。**

- 禁止用于任何商业用途
- 禁止用于任何违反法律法规、软件服务条款或其他不正当用途
- 本工具通过修改本地数据文件实现功能，绕过了软件原有的账号隔离机制，可能违反相关服务条款，请自行判断使用风险
- 使用本工具产生的一切后果——包括但不限于数据丢失、账号异常、服务受限、法律责任——**由使用者自行承担**
- 作者不对使用本工具造成的任何直接或间接损失承担任何责任
- 下载、运行或修改本项目，即视为你已阅读并同意上述条款

如果你不确定自己的使用场景是否合规，请不要使用。

---

## 卸载

右键图标 → 退出 → 取消勾选「开机自动启动」→ 删除 exe。

彻底清干净再删掉 `%APPDATA%\workbuddy-codebuddy-chat-sync\` 这个文件夹。

---

## 自己构建

需要 Python 3.9+：

```bat
pip install pystray pillow pyinstaller
pyinstaller --onefile --windowed --noupx --name workbuddy-codebuddy-chat-sync ^
  --icon icon.ico --paths src src\tray.py
```

产物在 `dist\`。也可以直接跑源码：`python src\tray.py`

实现细节（数据存哪、为什么必须改三处）：[docs/架构说明.md](docs/架构说明.md)

License: [MIT](LICENSE)
