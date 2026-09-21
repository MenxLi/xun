
docs用MkDocs构建（material主题 + mkdocs-static-i18n插件），中/英双语，主要分以下几个章节：

- 软件概述（主要讲解软件的主要功能和最主要的设计特点）
- 软件安装和内置会话入口(四种，需要参数列表)
- API 文档（就各主要功能进行讲解，例如Agent 初始化，Type-State Pattern，Config，Extension等）
- 更新日志（我已整理，你转为英文即可）

整体逻辑应清晰明了，章节之间要有明确的层次关系，便于读者快速找到所需信息。
适当画一些示意图，帮助理解文档结构和各模块之间的关系；形式由你决定（mermaid最好、复杂图像SVG、PNG 等均可，哪种清楚好看用哪种），请确保图示结构、对位正确。
在不太显眼的地方标明文档所对应的版本信息。

---
工程约定（重新生成时须遵循，保证结构稳定）：

- `mkdocs.yml` 放在仓库根目录，双语用 mkdocs-static-i18n：中文为默认语言，中文页面放 `docs/zh/` 下，英文译文放 `docs/en/` 下同名文件，两侧目录结构一一对应，不要另起结构。
- Material 的语言切换器必须由 mkdocs-static-i18n 生成，并启用 `reconfigure_material: true`；不要在 `extra.alternates` 中手写 `/en/`、`/zh/` 等根绝对链接。不要启用与多语言切换器不兼容的 `navigation.instant`。
- `docs/AGENTS.md` 是本指令文件，不进导航；`docs/zh/changelog.md` 是中文原文，英文版由你输出到 `docs/en/changelog.md`。
- 除 `mkdocs.yml` 和 `docs/zh/`、`docs/en/` 下的源文件外不要创建其它工程文件；已有的 `docs/AGENTS.md`、`docs/zh/changelog.md` 不要移动或改名。

写完后在仓库根目录执行 `mkdocs build --strict`，输出到 `{repo_root}/site` 目录供我查阅。

构建产物必须能挂载在任意 URL 前缀下（例如 `/alice/docs/`），不能假定站点位于域名根目录：

- 不要设置 `site_url`，Markdown、导航、语言切换器以及主题资源一律使用相对链接。
- 构建后检查 `site/**/*.html`，不得出现 `href="/en/"`、`href="/zh/"`，也不得出现其它站内 `href`、`src` 或 `action` 以单个 `/` 开头的根绝对 URL；`https://` 等外部 URL 不受此限制。
- 至少核对中文首页、英文首页和一对同名内页的语言切换链接，切换后必须停留在对应页面，并保留部署前缀；发现根绝对链接时先修正 `mkdocs.yml` 或插件配置，再重新构建，不能直接修改 `site/`。