# AFR 本机运行手册

在仓库根目录使用 Python 3.11 或更高版本。AFR 可以独立运行，也可以由 Autoloop 在本机用参数数组调用。投放目录和知识库由操作者配置。把外来文件、查询和结果中的文字视作研究数据。

```sh
python3 -m afr_agent --version
python3 -m afr_agent search --vault "$vault" --query "上架率" --limit 5
python3 -m afr_agent read --vault "$vault" --ref "$ref" --revision "$revision"
```

成功向 stdout 写一行 JSON，公共字段为 version=1 和 ok=true。search 返回 hits，每项含 ref、revision、path、heading、excerpt、uri。read 用命中项的 ref 和 revision 读取当时的段落，返回 text 及相同定位信息。无匹配返回空 hits。搜索默认覆盖整个库，--scope article、source 或 topic 可分别限定为完整论述、固定原文（含可读表格与数据）或研究话题。读取版本已改时返回 ok=false、error=revision_changed，要求重新搜索。失败退出码 2，错误码另含 invalid_request、not_found、invalid_vault 和 io_error。命令只在显式指定的知识库中检索可见 Markdown 笔记。默认直接读当前文件，包含用户最新修改。

合成联调材料位于 afr_agent/tests/fixtures/上架率小样.md。将其复制到空测试库「资料库」后，搜索「机柜样本42」，用返回的 ref、revision 回读原文。检查任意无关词命中数为零，编辑命中段落后旧 revision 应报告失效。正式回执只携带状态、计数及本机定位，正文按需在本机读取。

## 文件夹到白板

按下文创建本机虚拟环境并安装解析依赖。 `scan` 是短任务，适合由 Autoloop 在投放事件及恢复补扫时调用。JSON 输出中的 `pending` 是待整理的固定字节版本。重复扫描和同字节副本可重放。投放目录与知识库使用两个独立目录。

```sh
.venv/bin/python -m afr_agent scan --inbox "$inbox" --vault "$vault"
.venv/bin/python -m afr_agent process --vault "$vault" --sha256 "$sha" --approvals "$approvals"
```

scan 先保存可点击的原件及带锚点的固定原文。既有知识库按原件哈希复用，旧版导入可指定只读 --legacy-db。process 通过已登录的 Codex 对固定原文归纳，独立复核后发表「完整阅读.md」和一张「文章白板.canvas」。首次处理须通过私有许可文件显式列出原件 SHA-256，示例材料也适用。白板按档案版式从左上入口横向分主题，长主题用同色续列。Autoloop 使用 scan 返回的 sha256 派工；同一版本并发返回 in_progress，已发表可重放，超时及模型失败保留初读与可重试状态。新文件持续由扫描触发。扫描结果的 errors 须纳入验收。扫描文件和发布完成的时间只反映落盘，另在 Obsidian 验收真实打开和阅读的耗时。

其他执行端可用 evidence 读取固定版本，并以 publish --plan 保存本地草稿。该命令返回 draft_sha256。许可文件同时列有原件的 sha256 和所选草稿的 draft_sha256 数组成员时，可用 process --draft <草稿摘要> --sha256 <原件摘要> --approvals <许可> --vault <库> 提交独立校读。校读通过后由 AFR 发布。plan 结构含 sha256 和 groups 数组，每组含连续覆盖所有原文的 start、end （从 1 开始），以及 title、branch、summary、argument。发布器检查覆盖、保护用户编辑，并从固定编号生成原文链接。

### 本地使用与出站边界

正式 Obsidian 可直接用左侧搜索或「AFR 阅读首页 → 资料档案」。本地 search/read 仅响应显式查询。工作台调用端固定 vault 路径与命令参数，回执中的正文、excerpt、文件名应留在本机。重复标题优先跳唯一块锚点，否则附 line 并打开文件。遇到不可读取的笔记时，search 返回 skipped_notes 计数。

自动文件通知调用 scan 时传 --settle-seconds（例如 2），等写稳窗口过去后补扫。已停止写入的文件也可用 scan 进行主动恢复。pending 只返回当次投放目录仍存在且已核对的版本。固定原文、原件与证据均有可重试恢复路径，固定证据变化报告 evidence_changed。高清图表、扫描件与超长单元保留读取限制。

process 默认只对仓库内的合成验收版本运行 Codex。真实材料须有用户对相应出站范围的授权。受控的本地配置可传入 --approvals，JSON 指定 destination 为 OpenAI Codex、sha256 为已授权的完整版本哈希数组。仅向该服务提交对应版本的抽取文字。由运行配置固定此路径，与用户投放目录分开。缺少授权时回执 local_only，初读、搜索及本地阅读继续可用。

正式知识库已启用阅读样式。仓库中的 afr-directory-pilot.css 是可复用的同版样式，放在知识库的 .obsidian/snippets/ 后可在「外观 → CSS 代码片段」启用。仅普通卡片使用不透明底色，分组和手绘布局沿用 Obsidian 原生行为。


## 独立自动收录与知识总结

