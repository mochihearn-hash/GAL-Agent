# GAL Agent · 剧情观测室

Windows 桌面应用，让视觉模型通过选定的游戏窗口阅读 GAL / 文字 RPG、推进对话、选择分支。React 前端、Electron 桌面外壳、Python 后端均在本项目中。

0.2.0 使用 LangChain Agent 与中间件组织观察、模型判断和执行。普通一轮仍只请求一次视觉模型；界面不显示轮数，历史保留请求编号。源码虚拟环境已安装 LangChain，独立打包版内置依赖。

## 直接使用

打包产物位于 `release/`。解压 `GAL-Agent-0.2.0-Windows.zip` 后，打开其中的 `GAL-Agent-0.2.0/GAL Agent.exe`。本机也可以直接运行 `release/GAL-Agent-0.2.0/GAL Agent.exe`，请保留同目录资源。

从源码运行可在 PowerShell 中执行：

```powershell
Set-Location F:\play_tool
.\Start.ps1
```

初始化需要 Python 3.13、Node.js 24.14 或更新的兼容版本和网络。独立环境与锁定依赖已经建立；重新安装时运行 `scripts/Setup.ps1`。开发热更新入口为 `npm run dev`。

## 开始一段剧情

1. 点击“新建会话”，只填写名称。重名会话拥有独立 ID 和记忆。
2. 首次添加模型配置，保存后切换会话无需再次输入或确认，新会话自动复用最近选择的配置：名称、API URL、API Key、模型标识、接口类型。模型应支持图片输入。
3. “测试连接”会保存配置，分别检查连接与随机数字图片识别。无认证本地接口可以留空密钥；编辑已有配置时，空白密钥表示保留，另有移除选项。
4. 打开游戏的窗口模式，点击“选择窗口”，核验进程名称和窗口标题。
5. 点击“开始”，切回游戏窗口。Agent 只有在游戏位于前台时才会操作。
6. 打开“悬浮窗”，可直接开始/继续、暂停/接管、确认进度对齐，观察理解和下一步动作，拖动或收起；观看模式不抢游戏焦点。点击“编辑引导”先暂停，结束后可“返回观看”。
7. 游戏失焦会停止输入并等待；重新获得焦点后核对画面再继续。主动暂停、接管或发送引导后不会自动恢复，需要点击继续。

接口地址可以填写基础地址或完整端点：

| 类型 | 基础地址形式 | 自动附加端点 |
|---|---|---|
| 兼容 Chat Completions | `https://your-provider.example/v1` | `/chat/completions` |
| Anthropic Messages | `https://your-provider.example/v1` | `/messages` |
| Gemini GenerateContent | `https://your-provider.example/v1beta` | `/models/{模型}:generateContent` |

不替用户选择模型名称。温度默认不发送，可按服务支持情况填写；JSON 模式默认关闭，模型仍必须返回通过校验的 JSON。

## 会话与续玩

- 会话旁菜单支持重命名和删除。删除包含本会话记录、记忆、Agent 检查点和截图，不删除游戏存档。
- 多个会话持续保留，第一版同时运行一个 Agent。切换会话先暂停，再重新绑定窗口。
- 对话、模型请求、决策、动作和引导持续写入 SQLite。正常退出也会保存检查点。
- 重启不会自动执行输入。绑定当前游戏窗口，核对存档后点击“进度已对齐”，再继续。
- 游戏仍在运行时可以重新绑定；游戏也关闭时，请先载入它自己的存档。
- “恢复进度”可手动保存备注或恢复旧检查点。恢复前会保存当前记忆，后续剧情留在历史记录中，不进入恢复后的上下文。
- 一次输入发送后若未观察到结果就退出，会标记为未确认，禁止自动补点或重放。
- 新会话从空白记忆开始。从中途游戏存档开始时，可以通过引导补充前情。

**Agent 检查点不是游戏存档。** 第一版没有通用的自动游戏存档功能，需要在游戏内自行保存。

## 游戏适配与权限边界

默认设置适合窗口化、无严格限时、以文字和选项为主的游戏。通过“操作设置”调整推进按键、允许按键、点击位置、操作间隔和重试上限。推进方式由配置统一决定，普通对话不会被模型改为空格或其他点击。点击分阶段发送，默认移动后等待50毫秒、按住100毫秒；点击位置可以在预览中标记。

