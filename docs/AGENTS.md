
docs用MkDocs构建（material主题 + mkdocs-static-i18n插件），中/英双语，主要分以下几个章节：

- 软件概述（主要讲解软件的主要功能和最主要的设计特点）
- 软件安装和内置会话入口(四种)
- API 文档（就各主要功能进行讲解，例如Agent 初始化，Type-State Pattern，config，Extension等）
- 更新日志（我已整理，你转为英文即可）

工程约定（重新生成时须遵循，保证结构稳定）：

- `mkdocs.yml` 放在仓库根目录，双语用 mkdocs-static-i18n：中文为默认语言，中文页面放 `docs/zh/` 下，英文译文放 `docs/en/` 下同名文件，两侧目录结构一一对应，不要另起结构。
- `docs/AGENTS.md` 是本指令文件，不进导航；`docs/zh/changelog.md` 是中文原文，英文版由你输出到 `docs/en/changelog.md`。
- 除 `mkdocs.yml` 和 `docs/zh/`、`docs/en/` 下的源文件外不要创建其它工程文件；已有的 `docs/AGENTS.md`、`docs/zh/changelog.md` 不要移动或改名。

整体逻辑应清晰明了，章节之间要有明确的层次关系，便于读者快速找到所需信息。
适当画一些示意图，帮助理解文档结构和各模块之间的关系；形式由你决定（mermaid、SVG、PNG 等均可，哪种清楚好看用哪种），请确保图示结构、对位正确。

写完后在仓库根目录执行 mkdocs build，输出到 {repo_root}/site 目录供我查阅。
不要设置 site_url，页面内一律使用相对链接，保证输出文件能挂载在任意路径下。