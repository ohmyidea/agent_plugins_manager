# Plugins Manager

Plugins Manager 是一个可独立安装的 Python 3.11+ 项目。运行时只需要 Python 和 Git。项目将 Git 上游整理到 `plugins/<name>`，并按配方生成、合并或保留各平台的插件清单。

## 使用方法

从项目根目录运行：

```bash
cd /home/hb/projects/plugins_manager
python3 -m plugins_manager list
python3 -m plugins_manager generate --type all
python3 -m plugins_manager update --dry-run zotero-cli paper-spine
python3 -m plugins_manager update zotero-cli
```

在虚拟环境中运行 `python3 -m pip install .` 后，也可以使用 `plugins-manager` 命令。默认配方位于 `configs/plugins/`，每个插件一个 TOML 文件。默认图标位于 `configs/icons/`。这些资源与 Python 安装包分开，换一份配置或图标无需重新安装。

`--configs` 指定配置目录。省略时，工具从当前目录、上级目录及项目目录查找 `configs/plugins/`。配方放在配置目录的 `plugins/`，图标放在 `icons/`，自定义模板放在 `template/`。`generate` 和 `update` 的 `--output` 指定输出基目录，默认是配置目录的父目录。

```bash
python3 -m plugins_manager generate --type codex --output /path/to/export
python3 -m plugins_manager generate --type all --output /path/to/export
plugins-manager generate --type all --configs /path/to/configs --output /path/to/export --dry-run
```

`--dry-run` 会克隆指定 Git 引用并运行转换和校验，然后清理暂存结果。试运行期间不会创建输出目录。正式更新会替换整个目标插件目录，运行前请保存目标内需要保留的本地改动。单个插件和它的 marketplace 条目一起更新，失败时恢复旧内容。批量更新遇到第一个失败时，会报告插件名和原因，跳过后续插件。之前成功更新的插件和条目继续生效。同一插件的并发发布通过 `.tmp/` 下的锁隔离。更新结束后会删除空的 `.tmp/`，活动锁或恢复备份会使该目录保留。如果进程被强制终止，先检查目标目录以及锁目录下是否留有 `previous/`，确认没有进程在运行后再处理残留的锁。

下载使用普通的 `git clone --depth=1 --single-branch --no-tags --branch <名称>`，只获取指定分支或标签的一层历史。不指定名称时使用远端默认分支。上游误提交的 `*:Zone.Identifier` 下载标记文件会在检出时被忽略，避免 Windows 文件名错误。

指定 `commit` 时改用 `git init` 和 `git fetch --depth=1 --no-tags <repo> <完整 SHA>`，只获取该提交并验证实际 SHA，不先下载默认分支。远端须允许获取该提交。

## 插件图标

在 `[plugins.<name>.rules]` 中设置 `icon`。普通相对路径以 `--configs` 指定或自动发现的配置目录为基准。使用 `config:` 前缀时，路径以当前 TOML 文件所在目录为基准。也可以使用本地绝对路径或 HTTP(S) 直达 PNG 的 URL。

```toml
[plugins.example]
repo = "https://example.org/team/tools.git"

[plugins.example.rules]
icon = "config:../icons/example.png"
# 或改用 icon = "https://example.org/images/example.png"
```

配置图标后，工具在 Codex 和 Claude Code 的插件根目录下写入 `assets/icon.png`，并在生成或合并清单时填写 Codex 的 `interface.logo`、`interface.composerIcon` 或 Claude Code 的 `icon`。只生成 Copilot 清单的插件会跳过图标。PNG 上限为 5 MiB，读取失败会中止当前插件的发布。

默认配方从项目的 `configs/icons/` 读取图标，当前图标的设计与来源见 [图标说明](configs/icons/README.md)。

## 默认配方

