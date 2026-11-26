# AFR 全库发布验收

在源码根目录用 Python 3.11 或更高版本运行。将 `--vault` 指向验收副本或正式库，将 `--baseline` 指向发布前的完整恢复点。如果副本保留指向正式库的 Obsidian 绝对链接，填写 `--vault-alias`。

```bash
release="/path/to/afr-release-check"
python3.11 -B scripts/check_vault_release.py \
  --vault "$release/验收副本/研究知识库" \
  --baseline "$release/本机恢复点/原知识库" \
  --vault-alias "/path/to/active-obsidian-vault" \
  --expected-topics 2 --expected-events 5 \
  > "$release/全库上线审计.json"
```

示例中的主题数和记录数按验收样本填写，也可省略计数参数。脚本只读。它逐一核对知识账本的主题索引、事件在时间线上的具体标题和论述白板入口，并核对论述的标题、回到主白板的链接与固定原文锚点。文章白板按已有论点卡核对篇内论述、返回主板及完整阅读锚点。如果提供迁移前副本，还会逐卡比对标题和阅读段是否与旧入口一致。

链接扫描遍历可见的 Markdown、Canvas 和 Base，解析 Obsidian 内部链接、相对路径、库内完整路径和 Obsidian 打开链接，检查 Markdown 标题、块 ID、Canvas 节点及 PDF 页码片段格式。它不读取 PDF 页数，也不作 Obsidian 实点验收。旧入口检查同时覆盖已退役文件与 Obsidian 工作区。

JSON 的 `existing_link_issues` 记录在同一文件、同一链接上沿用的缺链，`new_link_issues` 记录此次才出现的缺链，`resolved_link_issues` 记录仍在库内且缺链已消失的历史页面。未提供基线时，当前缺链均计入 `new_link_issues`。有结构检查失败或新的缺链时退出码为 1。
