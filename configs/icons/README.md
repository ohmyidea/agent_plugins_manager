# 插件图标

2026-11-16 按用户要求，除 `zotero-cli.png` 外，七个插件图标使用 OpenAI 内置 `image_gen` 重新生成。Zotero 图标保持原文件不变。

这套图标采用清新明亮的颜色、柔软流动的线条、轻微立体质感和较小圆角的方形底板。外框圆角目标约为底板边长的 8%，图形内部保留圆润造型。输出统一为 256×256 RGBA PNG，保留透明边缘。经用户明确同意，导出时通过透明度遮罩清理底板轮廓外的残留杂点，再缩小尺寸；内部图案、颜色和质感保持不变，未通过代码重绘或改色。

| 插件 | 文件 | 图形与配色 |
| --- | --- | --- |
| Academic Figure Skill | [academic-figure-skill.png](academic-figure-skill.png) | 水蓝色图表、圆润柱形与上升曲线 |
| Academic Research Skills | [academic-research-skills.png](academic-research-skills.png) | 薄荷绿笔记本与柠檬黄色放大镜 |
| Academic Research Suite | [academic-research-suite.png](academic-research-suite.png) | 蓝紫色研究、写作、审阅文档环 |
| Nature Skills | [nature-skills.png](nature-skills.png) | 嫩绿色玻璃烧瓶与生长的叶片 |
| PaperSpine | [paper-spine.png](paper-spine.png) | 珊瑚橙书脊、丝带与奶油色纸页 |
| Research Writing Assistant | [research-writing-assistant.png](research-writing-assistant.png) | 紫粉色钢笔与流动墨迹 |
| RW Consulting PPT | [rw-consulting-ppt.png](rw-consulting-ppt.png) | 杏黄色演示屏与青绿色增长曲线 |

完整风格提示词、各图标主题及缩小圆角的最终修订提示词记录在 [generation-prompts.json](generation-prompts.json)。生成器的原始高分辨率图保留在本机 Codex 默认的 `generated_images` 目录，本目录中的 PNG 为实际打包文件。

本次七个新图标为重新生成的图像，并非 Lucide 图标的改色版本。