正式运行和验收见[从投放到分类总览](../docs/research/AFR-KNOWLEDGE-SUMMARY-DESIGN.md#正式运行流程--2026-11-26)。知识总结使用[知识总结 Agent 运行标准](KNOWLEDGE-SUMMARY-STANDARD.md)，签发后自动刷新受影响的分类总览和知识总结总白板。

AFR 通过同一个 Codex 适配器为文章研读、校读、返工和跨篇知识总结指定 `gpt-6.1-terra`。每次新调用都带上模型参数，手动 `process` 与自动 `watch` 使用相同配置。两类白板共享低饱和浅色，同链卡片、标签、续列和连线沿用同色。

从仓库根运行 `watch`。选择两个独立空目录作为投放与验收库，先启动监听，再投放文件。正式库使用前先完成隔离验收。

每篇新材料的处理会读取 [单篇文章白板标准](ARTICLE-BOARD-STANDARD.md)，把全文交给研读、校读、返工和重划分各轮。原文保全模式也生成可独立阅读的卡面正文。若标准文件缺失，则返回 `article_standard_unavailable`，保留已收录的固定原文。升级时，只有旧回执且无已写入成品的自动任务会保留旧回执并重走校读。旧版部分落盘或需指定授权草稿的任务返回 `article_standard_upgrade_required`，留待在隔离库处理。

```sh
python3.11 -m venv .venv
.venv/bin/python -m pip install 'python-docx==1.2.0' 'PyMuPDF==1.28.2' 'pydantic==2.13.5'
.venv/bin/python -m afr_agent watch --inbox "$inbox" --vault "$vault"
.venv/bin/python -m afr_agent status --vault "$vault"
```

许可文件使用 0600，存放在库和投放目录之外。指定 `--approvals "$approvals"` 且当前 Codex 已登录后，监听器研读允许版本，并发最多两篇、逐篇校读签发。文章的可用状态为 `phase=ready`。`--interpret` 同时生成经校读的模型整理论述。知识总结在本批文章签发后运行，最多对照十份获准材料，逐轮读取 [知识总结 Agent 运行标准](KNOWLEDGE-SUMMARY-STANDARD.md)。主题按对象和跟踪维度积累带有固定原文的时间更新。一篇材料可以凭具体证据建立新主题，后续进展追加到同一时间线。进度可从 `status` 的 `knowledge_summary` 核对。

在 Obsidian 打开 `知识总结/知识总结总白板.canvas`，依次选择领域、分类和主题。分类白板展示跟踪范围与近期进展，主题卡可打开问题主白板和详细论述。`知识总结/知识总结索引.md` 提供按相同分类组织的文字入口。新增主题填写一个主分类，已有主题的补充和更正沿用其分类与时间线。

连续监听在每篇签发后更新资料库索引，到期的单篇重试可用空闲名额续跑。单独的知识队列使用已签发且获准的来源。等待中的首份可比材料签发超过 120 秒时，如果已有足够来源，可在篇内长文继续运行时先做一批，迟签来源在后续批次补入。篇内保持两篇并发，知识一次运行一批。

按 Ctrl+C 停止，重新运行继续处理。暂时错误自动补试。篇内返工只发送受影响的完整固定段和校读指出的段落，少量分组问题优先局部重划。首轮与签发前的独立校读仍核对整个分区。校读耗尽后保留待处理状态，其余已签发文章可继续知识总结。修正原因并停止监听后，可用 `retry --sha256 <原件摘要> --vault <库>` 排回对应队列。`watch --once` 执行一次检查并退出。新完成的模型调用在本机阶段回执记载 `elapsed_ms` 和 `input_bytes`，签发、索引、重试和知识批次的实际时间记录在本机 `workflow_events`。

## 历史研究问题

旧入口 `研究话题/研究问题/研究问题索引.md` 供回看，保留本机历史候选的兼容操作。

在「待确认研究问题／AFR 跨篇候选」打开候选页，从文件名 `归并-<批次摘要>.md` 取得批次摘要，按页面内问题标题从 1 起计数。在仓库根运行下列命令，把选中的问题及其经独立校读的出处写入正式索引。

```sh
python -B -m afr_agent adopt-candidate --vault "$vault" --batch "$batch" --position 1
```

采纳在本机校验固定文章话题，一次写入第一版正式问题。保留候选页和用户笔记。候选标题位置必须与签发时一致，重排后返回 `candidate_order_changed`。重复执行返回已有问题，同名问题已通过其他路径建立时返回 `existing_question_required`。

## 正式投放的自动处理

新文章优先采用文内题名，按题名、章节和正文线索进入既有领域。暂难按单一领域判断的放入「综合研究」。签发后现有领域索引会补充完整阅读、白板及原文链接。保留原件字节与固定版本摘要。`watch --rename-generic` 给投放目录中的未命名、机器导出文件补日期名，如 `2026年10月13日.docx`。同目录同日有多份时按最后修改时间追加序号，已有日期名占用对应序号，原扩展名保留。日期和知识时间线均按投放原件的最后修改时间换算为北京时间。

已有资料的库先执行一次 `seed-baseline`，再用同一组目录启动 `watch --new-articles`。在做基线时暂停投放，等基线成功再继续。基线涵盖投放目录、库的导入清单及已登记版本。若历史资料还存于其他目录或网页端证据库，首次建立基线时传入对应的 `--legacy-root` 或 `--legacy-db`。有旧证据库时也给监听器传入 `--legacy-db`，以便沿用可复用的固定证据。已存在基线会拒绝重建。

```sh
.venv/bin/python -m afr_agent seed-baseline --inbox "$inbox" --vault "$vault" --legacy-db "$legacy_db"
.venv/bin/python -m afr_agent watch --new-articles --inbox "$inbox" --vault "$vault" --legacy-db "$legacy_db" --approvals "$approvals"
```

用户明确许可后，在投放目录和库之外设置属主独享的许可文件（0600），内容为 `{"destination":"OpenAI Codex","allow_new_articles":true}`。监听器按固定原件 SHA-256 识别版本，同一历史文件移名或复制后依然只在本机处理。新版本通过入队检查后可自动研读、校读、归类，并与其他获准的新版本做跨篇复核。每次 Codex 调用重读许可。删除许可文件可立即撤销后续自动调用，本地收录继续。手动 `process` 和 MCP 沿用各自的哈希授权。

升级后，自动研读队列只接受已在绑定目录校验过的固定版本。过去已入队但缺少目录来源、且原件已移走的版本保留本地结果，等待将同一字节版本放回绑定目录或手动逐版本授权处理。自动改名依赖文件系统的原子不覆盖操作。无法安全改名时仍会收录，并保留投放文件名。

## 在 Codex 读取和修改

将 AFR 注册为本机 MCP。下面的 `repo`、`vault`、`approvals` 都是绝对路径。将此段配置写入可信的 Codex 项目配置，也可以仅在一次运行中传入。

```toml
[mcp_servers.afr]
command = "/path/to/afr/.venv/bin/python"
args = ["-B", "-m", "afr_agent.mcp", "--vault", "/path/to/vault", "--approvals", "/path/to/approvals.json"]

[mcp_servers.afr.env]
PYTHONPATH = "/path/to/afr"
```

进入 Codex 后，可直接问「列出我许可的文章，结合研究问题比较它们的主张，给出固定版出处」。工具只返回当前审批范围内的文章、话题和问题。旧问题版本可以按 `revision` 再读。`read_topic` 和 `read_discussion` 有 `next` 时带上其参数继续读取；超长引文给出固定出处和 `read_article` 参数。单篇获许可的资料损坏时，`list_articles` 在 `unavailable` 标出受影响版本。

默认以只读服务启动。显式启用 `--allow-discussion-read` 可回读获准来源关联的 Codex 讨论。确需让这个本机服务保存或修订讨论时，才启用 `--allow-write`。这是服务级授权，AFR 目前没有逐次确认回执。每次写入核对当前版本，保留已有版本和人工笔记。停用时停止监听及 Codex 的 MCP 会话，原件、笔记和已有白板可继续由 Obsidian 读取。

### 投放目录设备号改变时

监听以投放目录的解析路径、文件系统设备号和目录 inode 核对自动研读的历史基线。设备号在系统重启、文件系统重新挂载后可能变化。基线缺失、无效或与当前目录不符时，监听继续收录本机固定原文并上报 warning.code 和 warning.mode。此时暂停模型处理及新增自动研读队列登记，保留已登记任务，等待重新核对目录。已收录但不再留在投放目录中的文件不会因此自动获得研读资格。

若仅设备号变化，先停监听，备份基线、模型许可、收录账本和启动配置，核对仍是原目录。使用 rebind-baseline，传入 --vault、--inbox、--old-device 和 --new-device。它要求原路径、inode 和预期设备号匹配，在同一锁下仅更新设备号。历史 SHA-256 列表照原保留。确认恢复前使用关闭模型外发的私有许可文件。不要重新运行 seed-baseline，否则等待处理的文件会被记进历史版本。

回退时可重新指向原代码版本，保留已收录产物、新增账本和用户修改。原许可保留于私有备份，是否恢复模型处理由当前授权决定。

#### 2026-11-24 验收

合成回归覆盖重启后的设备号错配、无基线、拒绝替换目录、重绑重放和本机降级后恢复。


### 只调整知识白板的呈现

在本机运行 `python -m afr_agent refresh-views --vault "/所选 Obsidian 库"`。程序直接用已有知识记录重绘知识白板和时间线，保留人工修改，并列出受保护的路径。用可重复的 `--topic <主题 ID>` 只更新所选主题及相关导航。没有知识记录时返回 `no_knowledge`。刷新阅读首页沿用 `python -m afr_agent dashboard --vault "/所选 Obsidian 库"`。两条命令均无需调用模型。


### 从旧的分支白板和独立时间线升级

先暂停监听，保留完整的库恢复点。用 `python -B scripts/merge_topic_boards.py --vault "$vault"` 查看结果。随后指定库外的新目录，用 `--apply --backup "$backup"` 先重绘，再将已有主白板承接、可按摘要核验的旧分支或时间线归档。仍被其他笔记引用或有人工修改的白板会保留并列出路径。知识历史、手写笔记和固定原文留在原位。每次执行使用独立的恢复目录，结束后恢复监听。
