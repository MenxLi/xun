
docs用VitePress构建（默认主题 + locales双语，见文末工程约定），英/中双语（英文为默认语言），主要分以下几个章节：

大纲（一、二级标题；仅供参考，标题、内容务必根据实际情况调整）：

- 概述
    - 软件主要功能、应用场景、设计特点
    - 软件安装（包括从源码安装和Docker镜像构建）

- 内置会话入口（需要参数说明）
    - xun/xuns 交互式入口
    - xunc/xunx 沙箱执行
    - xune 扩展 CLI 运行入口

- API 文档
    - Agent 初始化（最好直接使用Agent类，快速配置可用setup_agent方法）
    - Config 字段与配置方法
    - Agent生命周期（Type-State Pattern）
    - 显示扩展
    - 结构化输出
    - 工具设置（Tool Attributes & Tool Context）
    - 内置子智能体派生策略与工厂
    - Hooks
    - 链式取消
    - WebDisplay挂载

- 插件开发指南（内容简明，主要介绍机制、主要扩展点和示例等）
    - 插件和API的区别与联系（见下文“API 和插件的分工”）和加载机制
    - 插件快速开发指南与简要示例
    - Extension Attributes

- 更新日志（我已整理，你转为英文即可）

> API 和插件的分工：
> API 文档主要讲解核心功能的使用方法和接口定义，用于开发者理解和调用核心功能。
> API 是插件实现的基础，但也可以不止用于插件开发，例如直接基于 API 构建自定义工具或脚本。
> 插件则主要用于在保持现有程序入口不变的前提下，依托插件加载机制扩展功能。

要求（重要）：
- 请勿改动源码和现有git track的文件。
- 整体逻辑应清晰明了，尽量压缩文档长度
    - 细节且对读者不重要的内容应该省略。
    - 不稳定或实现较为随意的内容应省略或明确标注。
- 章节之间要有明确的层次关系，便于读者快速找到所需信息。
- 适当画一些示意图，帮助理解文档结构和各模块之间的关系
    - 形式由你决定（mermaid最好、复杂图像SVG、PNG 等均可，哪种清楚好看用哪种）。
    - 请确保图示结构、对位正确。
- 整体完成后委派子智能体进行检查，确保文档内容准确，示意图正确。
- 在不太显眼的地方标明文档所对应的版本信息。

---
工程约定（重新生成时须遵循，保证结构稳定）：

- VitePress 站点根为 `docs/`。英文为默认语言：英文页放 `docs/en/`，中文译文放 `docs/zh/` 下同名文件，两侧目录一一对应。
- `docs/package.json` 由仓库提供（git 跟踪），依赖与 `"docs:build": "vitepress build"` 已配好，勿改动；容器里 Node 来自 nvm，构建前先 `. "$NVM_DIR/nvm.sh"` 再 `npm install`。
- `docs/.vitepress/config.mts` 关键配置（缺失会导致路径或语言切换错误）：
  - `base: './'`（可重定位构建，产物全相对路径）、`outDir: '../site'`（产物输出到仓库根 `site/`）、`cleanUrls: false`、`srcExclude: ['AGENTS.md']`；不要设置 sitemap 等依赖绝对 URL 的配置。
  - `rewrites: { 'en/:page*': ':page*' }`：英文页 URL 位于站点根，中文保留 `/zh/`。页面间相对链接按重写后的 URL 书写并省略 `.md`（en→zh 用 `./zh/guide`，zh→en 用 `../guide`）。
  - `locales`: `root`（label English，link `/`）与 `zh`（简体中文，link `/zh/`），语言切换交给主题。
  - mermaid：默认主题不支持，需 `.vitepress/theme/` 自定义主题把 `mermaid` fence 渲染为组件，组件内动态 `import('mermaid')` 后 `mermaid.render`（SSG 安全）。
- 只在 `docs/` 内创建 VitePress 工程文件与 `docs/en/`、`docs/zh/` 页面；`docs/AGENTS.md`、`docs/package.json`、`docs/zh/changelog.md`（中文原文，英文译文写到 `docs/en/changelog.md`）勿动。生成物（`site/`、`node_modules/` 等）已 gitignore，不入库。
- 完成后在 `docs/` 执行 `npm run docs:build`，产物在仓库根 `site/`。死链会使构建失败（≈ `mkdocs build --strict`），不要关闭该检查。
- 产物须能挂在任意 URL 前缀下（如 `/alice/docs/`）：构建后检查 `site/**/*.html`，站内 `href`/`src`/`action` 不得以单个 `/` 开头（外部 URL 不限）；抽查首页与同名内页的语言切换，切换后须停留在对应页并保留前缀。有问题改配置重建，不得直接改 `site/`。