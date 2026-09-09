# Silo（智仓）

Silo 将一个抖音博主主页整理成可交付、可持续增量更新的完整语料资产：

1. 全量或增量读取主页作品；
2. 每条视频优先读取官方字幕，否则调用私有 GPU ASR 服务；
3. 同时保存忠实原始稿与轻度整理稿；
4. 基于截至本次运行的全部完整逐字稿重建分析报告和 `SKILL.md`；
5. 生成带截止日期、异常清单和索引的独立 ZIP 交付包。

采集是增量的，交付始终是全量的。每个版本都通过 `manifest.json` 和
`MANIFEST.md` 说明内容截止时间、作品数量、本次新增数量、字幕完成数量及异常。

## 已实现

- FastAPI Web 控制台和后台任务；
- 抖音短链/完整主页解析、博主资料和主页分页；
- API 分页采用批次限速、游标冷却重试、78 个月份定位补漏和浏览器回补；平台数量明显不符时保存工作语料，但拒绝伪造“完整版本”；
- SQLite WAL 数据库、视频 ID 幂等去重、运行记录和事件日志；
- 官方字幕结构探测、视频下载、私有 FunASR GPU 服务降级；
- 单次收割任务默认并行处理 4 条视频，ASR 服务端由 8 个独立 GPU 模型进程承接；
- 每条视频独立 Markdown，原始稿与阅读稿双份保留；
- 单篇分析缓存：增量更新只分析新稿，最终报告和 Skill 基于全量重新收敛；
- 版本化 CSV 索引、报告、Skill、manifest 和 ZIP；
- 博主专属资产空间：集中查看主页资料、收割历史、当前工作语料和所有截止日期版本；
- Web 逐字稿档案库：按状态和正文搜索，查看原始稿/整理稿，单篇或整库下载；
- 采集更新与重新蒸馏解耦：可只用现有全量语料重建报告和 Skill；
- 失败任务可继续处理，复用已经完成的逐字稿；
- Mac mini LaunchAgent 常驻及开机登录后自动启动。

## 部署运行

推荐目录结构如下：

```text
~/Silo/       应用代码和虚拟环境
~/SiloData/   数据库、Cookie、模型、语料、版本包和日志（亦可使用项目根目录下 ./data）
```

部署代码后执行（macOS / 边缘服务器）：

```bash
cd ~/Silo
./scripts/install_macos.sh
```

安装脚本会准备 Python 3.12、Python 依赖、Playwright 和 LaunchAgent 常驻服务。Silo 默认监听 `0.0.0.0:8001`；ASR 与 LLM 端点在配置文件中指定。

如果旧版 `douyin-homepage-stealer` 已保存近期 Cookie，可以迁移：

```bash
cd ~/Silo
set -a; source .env; set +a
.venv/bin/python scripts/import_legacy_cookie.py /path/to/douyin-homepage-stealer/config.ini
```

Cookie 失效时也可以在 Web 首页点击“更新抖音登录 Cookie”。Cookie 文件权限为 `0600`，API 不会返回 Cookie 内容。

## 访问地址

服务默认 `SILO_HOST=0.0.0.0`，同时监听所有可用网卡。根据部署和网络环境选择访问地址：

- 本机访问：`http://127.0.0.1:8001`
- 局域网访问：`http://<lan-ip>:8001`（或 `http://<hostname>.local:8001`）
- 私有组网访问（如 Tailscale）：`http://<tailscale-ip>:8001`

建议在路由器中为常开边缘主机配置 DHCP 静态租约（固定内网 IP）。


## 本地开发与测试

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check app tests
./run.sh
```

环境变量见 [`.env.example`](.env.example)。默认大模型端点为
`http://127.0.0.1:18080/v1`，模型为 `qwen3.8-27b-q8_0`。

## API


| 方法     | 路径                                      | 说明                    |
| ------ | --------------------------------------- | --------------------- |
| `POST` | `/api/runs`                             | 提交主页，开始一次完整版本生产       |
| `GET`  | `/api/runs/{run_id}`                    | 查询分阶段进度和事件            |
| `GET`  | `/api/creators`                         | 博主资产列表                |
| `GET`  | `/api/creators/{creator_id}`            | 博主专属空间、收割历史和版本        |
| `POST` | `/api/creators/{creator_id}/refresh`    | 增量采集并重建全量版本           |
| `POST` | `/api/creators/{creator_id}/redistill`  | 不采集，基于现有语料重建报告和 Skill |
| `GET`  | `/api/creators/{creator_id}/videos`     | 搜索和分页查看逐字稿            |
| `GET`  | `/api/creators/{creator_id}/corpus.zip` | 下载当前已完成的工作语料          |
| `GET`  | `/api/creators/{creator_id}/versions`   | 查询历史语料版本              |
| `GET`  | `/api/versions/{version_id}/download`   | 下载完整 ZIP 交付包          |
| `GET`  | `/api/versions/{version_id}/report`     | 获取分析报告                |
| `GET`  | `/api/versions/{version_id}/skill`      | 获取 `SKILL.md`         |
| `PUT`  | `/api/settings/douyin-cookie`           | 更新登录 Cookie           |
| `GET`  | `/api/diagnostics`                      | 检查采集、ASR 和模型状态        |


## 完整性的边界

“完整语料”定义为：截止时间前，主页接口能够发现的所有公开作品都有记录；所有
成功条目都有完整逐字稿，无法访问、下载或识别的作品进入明确的异常清单。系统不会
把失败条目静默当成已完成。

请仅处理你有权保存、分析和交付的公开内容，并遵守平台条款和适用法律。