# paper2zh（Windows 本地论文阅读与翻译）

paper2zh 在本机浏览器中管理和阅读 PDF。左侧可按嵌套文件夹整理论文；右侧提供原文、译文和对照视图。导入后即可阅读原文，再决定是否指定页码试译或翻译全文。排版翻译使用 BabelDOC。

## 官网与维护

正式介绍页由 [个人站点](https://liangji-seu.github.io/software/paper2zh/) 维护。`docs/` 中的 `MACOS_HANDOFF.md` 等文件是源码文档，旧静态页面仅作历史留存，不再自动发布；本仓库只维护源码、持续集成和安装包发布。

## 安装桌面版

从 [当前公开发布页](https://github.com/liangji-seu/paper2zh/releases/latest)下载 Windows 安装包；当前源码版本为 `v1.1.4`。安装程序默认安装在 `E:\paper2zh`，无需管理员权限；系统需要 Microsoft WebView2 运行时。桌面版附带独立 Python 3.11 与 BabelDOC 依赖，不要求系统安装 Python。首次翻译可能下载模型、字体等资源，请保持网络连接。

桌面版使用系统文件窗口选择 PDF。文献、译文、批注及 SQLite 索引保存在安装目录的 `library/` 中，默认路径为 `E:\paper2zh\library`。升级后首次打开会复制旧 `%LOCALAPPDATA%\paper2zh` 中的文献与文件夹记录，旧目录保留。API 配置和已有模型缓存继续使用原用户目录；API Key 由当前 Windows 用户的 DPAPI 加密。安装包不包含用户论文、数据库、Markdown、Key、批注或模型缓存。源码开发版继续使用独立的 `data/`。

## 文件库与 Agent 读取

导入时保存原 PDF 副本，并按文件 SHA-256 查重。同一 PDF 改名或移动后再次导入，会打开已有文献并提示，不会重复建档或启动翻译。原文和译文通过固定文献 ID 关联；翻译完成后，工作区优先显示从译文识别的中文标题，识别不可靠时保留原文件名。

翻译成功后在后台自动生成 `library/jobs/<文献ID>/markdown/source.md`、`translated.md` 和 `metadata.json`。`library/index.json` 列出已导出的文献及相对路径，Codex 等 Agent 可以直接读取，无需访问 API 设置。`source.md` 保留完整原文；试译的中文 Markdown 只对应所选页码，元数据明确记录翻译范围。Markdown 包含页码与 PDF 回链，采用本地文字提取，不额外调用翻译 API；扫描页及复杂公式、表格的准确排版仍以 PDF 为准。请备份整个 `library/`，保持数据库、文献和批注一起保存。

顶部采用紧凑工具栏；拖动侧栏分隔线可调整宽度，点击箭头可折叠，重启后恢复宽度。预览默认选择文字，按住空格临时切换抓手，松开恢复。下载按钮直接另存中文 PDF，不再选择原文或双语版本。

## 从源码启动

双击 启动阅读器.bat，打开 http://127.0.0.1:8765 。首次安装翻译引擎时运行 安装BabelDOC.ps1；它将 BabelDOC 0.6.4 及完整依赖安装到项目隔离环境。启动脚本优先寻找 Python 3.11 与 .runtime311。

在左侧底部“API 设置”中填写 Provider、Base URL、模型和 API Key，先保存再点“测试连接”。测试连接会向所设服务发送一次最小请求。默认配置使用 DeepSeek 的 OpenAI 兼容接口 https://api.deepseek.com 与 deepseek-v4-flash；在官方 DeepSeek 接口进行 BabelDOC 翻译时，程序明确关闭默认思考模式，以免推理内容耗尽译文输出预算。其他兼容服务不发送该专有参数。

## 阅读与批注

- 左侧可新建多级文件夹、展开或收起、重命名、移动或删除文件夹；删除文件夹时，其中的文献和子文件夹会移到上一级，文献文件和内容都会保留。论文可通过右键或操作菜单移动，也可以直接拖到文件夹或根目录。导入 PDF 时可仅导入、导入后翻译或运行不调用 API 的演示流程；桌面版和浏览器均支持一次选择多篇并按顺序批量入库，批量模式只执行“仅导入”，逐项报告新增、重复或失败结果。
- 左侧文献支持通过右键或操作菜单删除应用内托管副本与记录；删除前会确认，原始 PDF、外部译文和外部文件不会被删除，翻译中的文献会拒绝删除。
- 桌面版经系统文件窗口导入原 PDF 后，真实全文翻译会在该原文件的父目录新建 `translate/`，保存中文、双语 PDF 及源文件校验清单；再次导入同一源文件时，校验通过即可复用译文，无需重复调用 API。原目录不可写时仍保留应用内译文并提示，不覆盖已有人工文件。
- 右侧使用项目内附带的 PDF.js 和文字层渲染 PDF。原文在翻译前及翻译失败后都可阅读；原文、译文和同页对照支持连续滚动，切换视图时保留当前页、缩放和批注状态。连续阅读维护有界的 PDF 文档与页面缓存，按需渲染可视附近页面；连续缩放和窗口调整时会合并待处理的渲染请求。预览区按住 Ctrl 滚轮也可缩放。
- 拖选原文或译文文字，可用黄、绿、蓝、粉四种颜色高亮，或添加下划线。可删除所选批注、撤销最近一次新增；重开论文后会恢复批注。下载菜单可导出含标准 PDF 批注的副本，原始 PDF 不被改写。
- 右下角显示当前论文由翻译服务返回的 Token 累计数与本次运行数。没有可靠用量时显示“未统计”。演示模式的“译文”只是原文副本，界面会明确标注。

BabelDOC 会尽量保留论文的公式、图形与页面结构，但中文长度、字体和换行变化可能改变局部排版，不能保证逐像素一致。全文或所选试译页出现明显正文缺失时，任务会失败并保留源 PDF 与已统计的用量。

## 数据与隐私

服务只绑定 127.0.0.1。源码版的论文、任务、文件夹、批注和输出 PDF 保存在项目 `data/`；Windows 桌面版把文献、译文、批注及 SQLite 索引保存在安装目录的 `library/`，API 设置和已有模型缓存仍保存在用户目录。PDF.js、字体和渲染资源从本机加载，不依赖 CDN。API Key 在 Windows 交互式用户会话下用 DPAPI 加密保存；浏览器只收到是否设置及掩码，不持久化或记录明文。更换 Provider 或 Base URL 时，必须重新填 Key，或明确勾选复用。真实翻译会将论文文本发送到用户设置的 API 服务；本机批注不发送给翻译服务。PDF 服务支持单段 Range 按需读取，并按 64 KiB 分块发送，避免一次性读入整份文件。

关闭浏览器不会停止本地服务；关闭服务命令窗口即可停止。

## 验证

在项目目录运行 `python -m unittest discover -s tests -v`、`node --test tests/reader-geometry.test.mjs tests/pan-interaction.test.mjs tests/file-context-menu.test.mjs tests/library-drag-folder.test.mjs tests/import-batch.test.mjs`，以及 `python -m py_compile app/core.py app/server.py app/annotations.py app/library.py run.py`。自动测试不调用真实 API。16 MiB 文件的独立服务端 tracemalloc 回归测试记录一次性读取峰值 16,786,403 bytes、分块发送峰值 140,830 bytes；这是该测试的峰值对比，不代表整体速度。另使用两页合成英文测试 PDF 验证了本机真实翻译：各页完整中文正文、公式和矢量曲线保留；该测试不使用用户研究论文。复杂表格、扫描页和特殊字体仍需人工检查。

官方依据：BabelDOC https://github.com/funstory-ai/BabelDOC ；DeepSeek 思考模式 https://api-docs.deepseek.com/guides/thinking_mode/ 。

## 构建 Windows 安装包

在 Windows 上用 Python 3.11 执行 `powershell -ExecutionPolicy Bypass -File packaging/build.ps1`。脚本创建隔离构建环境、生成多尺寸应用图标、安装固定版本的桌面壳与翻译引擎、下载并校验官方 Python 3.11.9 嵌入版，再用 PyInstaller 与 Inno Setup 生成 `packaging/output/paper2zh-Setup-<VERSION>-win64.exe`。这些构建目录均被 Git 忽略；`docs/` 中的旧静态页面仅作历史留存，不再自动发布。本地开发机若已有 `.runtime311`，可传 `-UseExistingEngine` 快速预验收，正式构建应使用默认洁净安装流程。

## 跨平台开发交接

Mac 端的源码/适配范围、已知 Windows 专属路径、浏览器服务启动方式和分层验收要求见 [Mac 交接文档](docs/MACOS_HANDOFF.md)。该文档不代表当前已有 Mac 桌面兼容或 Mac 测试证据。