| 插件 | 导入方式 | 布局差异 |
| --- | --- | --- |
| academic-research-skills | 选择性导入 | 五个技能及运行脚本、协议、示例和金标准；保留一份中文 README，排除开发、测试及展示材料 |
| academic-research-suite | 选择性导入 | 只导入 suite 技能及运行依赖，清理开发/测试与历史评测输出；支持 `plugins/ars-codex/` 后备布局 |
| nature-skills | 选择性导入 | 导入许可证和完整技能依赖，剔除仓库站点、开发文件和仅供 README 展示的图片 |
| research-writing-assistant | 选择性导入 | 整体保留技能，导入运行脚本、启动钩子和写作模板 |
| paper-spine | 选择性导入 | 分别导入 `dist/claude` 和 `dist/codex`，将清单写入各自的子目录 |
| zotero-cli | 选择性导入 | 导入 `skill/` 下的 `zotero-cli` 技能 |

默认配方沿用各自的上游仓库和分支。版本依次取 `metadata.<platform>.manifest.version`、平台默认版本、跨平台默认版本、实际检出提交的前七位 SHA。切换标签时，未显式配置的版本同样使用提交 SHA。缺少插件清单模板时，工具直接从配方生成清单。缺少项目的 .gitignore 模板时，使用随包附带的版本。

`research-writing-assistant` 保留 `skills/`（含技能内部参考资料）、7 个运行脚本、Claude 会话钩子、计划与图表模板、LaTeX 模板说明及根 README。展示图片、旧 `modules/`、其他宿主的安装配置及仓库完整性检查脚本不打包；技能验证文档中的仓库开发检查段落也一并移除，稿件质量检查保持可用。

## 接入新插件

接入新插件时，将 [配置模板](configs/plugin_template.toml) 复制到 [配置目录](configs/plugins/)，例如 `configs/plugins/example.toml`，并替换插件名、源码及资源路径。工具按文件名排序加载目录下的 TOML 文件，并检查插件名是否重复。以下示例从 monorepo 的子目录导入技能，保留 Codex 清单中的平台字段。

```toml
[plugins.example]
repo = "https://example.org/team/tools.git"
tag = "v1.2.0"

[plugins.example.rules]
source_dir = "packages/example"
layout = "selective"
gitignore = "keep"
required = ["skills/example/SKILL.md"]

operations = [
    { action = "copy", from = "skill", to = "skills/example" },
    { action = "copy", from = ".codex-plugin/plugin.json", to = ".codex-plugin/plugin.json" },
    { action = "copy", from = "README.md", to = "README.md", optional = true },
]

[plugins.example.metadata]
description = "Example research workflow"
version = "1.2.0"

[plugins.example.metadata.codex]
category = "Productivity"

[plugins.example.metadata.codex.marketplace]
policy = { installation = "AVAILABLE", authentication = "ON_INSTALL" }

[plugins.example.metadata.codex.manifest]
mode = "merge"
interface = { displayName = "Example Research", shortDescription = "Research workflow" }
```

```bash
python3 -m plugins_manager generate --type all --dry-run
```

- 在 `[plugins.<name>]` 中填写 `repo` 和可选的 `branch`、`tag`、`commit`。`repo` 支持 Git URL、本地绝对路径或相对于配置目录的本地路径。Git 子模块需要单独处理。

  优先级为 `commit > tag > branch`；都省略或留空时使用远端默认分支。`branch`、`tag` 填名称，例如 `main`、`v0.14.2`；`commit` 填完整的 40 或 64 位十六进制 SHA，不要将提交 SHA 填入 `tag`。旧的 `ref` 字段已替换：

  ```toml
  branch = "main"
  # 固定标签时，将上面一行替换为：
  # tag = "v0.14.2"
  # 固定提交时改用：
  # commit = "7bff1d0539c0dfad6c30178bb93ce6dd8a5cebf8"
  ```

