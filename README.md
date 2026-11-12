# Plugins Manager

Plugins Manager 是一个可独立安装的 Python 3.11+ 项目。运行时只需要 Python 和 Git。项目将 Git 上游整理到 `plugins/<name>`，并按配方生成、合并或保留各平台的插件清单。

## 使用方法

从项目根目录运行：

```bash
cd /home/hb/projects/plugins_manager
python3 -m plugins_manager list
python3 -m plugins_manager generate
python3 -m plugins_manager update --dry-run zotero-cli paper-spine
python3 -m plugins_manager update zotero-cli
```

在虚拟环境中运行 `python3 -m pip install .` 后，也可以使用 `plugins-manager` 命令。默认配方位于 `configs/plugins/`，每个插件一个 TOML 文件。默认图标位于 `configs/icons/`。这些资源与 Python 安装包分开，换一份配置或图标无需重新安装。

`--configs` 指定配置目录。省略时，工具从当前目录、上级目录及项目目录查找 `configs/plugins/`。配方放在配置目录的 `plugins/`，图标放在 `icons/`，自定义模板放在 `template/`。`generate` 和 `update` 默认使用配置目录旁边的 `research-marketplace/`，也可用 `--output` 指定其他路径。

```bash
python3 -m plugins_manager generate --output /path/to/export
plugins-manager generate --configs /path/to/configs --output /path/to/export --dry-run
```

`--dry-run` 会克隆指定 Git 引用并运行完整的转换和校验流程，然后清理暂存结果。试运行期间不会创建输出目录。正式更新会替换整个目标插件目录，运行前请保存目标内需要保留的本地改动。单个插件按目录替换，失败时恢复旧目录。批量更新遇到第一个失败时，会报告插件名和错误原因，跳过剩余插件和 marketplace 刷新。之前已成功发布的插件继续生效。同一插件的并发发布通过 `.tmp/` 下的锁隔离。更新结束后会删除空的 `.tmp/`，活动锁或恢复备份会使该目录保留。如果进程被强制终止，先检查目标目录以及锁目录下是否留有 `previous/`，确认没有进程在运行后再处理残留的锁。

## 插件图标

在配方的插件表中设置 `icon`。普通相对路径以 `--configs` 指定或自动发现的配置目录为基准。使用 `config:` 前缀时，路径以当前 TOML 文件所在目录为基准。也可以使用本地绝对路径或 HTTP(S) 直达 PNG 的 URL。

```toml
[plugins.example]
repo = "https://example.org/team/tools.git"
icon = "config:../icons/example.png"
# 或改用 icon = "https://example.org/images/example.png"
```

配置图标后，工具在 Codex 和 Claude Code 的插件根目录下写入 `assets/icon.png`，并在生成或合并清单时填写 Codex 的 `interface.logo`、`interface.composerIcon` 或 Claude Code 的 `icon`。只生成 Copilot 清单的插件会跳过图标。PNG 上限为 5 MiB，读取失败会中止当前插件的发布。

默认配方从项目的 `configs/icons/` 读取图标，图形来源和许可见 [Lucide 许可](configs/icons/LICENSE-Lucide.txt)。

## 默认配方

| 插件 | 导入方式 | 布局差异 |
| --- | --- | --- |
| academic-research-skills | 选择性导入 | 保留五个技能及根目录运行资源，同时为 `skills/` 发现布局提供完整副本 |
| academic-research-suite | 选择性导入 | 整体保留 suite 技能，根目录缺失时从 `plugins/ars-codex/` 导入 |
| nature-skills | 选择性导入 | 导入许可证和完整技能依赖，剔除仓库站点、开发文件和仅供 README 展示的图片 |
| research-writing-assistant | 选择性导入 | 整体保留技能，导入运行脚本、启动钩子和写作模板 |
| paper-spine | 选择性导入 | 分别导入 `dist/claude` 和 `dist/codex`，将清单写入各自的子目录 |
| zotero-cli | 选择性导入 | 导入 `skill/` 下的 `zotero-cli` 技能 |

默认配方沿用原脚本的上游仓库、分支和 marketplace 元数据。默认版本取指向实际检出提交的 Git 标签（去除数字版本前的 `v` 或 `V`），无匹配标签时取检出提交的七位 SHA。需要固定版本时可在配方中设置 `version`。缺少插件清单模板时，工具直接从配方生成清单。缺少项目的 .gitignore 模板时，使用随包附带的版本。

## 接入新插件

接入新插件时，在 [配置目录](configs/plugins/) 新增一个以插件名命名的 TOML 文件，例如 `configs/plugins/example.toml`。工具按文件名排序加载配置目录下的 TOML 文件，并检查插件名是否重复。以下示例从 monorepo 的子目录导入技能，保留 Codex 清单中的平台字段。

```toml
[plugins.example]
repo = "https://example.org/team/tools.git"
ref = "refs/tags/v1.2.0"
source_dir = "packages/example"
layout = "selective"
gitignore = "keep"
marketplaces = ["codex"]
required = ["skills/example/SKILL.md"]

operations = [
    { action = "copy", from = "skill", to = "skills/example" },
    { action = "copy", from = ".codex-plugin/plugin.json", to = ".codex-plugin/plugin.json" },
    { action = "copy", from = "README.md", to = "README.md", optional = true },
]

[plugins.example.manifests]
".codex-plugin/plugin.json" = "merge"

[plugins.example.metadata]
description = "Example research workflow"
```

```bash
python3 -m plugins_manager generate --dry-run
```

