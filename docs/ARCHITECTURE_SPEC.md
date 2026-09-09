# Silo 深度技术规格与架构实现手册 (ARCHITECTURE SPEC)

> 本文档由 **Antigravity AI 与 Codex 架构师** 联合推演制定，专注规范 **Silo 抖音博主认知与表达风格蒸馏系统** 的技术实现。

---

## 1. 物理环境与算力资源拓扑

```
[局域网/手机/PC 客户端]
       │
       │ HTTP / Web 访问
       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    边缘应用服务器 (例如 Mac mini / Linux)                │
│                                                                         │
│  • Silo-Web 控制台 (FastAPI :8001 + 响应式前端)                          │
│  • 抖音监控与采集引擎 (复用 douyin-homepage-stealer 机制, 住宅 IP 防风控)    │
│  • 官方字幕解析器；无字幕视频调用 GPU ASR API                            │
│  • 本地 SQLite 数据库 ($SILO_DATA_DIR/silo.db)                          │
│  • 结构化 Markdown 语料库 ($SILO_DATA_DIR/corpus/{creator_id}/)         │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                     内网 HTTP RPC (风格蒸馏请求)
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    GPU 推理服务器                                       │
│                                                                         │
│  • Qwen3.8-27B 模型服务 (llama-server, 监听 127.0.0.1:18080)             │
│  • FunASR Paraformer + VAD + 标点服务 (GPU 1, 127.0.0.1:8000)          │
│  • 执行 Map-Reduce 认知与风格蒸馏，产出博主画像与 SKILL.md                │
│  • (可选) GPT-SoVITS (:9880) 用于音色克隆输出播客                           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 数据库详细设计 (SQLite WAL 模式)

数据库存储位置：`$SILO_DATA_DIR/silo.db`

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- 1. 博主基本信息表
CREATE TABLE IF NOT EXISTS creators (
    creator_id TEXT PRIMARY KEY,          -- 抖音 sec_user_id
    nickname TEXT NOT NULL,               -- 抖音昵称
    short_url TEXT,                       -- 原始主页短链
    avatar_url TEXT,                      -- 头像 URL
    signature TEXT,                       -- 个人签名/简介
    total_videos INTEGER DEFAULT 0,       -- 累计抓取视频数
    last_sync_time DATETIME,              -- 最近一次增量检查时间
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 视频与文案资产表
CREATE TABLE IF NOT EXISTS creator_videos (
    aweme_id TEXT PRIMARY KEY,            -- 视频唯一 ID (唯一索引，用于增量去重)
    creator_id TEXT NOT NULL,             -- 关联博主 ID
    title TEXT,                           -- 视频标题/文案
    publish_time DATETIME,                -- 视频发布时间戳
    duration INTEGER,                     -- 视频时长 (秒)
    like_count INTEGER DEFAULT 0,         -- 点赞数 (用于权重筛选)
    comment_count INTEGER DEFAULT 0,      -- 评论数
    share_count INTEGER DEFAULT 0,        -- 分享数
    video_url TEXT,                       -- 无水印视频源链接
    has_official_subtitle BOOLEAN DEFAULT 0, -- 是否直接命中官方内置字幕
    raw_transcript TEXT,                  -- 原始逐字稿 (官方字幕或 ASR 结果)
    cleaned_transcript TEXT,              -- 清洗去燥后的排版文稿
    audio_path TEXT,                      -- 本地提取的音频相对路径 (可选)
    status TEXT DEFAULT 'PENDING',        -- PENDING | TRANSCRIBED | DISTILLED | FAILED
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id)
);
CREATE INDEX IF NOT EXISTS idx_videos_creator ON creator_videos(creator_id);
CREATE INDEX IF NOT EXISTS idx_videos_likes ON creator_videos(like_count DESC);
CREATE INDEX IF NOT EXISTS idx_videos_publish ON creator_videos(publish_time DESC);

-- 3. 认知与风格蒸馏成果表
CREATE TABLE IF NOT EXISTS creator_profiles (
    profile_id TEXT PRIMARY KEY,          -- 唯一画像 ID (如 "prof_xxx")
    creator_id TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    persona_tag TEXT,                     -- 一句话人设标签 (如 "毒舌商业架构师")
    tone_traits TEXT,                     -- 语气特征 (JSON Array)
    catchphrases TEXT,                    -- 标志性口头禅 (JSON Array)
    hook_templates TEXT,                  -- 爆款开篇钩子公式 (JSON Array)
    narrative_blueprint TEXT,             -- 叙事五步法逻辑架构 (Markdown)
    skill_markdown TEXT NOT NULL,         -- 最终标准 SKILL.md 完整内容
    report_markdown TEXT NOT NULL,        -- 完整风格分析报告
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(creator_id) REFERENCES creators(creator_id)
);
```

