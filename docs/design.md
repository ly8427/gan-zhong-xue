# 设计记录 · gan-zhong-xue v1.0.0

> 把 `干中学` skill 发布到 GitHub 的设计决策。2026-07-06。

## 目标

**测试 + 发行都要**，但现阶段真正的目标是**收真人反馈迭代 v2**——发行只是降低门槛的手段。
动机直接来自 skill 自己的诚实局限：目前所有"它有用"的证据是 n=1（设计者自验），
公开发布前唯一该补的是独立被试试用。

## 关键决策

### 打包：Plugin marketplace（仓库根 = marketplace = 单 plugin = 单 skill）
- 安装：`/plugin marketplace add ly8427/gan-zhong-xue` → `/plugin install gan-zhong-xue@gan-zhong-xue`
- 仓库根用 `source: "./"`，参考 superpowers 的结构。

### 标识符
- marketplace / plugin 名：`gan-zhong-xue`（ASCII，命令行/URL 友好）
- skill 文件夹：`gan-zhong-xue`（ASCII 安装路径）
- skill `name:`：`gan-zhong-xue`（ASCII，符合 skill 规范——`name` 只允许字母/数字/连字符，见 writing-skills 指南）。folder 与 name 一致，无需再验证。
- 中文身份不靠 `name` 字段，靠：正文标题 `# 干中学`、description 里的中文触发词（"帮我搞懂 AI 正在做的这步"…）、README。触发两路：**确定性** `/gan-zhong-xue`（最稳）+ **语义**（description 匹配：明确意图能中、太泛的"我不懂给我讲"不稳）。注意 `/干中学` 不是命令名（name 字段是 gan-zhong-xue），只能"说"、不能"敲"。

### 语言方案：中文本体 + 英文 README 摘要 + 两条缝
- skill 本体保持中文（哲学锚 / 触发词的味不丢）
- `README.en.md` 英文摘要，让全球用户能发现、能转介
- 两条缝：description 并列英文触发词 + 顶部加「输出语言跟随用户」
- 选这条的理由：Claude 多语言，能读中文指令、用英文对话；英文用户唯一的真落差是"触发词是中文发现不了"和"地图模板是中文"——补这两条缝就够，不必全文翻译。

### 路径：`~/.干中学/` → `~/.gan-zhong-xue/`
- 功能上原路径不会真坏（FS 按 UTF-8 字节存取，跟 locale 无关），改是为了用户自检时顺手 + 和安装路径一致。
- 纯文件夹名变更，硬化版逻辑零变化。

### 脱敏 pass（隐私铁则套在 skill 自己头上）
发布前对 SKILL.md / README / examples 正文做脱敏——早期本地实测中踩过脱敏的坑（被自己当场抓到），据此把隐私升为第一铁则。改法：保留"因此加固"的教训，洗掉任何可反推的具体物。
- 删任何会窄化定位的领域限定词。
- 事故表述软化为"因此加固"，不写具体事故物（漏了什么、进了哪个报告）。
- 保留"真机"（承载躬行主线；"可实证深渊"的例子本就暗示硬件大类，属通用领域、非敏感）。
- 所有举例（正例/反例）一律用通用占位或与作者无关的大众器件，绝不用任何可能指向作者真实工作的型号/地址组合。

### examples 占位
真实一轮素材在别处会话，由作者后续脱敏灌入。仓库先放结构模板 + 脱敏清单 + 填写说明。**绝不编造实录。**

### 反馈通道
`.github/ISSUE_TEMPLATE/feedback.md` 只捕获三件：成功信号（"我原来没想到的是___"）、哪招最别扭、地图那格准不准。不做评分量表——和 skill「不伪装评分器」一致。

### 许可证
MIT（和 superpowers / everything-claude-code 一致，最无摩擦）。

## 明确不做（v1 首发边界）
- 不翻译 skill 本体（留 v2）
- 不编造示例
- 不替作者 push / 建仓库（outward-facing，留给作者）

## 文件布局

```
gan-zhong-xue/
├── .claude-plugin/
│   ├── marketplace.json
│   └── plugin.json
├── skills/gan-zhong-xue/SKILL.md      # name: gan-zhong-xue（标题/触发词用中文"干中学"）
├── README.md                           # 中文主入口
├── README.en.md                        # 英文摘要
├── examples/real-round-1.md            # 占位，等作者补真素材
├── .github/ISSUE_TEMPLATE/feedback.md  # 反馈模板
├── docs/design.md                      # 本文件
├── LICENSE                             # MIT
├── CHANGELOG.md
└── .gitignore
```