- `[plugins.<name>.rules]` 集中放置 `source_dir`、`layout`、`gitignore`、`icon`、`required` 和 `operations`。需要迁移旧配方时，将这六项从插件表移到 `rules`。使用默认打包规则时可以省略整个表。
- `source_dir` 默认是仓库根目录。`layout = "full"` 先复制该目录的内容，再应用转换操作。`layout = "selective"` 从空目录开始。
- `copy` 默认从上游复制。用 `origin = "stage"` 可从暂存目录复制。`move` 在暂存目录内移动。`remove` 接收路径或 glob 数组。`mkdir` 创建目录。`replace` 按字面替换 UTF-8 文件中的文本。
- `copy`、`move` 和 `replace` 支持 `optional = true` 跳过缺失文件。已存在的目标或缺少待替换文本会报错。`move` 可用 `skip_existing = true` 保留现有目标。`replace` 可用 `allow_unchanged = true` 接受已包含新文本的文件。`required` 列出所有转换完成后必须存在的路径。
- 所有操作支持可选的 `platform` 条件，可填 `"codex"`、`"claude"`、`"copilot"` 或平台列表。省略或填 `"all"` 时对所有平台执行。单平台构建只运行匹配的操作，非匹配操作不会读取源文件；`--type all` 执行共享产物中各平台匹配的操作，每条只执行一次。多平台独立更新复用一次源码下载，并分别执行各自的操作，保持原有顺序。

  ```toml
  { action = "copy", from = "dist/claude", to = "claude", platform = ["claude", "copilot"] },
  { action = "copy", from = "dist/codex", to = "codex", platform = "codex" },
  { action = "copy", from = "LICENSE", to = "LICENSE" }, # 所有平台执行
  ```

- `[plugins.<name>.metadata]` 接受跨平台共用的版本、描述、作者、主页、仓库、许可证和关键字。`[plugins.<name>.metadata.<platform>]` 声明生成目标，接受该平台的插件清单和目录条目共用字段。Codex 的共用字段是 `category`，会映射到插件的 `interface.category` 和目录条目的 `category`。Claude 和 Copilot 可以覆盖共享元数据，Claude 还支持 `displayName`、`dependencies`、`defaultEnabled` 和 `metadata`。
- `marketplace` 和 `manifest` 分别继承适用于自己输出的默认值，再应用子表覆盖。对象按键合并，数组整体替换。
- 平台下的 `manifest.root` 是打包产物内的相对根目录，默认 `"."`。文件名由平台决定。`mode = "template"` 从模板生成，`"merge"` 以打包产物的原清单为底，`"preserve"` 校验并保留原清单。默认使用 `template`。
- `manifest` 也可声明仅用于插件清单的原生字段，例如 Codex 的 `interface` 或 Claude 的 `userConfig`。清单专属覆盖与市场专属覆盖分别生效。
- 平台下的 `marketplace` 配置条目的展示信息或策略。默认启用；命令行构建会跳过 `enabled = false` 的平台。插件在所有选中平台均未配置或被禁用时，会在下载前直接跳过。条目的名称、来源和版本由产物决定。
- `gitignore` 默认使用 `template/.gitignore`。`keep` 保留导入后的状态，`merge` 将模板追加到导入的文件末尾。

复杂布局可通过 Python 扩展。回调接收上游目录和暂存目录，运行时机在配方操作之后、清单处理和最终校验之前。

```python
from pathlib import Path
from plugins_manager import Tree, Updater, load_specs


def adapt(source: Tree, stage: Tree) -> None:
    stage.copy_from(source, "extras/catalog.md", "references/catalog.md", optional=True)


spec = load_specs(Path("custom-plugins.toml"))["example"]
updater = Updater(Path("/path/to/configs"), output=Path("/path/to/marketplace"))
result = updater.update(spec, transform=adapt, dry_run=True)
print(result.commit)
```

`Tree` 提供 `copy_from`、`move`、`remove`、`mkdir`、`replace_text`、`write_text` 和 `require`。传入斜杠分隔的相对路径。发布前会检查符号链接是否指向插件目录内。