---

## 3. 核心流水线设计与实现细节

### 3.1 采集与增量监控模块 (Harvester)
1. **短链还原**：请求 `https://v.douyin.com/xxx/` 获取 HTTP 302 重定向后的真实 URL，正则提取 `sec_user_id`。
2. **全量首次拉取**：调用主页 post 列表接口，通过 `max_cursor` 迭代翻页，批量入库 `creator_videos`。
3. **增量巡检机制**：定时（如每 10 分钟或按需触发）请求前 1~2 页，一旦遇到已存在且发布时间小于 `last_sync_time` 的 `aweme_id`，立即触发 Early-Stopping，杜绝多余请求。

### 3.2 字幕提取与 ASR 降级流水线 (Transcript Pipeline)
```
                        [获取到视频 Aweme 元数据]
                                   │
                                   ▼
                   [检查 video.caption_info / 字幕列表]
                                   │
                   ┌───────────────┴───────────────┐
                   │                               │
            (存在官方字幕)                   (无官方字幕)
                   │                               │
                   ▼                               ▼
       [直接下载 WebVTT/JSON]                    [下载视频]
                   │                               │
       (耗时 0 秒，平台原文)               [调用私有 GPU ASR API]
                   │                               │
                   └───────────────┬───────────────┘
                                   │
                                   ▼
                    [轻量空白与标点归一化]
                                   │
                                   ▼
             [写入 SQLite + 导出 Markdown 结构化语料]
```

---

## 4. 认知与风格蒸馏引擎 (Map-Reduce 范式)

为了避免 50~100 篇视频直接拼接输入超出上下文或导致核心特征稀释，采用 **Map-Reduce 双层蒸馏**：

### 4.1 Map 阶段：逐篇微观解构 Prompt
```text
你是一个顶级短视频内容拆解专家。请深度分析以下这篇视频逐字稿：
【视频标题】：{{title}}
【点赞量】：{{like_count}}
【视频文本】：
{{transcript}}

请提取并输出严格 JSON 格式：
{
  "hook": {
    "type": "痛点反问 / 反常识否定 / 利益吸引 / 故事代入",
    "text": "前 3 秒具体文案"
  },
  "core_argument": "这篇视频传递的核心认知或观点（不超过 30 字）",
  "emotional_tone": "犀利 / 毒舌 / 激昂 / 治愈 / 严肃 / 市井",
  "catchphrases": ["提取文中的特色口头禅或标志性用词"],
  "rhetorical_devices": ["使用的标志性比喻或修辞"]
}
```

### 4.2 Reduce 阶段：宏观收敛与 SKILL.md 生成 Prompt
```text
你是一个高级 Agent 架构师与 IP 策划专家。以下是博主【{{nickname}}】点赞最高的 20 个代表作拆解数据：
{{aggregated_map_json}}

请对该博主的认知体系与表达方式进行全面收敛，严格按照以下 YAML Frontmatter + Markdown 格式生成一份标准的 SKILL.md 文件：

---
name: {{creator_slug}}-style
description: 深度模拟博主【{{nickname}}】的语气、文风、思考框架与爆款钩子进行内容创作。
---

# 1. 角色画像与世界观 (Persona)
- 人设标签与核心态度
- 坚信的底层信念 vs 极度反感的观点

# 2. 黄金 3 秒开篇钩子公式 (Hook Formulas)
- 提取 3~5 种最容易起量的开篇句式模板（含填空占位符）

# 3. 语言指纹与修辞偏好 (Linguistic Style)
- 口头禅与语气词清单
- 句式长短与节奏感
- 标志性比喻体系

# 4. 经典叙事结构五步法 (Storytelling Architecture)
- 第 1 步：炸场
- 第 2 步：揭露死穴
- 第 3 步：给硬核解法
- 第 4 步：升华认知
- 第 5 步：互动二选一

# 5. 绝对负向约束 (Negative Rules)
- 严禁出现的词汇与陈词滥调
```