每轮截图后，由模型在同一次请求中判断当前状态、上一操作结果及下一动作。普通动画和闪烁图标不会导致程序作废响应。模型选择等待时，等待2秒后重新截图读取，最多额外2次；转场、逐字显示和加载中自动按此规则等待。模型确认仍是原剧情并决定重复操作时，默认最多额外执行2次；设为0关闭操作重试。两类次数独立，首次不计入额外次数。部分系统输入失败不自动重试。

模型每轮只提出一个动作。程序检查窗口身份、运行版本、焦点、几何信息、点击遮挡和按键白名单后发送输入。剧情是否推进由模型判断，程序不再按像素差或台词字符串否决。文字未完成、转场、加载、菜单和未知状态只允许等待或请求帮助。模型请求期间或发送前失焦后重新截图决策；窗口位置改变则停止旧坐标动作。动作格式失败会保存原始回复及错误字段，可在历史中展开查看，失败后不发送输入。

捕获采用 Windows Graphics Capture，精确绑定 HWND，并核验 PID 和进程创建时间。仅提供游戏客户区图像；窗口尺寸变化会重建捕获。不会退化为整个桌面截图。主窗口和悬浮窗排除在目标选择之外。

`SendInput` 是系统级输入流接口。焦点和位置检查存在竞态，这属于应用层限制，**不是系统级单窗口沙箱**。需要严格隔离时，应在专用虚拟机或隔离环境运行。管理员权限游戏可能因 Windows 输入权限等级限制拒绝操作，应用不自动提权。

模型没有系统命令、任意脚本或文件操作工具。截图内的剧情文字不能扩大它的权限。没有网络检索工具，但无法保证模型训练时从未见过游戏剧情。

当前未验证任何商业 GAL 的兼容性，也未接入真实付费模型验收。限时选项、实时战斗、复杂地图、持续文字动画、特殊字体和模型识字错误都需要额外适配。

摘要格式错误会保存失败响应，并最多进行两次修正请求。校验全部通过后才替换记忆；持续失败时保留原记忆并暂停。

## 本地数据与隐私

开发数据默认保存于项目 `data/`。打包应用使用 Electron 用户数据目录下的 `data/`；Windows 通常位于 `%APPDATA%/GAL Agent/data/`。

API Key 保存在 Windows 凭据管理器中；数据库只保存引用，读取失败明确报错，不自动改为明文。模型输入含游戏截图、剧情摘要和引导，会发送到用户配置的模型服务。

剧情历史、截图和导出 JSON 本身包含游戏内容与引导，请按需要保管。导出不包含 API Key 或认证请求头。可用 `GAL_AGENT_DATA_DIR` 指定独立数据目录，开发测试均使用隔离目录。

## 构建与测试

```powershell
npm run build
npm run test:backend
npm test
npm run pack
```

`npm test` 包括 Electron 实际窗口测试，不需要另行安装浏览器。原生捕获测试单独执行，且应与桌面界面测试串行运行：

```powershell
.\.venv\Scripts\python.exe tests\native_smoke.py
```

它只创建、捕获和操作自己的测试窗口；Windows 不授予前台焦点时会明确跳过输入，不抢占其他应用。

Electron 下载较慢时，可按官方安装文档配置下载镜像后运行本地安装入口：

```powershell
$env:ELECTRON_MIRROR='https://npmmirror.com/mirrors/electron/'
node node_modules\electron\install.js
```

本次更新操作说明见 `docs/UPDATE-0.2.0.md`。测试结果及限制见 `docs/VALIDATION.md`，架构见 `docs/ARCHITECTURE.md`，原始需求见 `docs/REQUIREMENTS.md`。

## 实现依据

- [LangChain Agent](https://docs.langchain.com/oss/python/langchain/agents)
- [LangChain 自定义中间件](https://docs.langchain.com/oss/python/langchain/middleware/custom)

- [Windows 单窗口捕获](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow)
- [Windows SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)
- [OpenAI 官方图片输入文档](https://developers.openai.com/api/docs/guides/images-vision)
- [Anthropic 视觉输入文档](https://platform.claude.com/docs/en/build-with-claude/vision)
- [Gemini GenerateContent](https://ai.google.dev/api/generate-content)
- [Electron 桌面窗口](https://www.electronjs.org/docs/latest/api/browser-window)