## 各平台的插件清单

| 平台 | 插件根目录下的文件 | 生成规则 |
| --- | --- | --- |
| Codex | `.codex-plugin/plugin.json` | 使用兼容格式。生成 `interface`，并按实际文件填写 `skills`、`mcpServers` 和图标路径。 |
| Claude Code | `.claude-plugin/plugin.json` | 写入共享元数据和可选 `icon`。标准组件目录由 Claude Code 自动发现，可在配方里声明其他组件。 |
| GitHub Copilot CLI | `plugin.json` | 使用 Agent Plugins 格式，默认 `1.0.0`。技能从 `skills/<name>/SKILL.md` 发现，MCP 使用根目录的 `mcp.json`。 |

Copilot 的 `metadata.copilot.manifest.schema` 可指定 `1.0.0` 或 `1.1.0`。省略时采用根清单或模板中受支持的 schema，缺省为 `1.0.0`。生成时会清理该根目录下的旧 Copilot 清单。

工具会把可以直接转换的 `.mcp.json` 写成同 schema 版本的 `mcp.json`，并把常规位置的 Copilot v1 hooks 复制到 `com.github.copilot/hooks/hooks.json`。共用源文件保留给其他平台。自定义组件路径可通过配方的 `operations` 整理到新版目录，带请求头或其他无法直接转换的 MCP 配置需提供原生 `mcp.json`。

Codex 的 `interface` 支持展示名、短描述、长描述、开发者、分类、能力标签、网址、示例提示词、品牌颜色、图标和截图。生成器用共享元数据和已打包图标填充缺省项。显式资源路径以各自插件根目录为基准。Claude 的清单支持 `displayName`、`icon`、`documentationUrl`、`supportUrl`、`privacyPolicyUrl` 和 `termsOfServiceUrl`。

可以在配置目录的 `template/` 下提供平台模板。对应文件为 `.codex-plugin/plugin.json`、`.claude-plugin/plugin.json` 和 `plugin.json`。Codex 和 Copilot 共用安装根时，根清单会带上 Codex 的 `com.openai` 展示配置，核心元数据须一致。需要不同的包描述、版本或组件布局时，请分别设置 `manifest.root`。

