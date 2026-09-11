# 云端无人值守部署清单（一次性，约 15 分钟）

目标：**GitHub Actions 每天 09:00（北京时间）自动采集 → 生成日报 → 发布到 GitHub Pages**。
全程免费，之后**不需要开电脑**。

最终网址形如：`https://<你的用户名>.github.io/ai-news/`

---

## 第 1 步：注册智谱，拿免费 API Key（5 分钟）

> **注意：GLM-4-Flash 不需要"领取"**。它不是限时活动，而是常驻的永久免费模型——控制台里没有"立即领取"按钮，只要实名认证通过，直接调用就是 0 费用。找不到入口不是你的问题。

1. 打开 https://open.bigmodel.cn → 手机号登录（已有账号直接用）
2. 右上角头像 →「实名认证」→ 个人认证（必须做，否则 API 不能用）
3. 右上角头像 → **「API Keys」** → 新建 API Key → 复制（形如 `xxxxxxxx.xxxxxxxx`）
4. 想亲眼确认免费：打开 https://bigmodel.cn/console/trialcenter?modelCode=glm-4-flash ，该模型标价为 **0 元 / 永久免费**，限流约 3 次/秒（每日采集任务只用 1 次，完全够）

**自助验证 Key 是否可用**（电脑 cmd 里运行，把 Key 换成你的）：

> ⚠️ Windows cmd 默认 GBK 编码，**测试命令里千万别带中文**，否则会报 `Invalid UTF-8 start byte 0xbb`（GBK 字节被当 UTF-8 解析）。测试内容一律用纯英文：

```bash
curl https://open.bigmodel.cn/api/paas/v4/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer 你的Key" -d "{\"model\":\"glm-4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"reply ok\"}]}"
```

返回 JSON 里有 `"content"` 字段就说明 Key 和免费模型都通了。

## 第 2 步：创建 GitHub 仓库并推送代码（二选一）

### 方式 A：给我一个 Fine-grained Token，我来推（推荐）

1. 登录 GitHub → 右上角头像 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. Repository access 选 **All repositories**（或先建好仓库再只选它）
3. Permissions → Repository permissions：
   - **Contents: Read and write**
   - **Pages: Read and write**
   - **Workflows: Read and write**
4. 生成后把 **token + 仓库名** 发给我，我完成推送并跑通第一次

> 需要先在 GitHub 上建一个**空仓库**（如 `ai-news`，Public，不要勾选任何初始化文件）。

### 方式 B：自己推（5 条命令）

```bash
cd cloud
git init -b main
git add -A
git commit -m "init: AI daily news site"
git remote add origin https://github.com/<你的用户名>/ai-news.git
git push -u origin main
```

## 第 3 步：配置 Secrets（放 API Key，加密存储）

仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**

- Name：`ZHIPU_API_KEY`
- Secret：粘贴第 1 步的 Key

> ⚠️ **保存后再打开这个页面，Value 框会是空的 —— 这是正常的**。GitHub Secrets 只写不读，任何人在网页上都看不到已存的值（连仓库管理员也不行）。判断有没有配成功，看 Actions 日志或 `site/data/run-log.json` 里的 `mode` 字段：`llm` = Key 生效，`rule` = 没读到 Key。
> 🔁 顺带一提：Key 一旦出现在截图/聊天里，建议去智谱控制台**重建一个** Key 并更新此 Secret。

## 第 4 步：开启 GitHub Pages（必做，否则发布 Job 会失败）

仓库 → **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**

## 第 5 步：验证

1. 仓库 → **Actions** → 左侧选「AI 变现日报」→ 右侧 **Run workflow** 手动触发一次
2. 等待 2–5 分钟，两个 Job 全绿即成功
3. 打开 `https://<你的用户名>.github.io/ai-news/` 应能看到日报
4. 之后每天北京时间 09:00 自动运行（GitHub 定时可能有 5–30 分钟延迟，属正常）

---

## 运行机制与降级策略

| 情况 | 行为 |
|---|---|
| API Key 正常 | GLM-4-Flash 从抓取的候选中挑选、分类、生成中文摘要与新手三指标（难度/金额/周期） |
| Key 缺失 / 调用失败 | **规则分类兜底**：按关键词分到 money/free，摘要取原文截断，**不编造任何内容** |
| 当天一个候选都没有 | 跳过当天，保留上一期，不生成空日报 |

## 双轨说明（与本机 WorkBuddy 版的关系）

- **云端版（本仓库）**：保证"每天必有"，RSS/HN/Reddit 聚合 + AI 筛选，覆盖面广但拆解深度一般
- **WorkBuddy 版（本机）**：电脑开着时 9 点跑的深度版（联网搜索 + 拆解），质量更高
- 两者数据结构完全一致。哪边后写入当天日期，哪边就是当天内容；页面上看的是各自部署渠道的最新数据

## 加信息源

编辑 `scripts/collect.py` 顶部的 `SOURCES` 列表，`type` 支持：
- `rss`：任何 RSS/Atom 地址（B站 UP 主可用 RSSHub 实例：`https://rsshub.app/bilibili/user/video/<UID>`）
- `hn`：Hacker News 关键词检索（改 `HN_QUERIES` 列表即可加关键词）
