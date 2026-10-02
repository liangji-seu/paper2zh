# paper2zh 1.0.0（Windows 本地论文阅读与翻译）

paper2zh 在本机浏览器中管理和阅读 PDF。左侧可按嵌套文件夹整理论文；右侧提供原文、译文和对照视图。导入后即可阅读原文，再决定是否指定页码试译或翻译全文。排版翻译使用 BabelDOC。

## 安装桌面版

从 [v1.0.0 发布页](https://github.com/liangji-seu/paper2zh/releases/tag/v1.0.0)下载 `paper2zh-Setup-1.0.0-win64.exe`。安装程序默认安装在 `E:\paper2zh`，无需管理员权限；系统需要 Microsoft WebView2 运行时。桌面版附带独立 Python 3.11 与 BabelDOC 依赖，不要求系统安装 Python。首次翻译可能下载模型、字体等资源，请保持网络连接。

桌面版使用系统文件窗口选择 PDF。设置、论文、批注和模型缓存保存在 `%LOCALAPPDATA%\paper2zh`；升级和卸载不会清除这些数据。开发版的 `data/` 不会自动复制到安装版，若需迁移，请先退出两个版本，再备份并复制整个 `data/` 到该用户数据目录。API Key 使用当前 Windows 用户的 DPAPI 加密，只能在同一用户环境解密。安装包不包含用户论文、Key、批注或模型缓存。

## 从源码启动

双击 启动阅读器.bat，打开 http://127.0.0.1:8765 。首次安装翻译引擎时运行 安装BabelDOC.ps1；它将 BabelDOC 0.6.4 及完整依赖安装到项目隔离环境。启动脚本优先寻找 Python 3.11 与 .runtime311。

在左侧底部“API 设置”中填写 Provider、Base URL、模型和 API Key，先保存再点“测试连接”。测试连接会向所设服务发送一次最小请求。默认配置使用 DeepSeek 的 OpenAI 兼容接口 https://api.deepseek.com 与 deepseek-v4-flash；在官方 DeepSeek 接口进行 BabelDOC 翻译时，程序明确关闭默认思考模式，以免推理内容耗尽译文输出预算。其他兼容服务不发送该专有参数。

## 阅读与批注

- 左侧可新建多级文件夹、展开或收起、重命名或移动文件夹，并把论文移入指定文件夹。导入 PDF 时可仅导入、导入后翻译或运行不调用 API 的演示流程。
- 桌面版经系统文件窗口导入原 PDF 后，真实全文翻译会在该原文件的父目录新建 `translate/`，保存中文、双语 PDF 及源文件校验清单；再次导入同一源文件时，校验通过即可复用译文，无需重复调用 API。原目录不可写时仍保留应用内译文并提示，不覆盖已有人工文件。
- 右侧使用项目内附带的 PDF.js 和文字层渲染 PDF。原文在翻译前及翻译失败后都可阅读；原文、译文、对照共享页码与缩放。预览区按住 Ctrl 滚轮也可缩放，文档在预览区内滚动。
- 拖选原文或译文文字，可用黄、绿、蓝、粉四种颜色高亮，或添加下划线。可删除所选批注、撤销最近一次新增；重开论文后会恢复批注。下载菜单可导出含标准 PDF 批注的副本，原始 PDF 不被改写。
- 右下角显示当前论文由翻译服务返回的 Token 累计数与本次运行数。没有可靠用量时显示“未统计”。演示模式的“译文”只是原文副本，界面会明确标注。

BabelDOC 会尽量保留论文的公式、图形与页面结构，但中文长度、字体和换行变化可能改变局部排版，不能保证逐像素一致。全文或所选试译页出现明显正文缺失时，任务会失败并保留源 PDF 与已统计的用量。

## 数据与隐私

服务只绑定 127.0.0.1。源码版的论文、任务、文件夹、批注和输出 PDF 保存在项目 `data/`；桌面版保存在 `%LOCALAPPDATA%\paper2zh`。PDF.js、字体和渲染资源从本机加载，不依赖 CDN。API Key 在 Windows 交互式用户会话下用 DPAPI 加密保存；浏览器只收到是否设置及掩码，不持久化或记录明文。更换 Provider 或 Base URL 时，必须重新填 Key，或明确勾选复用。真实翻译会将论文文本发送到用户设置的 API 服务；本机批注不发送给翻译服务。

关闭浏览器不会停止本地服务；关闭服务命令窗口即可停止。

## 验证

在项目目录运行 python -m unittest discover -s tests -v，以及 python -m py_compile app/core.py app/server.py app/annotations.py app/library.py run.py。自动测试不调用真实 API。另使用两页合成英文测试 PDF 验证了本机真实翻译：各页完整中文正文、公式和矢量曲线保留；该测试不使用用户研究论文。复杂表格、扫描页和特殊字体仍需人工检查。

官方依据：BabelDOC https://github.com/funstory-ai/BabelDOC ；DeepSeek 思考模式 https://api-docs.deepseek.com/guides/thinking_mode/ 。

## 构建 Windows 安装包

在 Windows 上用 Python 3.11 执行 `powershell -ExecutionPolicy Bypass -File packaging/build.ps1`。脚本创建隔离构建环境、安装固定版本的桌面壳与翻译引擎、下载并校验官方 Python 3.11.9 嵌入版，再用 PyInstaller 与 Inno Setup 生成 `packaging/output/paper2zh-Setup-1.0.0-win64.exe`。这几个构建目录均被 Git 忽略；网站静态页面在 `docs/`。本地开发机若已有 `.runtime311`，可传 `-UseExistingEngine` 快速预验收，正式构建应使用默认洁净安装流程。