- [OpenAI Docs 清单字段](https://developers.openai.com/plugins/deploy/submission#manifest-fields)
- [Claude Code 清单参考](https://code.claude.com/docs/en/plugins-reference)
- [GitHub Copilot CLI 插件参考](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)

## 市场清单

生成或更新成功后，工具会按配方刷新市场清单。各平台的清单位置如下。

- Codex 的 `.agents/plugins/marketplace.json`
- Claude Code 的 `.claude-plugin/marketplace.json`
- GitHub Copilot 的 `.github/plugin/marketplace.json`

`generate` 必须指定 `--type {codex,claude,copilot,all}`。前三项只生成对应平台的独立产物；`--type all` 在一个共享目录中生成已启用平台的清单。

构建前按平台筛选插件。例如，`--type codex` 会跳过没有 Codex 配置或设置了 `metadata.codex.marketplace.enabled = false` 的插件，不下载源码。`--type all` 只构建各插件启用的平台；全部禁用的插件同样不下载。`update` 和 `--dry-run` 使用相同的筛选规则。

```text
<output>/
  codex/research-marketplace/     # 独立 Codex 产物
  claude/research-marketplace/    # 独立 Claude 产物
  copilot/research-marketplace/   # 独立 Copilot 产物
  all/research-marketplace/       # --type all 的共享产物
```

单平台构建会从产物中省略配方声明的其他平台安装子目录。共用相同 `manifest.root` 的平台各自使用该目录的内容。一次 `--type all` 构建中，同一个插件只检出和打包一次，三个平台共用插件产物。

共享目录中已经引用本次构建产物的条目会同步更新或按配置移除。只有本次会写入的目录参与条目校验，`--dry-run` 也使用这一规则。

`update` 默认发现已有的独立平台产物，可用 `--type codex`、`--type claude` 或 `--type copilot` 缩小范围。更新共享产物时使用 `update --type all`。刷新只处理本次选中的插件。独立目录中移除平台配置后，更新会移除该平台的受管产物和条目。

生成器会为 Codex 市场条目补齐分类和安装策略字段，默认分类为 `Productivity`。

条目的来源路径相对于 marketplace 根目录。例如 PaperSpine 的 Claude 和 Copilot 条目指向 `./plugins/paper-spine/claude`，Codex 条目指向 `./plugins/paper-spine/codex`。目录字段优先取平台的 `marketplace`，其次取平台和跨平台默认值；上游清单中的展示字段可以补齐缺省值。Claude 和 Copilot 的版本始终取最终插件清单。Claude 条目还支持 `displayName` 和 `relevance`，Copilot 支持 `category`、`tags` 和 `strict`，Codex 支持 `category` 和 `policy`。显式配置的安装策略覆盖已有值。

在每个 marketplace 内，插件产物和条目一起更新。若写入失败，工具会尝试恢复已改动的清单和原插件。无法恢复的旧清单备份会保留，错误会报告原文件和备份路径。独立模式下已成功更新的平台目录会继续生效。手工条目与其他插件条目保留。

## 生成、更新与发布

- `generate` 按配方生成本地插件和市场清单，必须指定 `--type {codex,claude,copilot,all}`。默认处理全部插件，也可以列出要生成的插件名。直接运行 `python3 -m plugins_manager` 会显示帮助。
- `update` 在已生成的 marketplace 中更新插件并刷新现有平台的清单。默认更新全部配置的插件，也可以列出要更新的插件名。
- `publish` 发布一个 marketplace 根目录，默认是当前目录下的 `all/research-marketplace/`。发布独立平台产物时，将 `--output` 指向该平台的完整 marketplace 路径，并指定 `--repo` 和 `--branch`。各平台可发布到不同分支。

```bash
python3 -m plugins_manager generate --type claude --output ../out
python3 -m plugins_manager update --output ../out --type claude
python3 -m plugins_manager publish --output ../out/claude/research-marketplace --repo OWNER/REPO --branch claude

python3 -m plugins_manager generate --type all --output ../out
python3 -m plugins_manager update --type all --output ../out
python3 -m plugins_manager publish --output ../out/all/research-marketplace --repo OWNER/REPO --branch marketplace
```

`generate` 和 `update` 都支持 `--exclude PLUGIN`，跳过指定插件及其市场条目。一个选项可以接多个插件名，也可以重复指定。如果同时列出要处理的插件，请把它们放在排除选项前。

```bash
python3 -m plugins_manager generate zotero-cli --type codex
python3 -m plugins_manager generate --type all --exclude zotero-cli paper-spine
python3 -m plugins_manager update --exclude zotero-cli paper-spine
```

`--configs` 用于 `list`、`generate` 和 `update`。`--repo` 接受 GitHub 的 `OWNER/REPO`、HTTPS 或 SSH 克隆地址。用 `--message` 可以指定提交说明。提交使用本机 Git 身份，推送使用已配置的认证。[GitHub 认证文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)

发布时用本地市场清单及其引用的 `plugins/` 子目录替换远端的对应路径，遵循 `.gitignore`，保留目标分支的其他文件。新分支从远端默认分支创建，空仓库则创建首个提交。已有分支没有文件变化时不创建新提交。

`generate --type all --dry-run` 和 `update --dry-run` 验证插件构建。`publish --dry-run` 预览和目标分支的文件差异数量。

## 本地测试

从项目根目录运行 `python3 -m unittest discover -s tests -v`。测试使用临时 Git 仓库，无需联网。
