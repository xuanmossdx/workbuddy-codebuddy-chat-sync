# WorkBuddy 工具台

**聊天记录同步 + 多账号实例 + 多产品用量看板，全部在本机完成。**

WorkBuddy / CodeBuddy 按账号过滤会话列表，换号登录后旧会话就不显示了——数据没丢，只是被藏起来。这个工具常驻托盘，自动把本地记录归到当前账号，几个号都能看到全部历史；同时把 Qoder / WorkBuddy / CodeBuddy 的真实 token 消耗折算成「如果按公开 API 付费要花多少钱」，Trae 则单独展示厂商积分额度。

> 仓库只包含通用程序，不包含任何账号、聊天记录、登录令牌或本机路径。运行时数据只写在本机 `data\`。

---

## 三个页签

### 1. 聊天记录同步

分栏列出 WorkBuddy 和 CodeBuddy 识别到的账号，标出当前生效的是哪个。点「切换到此账号」就把该软件的全部历史会话归到它名下。

换号后等两分钟，或者点「立即同步」，然后**重启那个软件**，历史就回来了。

### 2. 账号

管理 WorkBuddy / Trae / Qoder 的登录态与额度：

- **新建 WorkBuddy 账号**：在 `accountsRoot` 下创建独立实例目录（如 `wb-01`），生成独立的 `launch-workbuddy.ps1`，用隔离的配置目录/用户目录启动同一个 WorkBuddy.exe，因此可以同时登录另一个账号。
- 登录成功后自动登记，并在桌面创建 `WorkBuddy - <昵称>` 快捷方式，双击即进入该实例。
- 支持置前、停止、打开目录、按需刷新额度。
- Trae / Qoder 只读本地登录态，不代为签到、不自动点击。

### 3. 用量看板

- **总览**：等价 API 总费用、总 token、总请求、缓存命中率，产品对比和每日费用堆叠图。
- **Qoder**：读取本机 Qoder 用量看板的 `snapshot.json`；快照过期时自动调用它的 `collect.py`。
- **WorkBuddy**：读取 `~/.workbuddy/projects` 下转录里的真实 input / cache / output token。
- **CodeBuddy**：读取本地会话索引里的真实 token；多账号同步产生的重复目录会按会话 ID 去重。
- **Trae**：本地没有逐会话 token 明细，只展示厂商积分口径，并明确不计入金额汇总；只有点「刷新 Trae 额度」才会联网查询，平时只读缓存。

首次打开用量看板会全量扫描本地记录（WorkBuddy 约十几秒），之后按文件大小 + mtime 增量缓存。

---

## 用量口径（重要）

**金额只用真实 token × 公开 API 单价折算，不使用厂商 credits / 积分计价。**

厂商积分对应额度会被随时调整，所以积分只做展示，不参与任何金额汇总：

- Qoder 的 credits、CodeBuddy 的 credit 都只显示，不参与计算。
- Trae 没有 token 明细，只展示积分，未计入金额汇总。
- WorkBuddy 的免费模型（如 Hy4-Flash-Free）按同档付费模型的公开价估算「等价开销」，方便看白嫖了多少。

单价表在 `src/usage_pricing.json`。打包后把同名文件放到 exe 同级目录，会优先使用你的版本，不用重新打包。

---

## 快速开始

下载/拿到 exe 后双击即可（通用版里叫 `WorkBuddy工具台.exe`，仓库产物名是 `workbuddy-codebuddy-chat-sync.exe`）。

- 会打开管理窗口，右下角出现蓝色圆形托盘图标。
- 关闭窗口不会退出，只是最小化到托盘继续后台同步。
- 右键托盘图标可以打开窗口、立即同步、设置开机自启或退出。
- 不需要安装 Python，不需要改配置，不需要填账号 ID。

---

## 数据位置与隐私

- **不联网、不上报、不收集任何数据**；「刷新额度 / 刷新 Trae 额度」是唯一的手动按需查询。
- 只改本地数据库里「会话属于哪个账号」这一个字段，不动软件本体。
- 账号表与轻量状态存在 exe 同级的 `data\`；该位置不可写时回退到
  `%APPDATA%\workbuddy-codebuddy-chat-sync\data\`，界面会显示实际位置。
- `data\` 里不复制客户端数据库，也不保存登录令牌。
- 用量缓存在 `data\usage-cache.json`，只含 token 计数、时间、模型名，不含聊天正文。
- 仓库和通用版 exe 都不包含任何个人数据。

**使用前建议备份**（先把软件关掉）：

```bat
copy "%USERPROFILE%\.workbuddy\workbuddy.db" "%USERPROFILE%\Desktop\workbuddy.db.bak"
copy "%APPDATA%\CodeBuddy CN\codebuddy-sessions.vscdb" "%USERPROFILE%\Desktop\codebuddy.vscdb.bak"
```

---

## 常见问题

**窗口关了，图标又找不到？** 程序还在托盘运行。点任务栏右下角的 `∧` 展开，右键蓝色图标 →「打开管理窗口」。想让它常驻：任务栏设置 →「选择哪些图标显示在任务栏上」。

**切了账号列表还是空的？** 先点「立即同步」，再重启软件。列表是启动时读进内存的，不重启不刷新。

**用量看板第一次打开很慢？** 在扫 WorkBuddy 的本地转录（约 280 MB），只慢一次；之后只解析新增文件。

**Trae 页显示「尚未查询积分」？** 点右上角「刷新 Trae 额度」。平时不自动联网。

**新建 WorkBuddy 账号一直超时？** 登录的必须是本机还没登记过的新账号；如果是已有账号，它会等到超时。超时目录会保留，不会自动删。

**Agent 窗口里会话很少？** 那个窗口按工作区（项目目录）过滤，跟账号无关。从对应项目目录启动就能看到。

**Windows 提示「已保护你的电脑」？** 没有代码签名证书的程序都会这样。点「更多信息」→「仍要运行」。

---

## 自己构建

需要 Python 3.9+。仓库自带 PyInstaller spec：

```bat
pip install pystray pillow pyinstaller
pyinstaller --noconfirm workbuddy-codebuddy-chat-sync.spec
```

产物在 `dist\workbuddy-codebuddy-chat-sync.exe`，约 19 MB。spec 会把 `src/usage_pricing.json` 一起打进去，并在 `--exclude-module numpy` 里排除 Pillow 的可选依赖。

也可以直接跑源码：

```bat
python src\tray.py          :: 打开管理窗口 + 托盘
python src\tray.py --tray   :: 只启动托盘，不弹窗口
```

解密客户端凭据用的 AES-CBC / AES-GCM 是 `src\_aes.py` 里的纯标准库实现，不依赖 `cryptography`，打包时不需要额外安装或附带 DLL。

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

右键托盘图标 → 退出 → 取消勾选「开机自动启动」→ 删除 exe。

如果 `data\` 在 exe 旁边，一并删掉；如果界面显示回退到了 `%APPDATA%`，再删掉 `%APPDATA%\workbuddy-codebuddy-chat-sync\` 这个文件夹。

License: [MIT](LICENSE)
