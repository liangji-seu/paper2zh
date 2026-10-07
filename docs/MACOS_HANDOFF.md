# paper2zh Mac 适配交接

面向接手 Mac 适配的 Codex。本文件对应 `main`、`v1.1.3`、基线提交 `c0a4ea4`，remote 为 `git@github.com:liangji-seu/paper2zh.git`。当前没有 Mac 实机测试证据，也没有签名或公证配置。

## 现状和架构地图

- `app/server.py` 用标准库 HTTP 服务绑定 `127.0.0.1`，提供静态页、PDF Range 读取、导入、任务、设置、批注、库和 Markdown API；`static/` 含工作区与本地 PDF.js。
- `app/core.py` 管理数据目录、设置、SHA-256 去重、翻译任务、BabelDOC 子进程、输出和 Markdown 调度；`app/library.py` 管理嵌套文件夹；`app/catalog.py` 管理索引；`app/annotations.py` 管理批注；`app/pdf_worker.py` 串行化 PDF 操作。
- `run.py` 是浏览器开发入口，`desktop.py` 是 Windows 原生窗口、文件选择和剪贴板桥接入口。后者当前不能视为 Mac 入口。
- `_find_babeldoc()` 优先 `PAPER_TRANSLATOR_PYTHON311`，再查仓库 `.runtime311` 配合 Windows `E:\miniconda\envs\EXO\python.exe` 或 `py -3.11`，最后回退 `.runtime`、PATH 的 `babeldoc` 或 `uv tool run babeldoc`；Mac 需重写/验证选择逻辑。
- 源码开发数据在 `data/`。Windows 打包版托管文献、译文、批注和 SQLite 索引在安装目录 `library/`；API 设置和模型缓存仍在用户目录。源码克隆不包含这些用户数据。

## Mac 获取源码和分支

```bash
git clone git@github.com:liangji-seu/paper2zh.git
cd paper2zh
git switch main
git pull --ff-only origin main
git switch -c macos/desktop-browser-adaptation
git rev-parse --short HEAD
```

SSH 不可用时可用 GitHub 允许的 HTTPS 克隆。不要把 token、API Key、私人 PDF/MD、`data/` 或 `library/` 放入仓库。

## 首次浏览器服务路径

### 源码确认的入口（未在 Mac 运行）

`run.py` 直接调用 `app.server.run()`，默认端口为 `8765`，可用 `PAPER_TRANSLATOR_PORT` 改变。`requirements.txt` 为 `pypdf`、`PyMuPDF`、`cryptography`；静态资源和 PDF.js 已在仓库内。自动测试不调用真实 API。导入、原文预览、文件夹、批注和 Markdown 是待 Mac 验收目标；Markdown 自动生成发生在翻译成功后，离线验收须使用现有合成测试或明确的演示任务。

### 待 Mac 实机验证（不要据此宣称兼容）

```bash
python3.11 --version
node --version
python3.11 -m venv .venv-macos
source .venv-macos/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
PAPER_TRANSLATOR_DATA_DIR="$PWD/data/macos-dev" python run.py
```

Mac agent 先确认已安装 Python 3.11+ 和受支持的 Node.js 版本，再执行上述命令；不要默认任意 `python3`。浏览器打开 `http://127.0.0.1:8765`。首次只用公开/合成 PDF，验证原文、文件夹、批注、重复导入，以及通过现有合成测试或明确演示任务生成的 Markdown；是否需要额外 Mac 系统库、Python/Node 版本或 Apple Silicon 原生轮子，必须由 Mac 记录后再写入文档。

## 必须处理的 Windows 专属项

| 项 | 当前事实 | Mac 工作项 |
|---|---|---|
| `windll` | `desktop.py` 无条件调用 `ctypes.windll.shell32`；剪贴板还调用 `kernel32/user32` | 按平台条件化，选择 Mac 剪贴板方案并验证失败提示 |
| GUI | `webview.start(gui="edgechromium")`，带 `.ico` | 选择并实测 Mac GUI 后端与图标资源；不要假设 WebView2 |
| Key | `app/security.py` 用 `crypt32` DPAPI；当前 `protect(nonempty)` 在非 `win32` 抛 `OSError` | 用 Mac Keychain 或等价本机安全存储，重新验证设置流程 |
| 路径 | `LOCALAPPDATA`、反斜杠、旧库迁移和缓存环境变量均是 Windows 语义 | 明确 Mac 设置、缓存、库、日志和临时目录，保留升级/授权语义 |
| 引擎 | 打包依赖 `engine/python.exe`；构建下载 Windows embeddable Python，源码还有 Windows Conda/`py` 路径 | 设计 Mac Python/venv 或独立运行时，验证 BabelDOC 依赖与 arm64 轮子 |
| 打包 | `build.ps1`、PyInstaller spec、Inno Setup、x64 `.exe` | 另做 `.app`/DMG、资源、更新和卸载方案；签名、公证另行配置 |