- `repo` 支持 Git URL、本地绝对路径或相对于配置目录的本地路径。`ref` 默认检出远程默认分支，也可指定分支、标签或可从仓库取得的提交。Git 子模块需要单独处理。
- `source_dir` 默认是仓库根目录。`layout = "full"` 先复制该目录的内容，再应用转换操作。`layout = "selective"` 从空目录开始。
- `copy` 默认从上游复制。用 `origin = "stage"` 可从暂存目录复制。`move` 在暂存目录内移动。`remove` 接收路径或 glob 数组。`mkdir` 创建目录。`replace` 按字面替换 UTF-8 文件中的文本。
- `copy`、`move` 和 `replace` 支持 `optional = true` 跳过缺失文件。已存在的目标或缺少待替换文本会报错。`move` 可用 `skip_existing = true` 保留现有目标。`replace` 可用 `allow_unchanged = true` 接受已包含新文本的文件。`required` 列出所有转换完成后必须存在的路径。
- `marketplaces` 指定插件可收录的平台，值为 `codex`、`claude`、`copilot` 的数组。省略时收录到所有具有对应插件清单的平台，`marketplaces = []` 表示不收录。
- `manifests` 将相对路径映射到处理方式。`template` 按平台生成，`merge` 在已有清单上更新配方指定的字段，`preserve` 校验已有清单的名称并保留其内容。`manifests = {}` 可跳过清单处理。
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

已受支持的 Agent Plugins `$schema` 会沿用。也可通过 `manifest_fields.copilot` 显式指定。旧配方若指定了 `.github/plugin/plugin.json` 或 `.plugin/plugin.json`，请把目标改为插件根目录下的 `plugin.json`。已暂存的旧 Copilot 清单会在生成时清除。

工具会把可以直接转换的 `.mcp.json` 写成同 schema 版本的 `mcp.json`，并把常规位置的 Copilot v1 hooks 复制到 `com.github.copilot/hooks/hooks.json`。共用源文件保留给其他平台。自定义组件路径可通过配方的 `operations` 整理到新版目录，带请求头或其他无法直接转换的 MCP 配置需提供原生 `mcp.json`。

Codex 的 `interface` 由生成器根据插件名称、简介、作者、主页和图标推导。

可以在配置目录的 `template/` 下提供平台模板。对应文件为 `.codex-plugin/plugin.json`、`.claude-plugin/plugin.json` 和 `plugin.json`。当版本不符合 SemVer 时，Codex 和 Copilot 清单使用 `0.0.0+git.<sha>`，Claude 保留上游版本。公开上架所需的审核信息和链接应由发布者提供。

- [OpenAI Docs 清单字段](https://developers.openai.com/plugins/deploy/submission#manifest-fields)
- [Claude Code 清单参考](https://code.claude.com/docs/en/plugins-reference)
- [GitHub Copilot CLI 插件参考](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)

## 市场清单

生成或更新成功后，工具会按配方刷新市场清单。各平台的清单位置如下。

- Codex 的 `.agents/plugins/marketplace.json`
- Claude Code 的 `.claude-plugin/marketplace.json`
- GitHub Copilot 的 `.github/plugin/marketplace.json`

`generate` 默认生成所有适用平台的市场清单。可以重复指定 `--type` 选择要生成的清单。

```bash
python3 -m plugins_manager generate --type codex --type copilot
```

`update` 根据输出目录中已有的市场清单推断平台。若没有任何市场清单，先运行 `generate`。插件收录的平台仍由配方 `marketplaces` 和已声明的插件清单决定。刷新时会移除已被当前配方排除的平台条目。

生成器会为 Codex 市场条目补齐分类和安装策略字段，默认分类为 `Productivity`。

条目的来源路径相对于 marketplace 根目录。例如 PaperSpine 的 Claude 和 Copilot 条目指向 `./plugins/paper-spine/claude`，Codex 条目指向 `./plugins/paper-spine/codex`。重新生成时，匹配条目的来源路径和配方提供的目录元数据会更新。现有安装策略、额外字段和其他插件条目会保留。

## 生成、更新与发布

- `generate` 按配方生成本地插件和市场清单。默认处理全部插件，也可以列出要生成的插件名。直接运行 `python3 -m plugins_manager` 会显示帮助。
- `update` 在已生成的 marketplace 中更新插件并刷新现有平台的清单。默认更新全部配置的插件，也可以列出要更新的插件名。
- `publish` 读取当前目录下的 `research-marketplace/` 并推送到指定仓库分支。产物在其他位置时，用 `--output` 指定。命令需要同时指定 `--repo` 和 `--branch`。

```bash
python3 -m plugins_manager generate
python3 -m plugins_manager update
python3 -m plugins_manager publish --repo OWNER/REPO --branch marketplace
```

`generate` 和 `update` 都支持 `--exclude PLUGIN`，跳过指定插件及其市场条目。一个选项可以接多个插件名，也可以重复指定。如果同时列出要处理的插件，请把它们放在排除选项前。

```bash
python3 -m plugins_manager generate zotero-cli --type codex
python3 -m plugins_manager generate --exclude zotero-cli paper-spine
python3 -m plugins_manager update --exclude zotero-cli paper-spine
```

`--configs` 用于 `list`、`generate` 和 `update`。`--repo` 接受 GitHub 的 `OWNER/REPO`、HTTPS 或 SSH 克隆地址。用 `--message` 可以指定提交说明。提交使用本机 Git 身份，推送使用已配置的认证。[GitHub 认证文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github)

发布时用本地市场清单及其引用的 `plugins/` 子目录替换远端的对应路径，遵循 `.gitignore`，保留目标分支的其他文件。新分支从远端默认分支创建，空仓库则创建首个提交。已有分支没有文件变化时不创建新提交。

`generate --dry-run` 和 `update --dry-run` 验证插件构建。`publish --dry-run` 预览和目标分支的文件差异数量。

## 本地测试

从项目根目录运行 `python3 -m unittest discover -s tests -v`。测试使用临时 Git 仓库，无需联网。