---

## 5. Web 控制台与 API 规范

### 5.1 REST API 接口定义 (FastAPI: `:8001`)

| 接口 | 方法 | 说明 | 入参 / 返回 |
| :--- | :---: | :--- | :--- |
| `/api/creator/submit` | `POST` | 提交博主主页链接，开始抓取与转录 | `{"url": "https://v.douyin.com/xxx/"}` $\to$ `{"creator_id": "...", "status": "PROCESSING"}` |
| `/api/creator/{creator_id}/status` | `GET` | 查询抓取、转写进度 | 返回总视频数、已转写数、点赞最高文案摘要 |
| `/api/creator/{creator_id}/distill` | `POST` | 触发 Qwen3.8-27B 风格蒸馏 | 异步任务，返回 `task_id` |
| `/api/creator/{creator_id}/skill` | `GET` | 获取生成的 `SKILL.md` 源码 | 返回 Markdown 文本 |
| `/api/creator/{creator_id}/export` | `GET` | 打包下载该博主全量语料包 (ZIP) | 包含所有视频 Markdown + `SKILL.md` |

### 5.2 极简 Web 控制台功能
* **页面 1（首页/提交页）**：大输入框，粘贴抖音链接，点击“一键收割并蒸馏”，显示实时处理流与进度条。
* **页面 2（博主画像库）**：卡片式展示已蒸馏的所有博主，可点击查看《风格拆解报告》、《SKILL.md》，并提供在线“仿写测试”对话框（直接以该博主口吻生成一段新话题文案）。

---

## 6. 已实现版本的语料版本原则

本节覆盖前文中“定时监控”和“Top 20 代表作”的早期假设，以当前实现为准：

1. 首期采用人工发起的一次性全量收割，不运行分钟级定时巡检；
2. 后续更新仍由用户手动发起，系统用 `aweme_id` 识别新增作品；
3. 采集与逐篇分析采用增量方式，已经完成且文本未变化的内容会复用；
4. 每次正式输出都是一个独立的全量语料版本；
5. 报告与 `SKILL.md` 每次都基于该截止日期下的全部成功逐字稿重新收敛；
6. 每个版本同时记录 `content_cutoff_at` 和 `generated_at`；
7. 无法访问或转写失败的作品必须进入异常清单，不能静默遗漏。

新增核心表：

- `harvest_runs`：一次运行及其截止日期、阶段、计数和最终状态；
- `run_events`：可追溯的分阶段处理日志；
- `video_analyses`：按视频、逐字稿哈希和模型缓存 Map 分析；
- `corpus_versions`：可独立销售和下载的全量语料快照；
- `creator_profiles.corpus_version_id`：报告和 Skill 与语料版本一一对应。

## 7. 完整语料交付结构

```text
{version_label}_{run_id}/
├── corpus/
│   └── {publish_date}_{aweme_id}.md  # 每条视频独立完整逐字稿
├── corpus_index.csv                  # 全量索引，可直接用 Excel 打开
├── creator_profile.md                # 基于本版全量语料生成
├── SKILL.md                          # 基于本版全量语料生成
├── manifest.json                     # 机器可读的版本和完整性说明
└── MANIFEST.md                       # 面向交付对象的版本说明
```

每个视频 Markdown 同时保留“完整原始逐字稿”和“阅读整理稿”。风格分析优先使用
原始逐字稿，避免在清洗阶段删除口头禅、重复和节奏等语言指纹。

## 8. 生产部署参考
 
- 应用：`~/Silo`；
- 数据：`~/SiloData`（或本地 `./data`）；
- 服务：macOS LaunchAgent 或 systemd 常驻进程，监听 `0.0.0.0:8001`；
- ASR：GPU 上的 FunASR `Paraformer-large` + FSMN-VAD + 标点恢复；
- LLM：OpenAI 兼容端点（如 llama-server, vLLM, Ollama）；
- 远程访问：通过安全私有内网或 Tailscale 网络访问。

