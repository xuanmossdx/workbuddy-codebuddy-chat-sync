# workbuddy-codebuddy-chat-sync

**WorkBuddy / CodeBuddy 换账号后，聊天记录不会消失。**

这两个软件按账号过滤会话列表。换号登录后旧会话就不显示了——数据没丢，只是被藏起来。这个小工具常驻托盘，自动把本地记录归到当前账号，几个号都能看到全部历史。

---

## 使用

下载 `workbuddy-codebuddy-chat-sync.exe`，双击。会打开**管理窗口**，右下角同时出现**蓝色圆形图标**。

窗口里分两栏列出 WorkBuddy 和 CodeBuddy 识别到的账号，标出当前生效的是哪个；点某个账号后面的「切换到此账号」就能把所有历史会话归到它名下。窗口底部显示上次同步时间和结果，可以「立即同步」或「查看日志」。

切到「账号」页可以管理多个 WorkBuddy 实例：新建账号、切换/置前、停止、按需查看额度。
新建成功后会同时在桌面生成 `WorkBuddy - <昵称>` 的快捷方式，双击即可进入该账号。
这一页只做本地操作，不发签到请求，也不做自动化点击。详见
[docs/账号管理使用说明.md](docs/账号管理使用说明.md)。

换号后等两分钟，或者点「立即同步」，然后**重启那个软件**，历史就回来了。

关闭窗口不会退出程序，只是最小化到托盘继续后台同步。右键托盘图标可以打开窗口、立即同步、设置开机自启或退出。

不需要装 Python，不需要改配置，不需要填账号 ID。程序会自己扫出本机用过的账号，还会从软件日志里读出昵称。

---

## 常见问题

**窗口关了，图标又找不到？** 程序还在托盘运行。点任务栏右下角的 `∧` 展开，右键蓝色图标 →「打开管理窗口」。想让它常驻：任务栏设置 →「选择哪些图标显示在任务栏上」。

**切了账号列表还是空的？** 先点「立即同步」，再重启软件。列表是启动时读进内存的，不重启不刷新。

**Agent 窗口里会话很少？** 那个窗口按工作区（项目目录）过滤，跟账号无关。从对应项目目录启动就能看到。

**Windows 提示「已保护你的电脑」？** 没有代码签名证书的程序都会这样。点「更多信息」→「仍要运行」。

---

## 说明

- 不联网，不上报，不收集任何数据（「刷新额度」是唯一的手动按需查询）
- 只改本地数据库里「会话属于哪个账号」这一个字段，不动软件本体
- 账号表与轻量状态存在 exe 同级的 `data\`；该位置不可写时回退到
  `%APPDATA%\workbuddy-codebuddy-chat-sync\data\`，界面会显示实际位置
- `data\` 里不复制客户端数据库，也不保存登录令牌
- 同步工具的配置与日志仍在 `%APPDATA%\workbuddy-codebuddy-chat-sync\`，格式不变
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

如果 `data\` 在 exe 旁边，一并删掉；如果界面显示回退到了
`%APPDATA%`，再删掉 `%APPDATA%\workbuddy-codebuddy-chat-sync\` 这个文件夹。

---

## 自己构建

需要 Python 3.9+：

```bat
pip install pystray pillow pyinstaller
pyinstaller --onefile --windowed --noupx --exclude-module numpy ^
  --name workbuddy-codebuddy-chat-sync ^
  --icon icon.ico --paths src src\tray.py
```

`--exclude-module numpy` 是为了不把 Pillow 可选依赖 numpy 打进去，否则体积会多出十几 MB。产物约 19 MB。

解密客户端凭据用的 AES-CBC / AES-GCM 是 `src\_aes.py` 里的纯标准库实现，不依赖 `cryptography`，打包时不需要额外安装或附带 DLL。

也可以直接跑源码：`python src\tray.py`（加 `--tray` 只启动托盘，不弹窗口）。

实现细节（数据存哪、为什么必须改三处）：[docs/架构说明.md](docs/架构说明.md)

License: [MIT](LICENSE)