建议先做 Apple Silicon（arm64）目标，再决定 Intel 或 Universal；不把 Linux/CI 结果当作 Mac 证据。

## 构建发布规划（本次不改 CI/发布）

当前 `ci.yml` 覆盖 Windows/Ubuntu 测试，`release.yml` 只发布 Windows 安装包。本次不要修改它们。后续在 Mac 实机验收后，再增加独立 macOS runner（先 arm64，需要时再 Intel）、`.app`/DMG、校验值、Developer ID 签名和公证；证书、Team ID、notarytool 凭据不得写入仓库。参考：[PyInstaller](https://pyinstaller.org/en/stable/usage.html)、[pywebview](https://pywebview.flowrl.com/guide/installation.html)、[GitHub hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[Apple Developer ID](https://developer.apple.com/developer-id/)。

## 测试和分层验收

```bash
python -m unittest discover -s tests -v
node --test tests/reader-geometry.test.mjs tests/pan-interaction.test.mjs tests/file-context-menu.test.mjs tests/library-drag-folder.test.mjs
python -m py_compile app/security.py app/core.py app/server.py app/annotations.py app/library.py run.py
```

测试文件名：`test_annotations.py`、`test_core.py`、`test_desktop.py`、`test_library.py`、`test_markdown_export.py`、`test_owned_library.py`、`test_pdf_worker.py`、`test_quality.py`、`test_security.py`、`test_server_files.py`、`test_v1.py`，以及四个 `.mjs` 交互测试（含 `library-drag-folder.test.mjs`）。

验收顺序：

1. Windows 功能不回归：桌面文件选择、DPAPI、剪贴板、安装目录 `library/`、升级迁移和引擎选择。
2. Mac 实机服务/窗口：启动停止、端口、文件选择、导入原文、文件夹、批注、拖拽、删除和 Markdown。
3. 公开/合成 PDF 的公式、字体、文字层和复杂页面；记录缺字体、错位或 PDF.js 差异。
4. 托管库重复导入不重复建档/翻译；`source.md`、`translated.md`、`metadata.json` 和回链正确；删除文件夹后归属正确；数据升级可恢复。
5. 真实翻译只用用户明确配置的服务；日志/诊断脱敏且不含 Key、请求头、正文、私人文件名或 PDF/MD 内容。

## 安全和迁移边界

- API Key 仅本地输入；禁止提交或回显。无用户端点/Key 时不做真实 API 测试，不使用默认付费 API。
- 禁止为诊断读取、上传或回显 `settings.json` 正文、私人 PDF/MD、`data/`、`library/`；只用掩码、字段名、哈希、错误类别和脱敏路径。
- API 设置只返回是否设置和掩码，日志不记录请求体/请求头；新增 Mac 诊断遵循同样规则。
- 当前用户数据不随源码克隆。可选迁移只能在用户明确授权后安全本地拷贝库，先校验目标目录/权限/容量；Key 必须在 Mac 重新配置，不能复制 DPAPI 值或从设置正文提取。

## 可复制给 Mac Codex 的提示词

```text
接手 paper2zh macOS 适配。先读 docs/MACOS_HANDOFF.md、README.md、desktop.py、app/security.py、app/core.py、app/server.py、packaging/build.ps1、packaging/paper2zh.spec 和 tests/；基线 main c0a4ea4、v1.1.3。

先在 Apple Silicon 实机创建隔离 Python 环境，安装 requirements.txt，用临时 PAPER_TRANSLATOR_DATA_DIR 启动 python run.py，验证本地服务、公开/合成 PDF、原文、文件夹、批注、Markdown、重复导入。随后按平台适配 windll、edgechromium、剪贴板、DPAPI、LOCALAPPDATA、engine/python.exe、PyInstaller/Inno Setup 和 ico；设计 Mac Keychain、BabelDOC 运行时及 .app/DMG。protect(nonempty) 当前在非 Windows 抛 OSError，desktop.py 当前有无条件 Windows 调用，不能宣称已有 Mac 兼容。

运行 Python 单测、Node 测试（含 library-drag-folder）和 py_compile；保持 Windows 不回归。人工检查 Mac 窗口、公式/字体、托管库、重复导入、Markdown、拖拽/删除文件夹和数据升级。只用明确配置的 API，不读/上传 settings 正文、私人 PDF/MD、data/library，不提交 Key 或未脱敏日志；先不要改 CI/发布、签名或公证配置。完成后报告改动、命令结果、Mac 证据、风险，并确认 macOS 版本、arm64/Intel/Universal、Developer Team、Developer ID 证书和公证凭据；未经授权不要提交/推送。
```

## 交接前待确认

- macOS 版本、机型和架构；建议先确认 arm64。
- 是否支持 Intel/Universal，目标分发形式和更新策略。
- Apple Developer Team、Developer ID Application/Installer 证书、钥匙链和公证账号；签名/公证需另行配置。
- Mac Keychain 方案和用户授权库迁移流程。
