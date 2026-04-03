你现在接手的是一个已经被做过一轮高强度整理的 Bluesky collector repo。你不要从零乱改，而是把已经做对的东西保留下来，然后把剩下最后一轮结构化收口做完。

你必须把自己当成这个 repo 的第二轮重构者，而不是“写几个补丁的人”。请直接动手，不要先问一堆澄清问题。除非遇到真正无法解析的代码冲突，否则请自己往前推进。

====================
一、项目目标
====================

这个 repo 的目标不是普通爬虫，而是服务两条研究问题：

RQ1：在监控到的 Bluesky / custom feed top-K panel 中，控制 timing 与 panel context 后，语义相同或近重复的帖子是否得到系统性不同的 observed exposure opportunity？如果有，哪些可观测的 post / author / feed / viewer factor 与残余差异相关？

RQ2：在特定政治话题或事件 cluster 中，监控到的 top-K panel 是否按 observed frame supply 分配 exposure？如果没有，剩余的 frame disparity 有多少，会如何随 feed type、viewer mode、moderation environment 变化？

请把 repo 的工程设计始终围绕这两个问题来理解。

====================
二、你接手时，已经完成了什么
====================

上一轮重构（2026-04-02）已经做了下面这些实质性改动。你不要把这些改坏。

1. 新增了历史 CSV -> registry 的入口：
   - `bsky_collector_v2/jobs/seed_post_registry.py`
   - CLI: `python -m bsky_collector_v2 seed-post-registry`
   - 作用：扫描 `hourly/`、`wide/`、`micro5/` 和 `posts_first_seen`，把旧 post 系统性 seed 进 `control/control_state.db`
   - 价值：旧 post 不再只是躺在历史 CSV 里，而是可以继续被 interaction backfill 和 RQ1 factor backfill 处理

2. 新增了 public-only omnibus collector：
   - `bsky_collector_v2/jobs/public_omnibus.py`
   - CLI: `python -m bsky_collector_v2 collect-public-omnibus`
   - wrapper: `scripts/collector_public_omnivore_daemon.sh`
   - 作用：把 discovery、panel、snapshot、wide、hydrate、micro study、backfill 全部纳入一个 public-only 统一入口
   - 注意：这个模式强制 `viewer_modes=unauth`，是 public-only，不等于 auth+unauth 的研究主线

3. RQ1 factor backfill 已经被状态化：
   - `bsky_collector_v2/state.py`
   - `bsky_collector_v2/jobs/backfill_rq1_factors.py`
   - 新增 `post_rq1_factor_registry`
   - 现在 `backfill-rq1-factors` 可以像 `backfill-interactions` 一样知道哪些 post 已经 hydration 过，默认避免重复跑

4. `micro-snapshot-study` 现在支持 public-only：
   - `--public-only`
   - 可以让 public collector 在 study manifest 原本带 auth 视角时，强制只跑 unauth

5. 新增 / 强化了测试：
   - `tests/test_seed_post_registry.py`
   - `tests/test_public_omnibus.py`
   - `tests/test_cli_public_collectors.py`
   - `tests/test_rq1_factor_backfill.py`

====================
三、你必须接受的 repo 事实
====================

1. `data_v2_full/` 是 canonical data root。不要重命名，不要为了好看改成 `data/`。
2. 不要删除历史 raw data。
3. 不要破坏现有输出布局：`hourly/`、`wide/`、`micro5/`、`interactions/`、`metadata/`、`effective_csv/`。
4. 如果多个作业可能同时写 `control/control_state.db`，优先使用 `state-writer` / `BSKY_STATE_WRITER_SOCKET`，不要粗暴并发写 SQLite。
5. 不要把 public-only collector 说成已经等价于 auth+unauth fixed-panel study。

====================
四、当前 repo 的两个主要运行模式
====================

A. 论文主线 / 主研究模式：10-minute fixed feed panel collection

当前 canonical wrapper：
- `scripts/collector_daemon.sh`
- `scripts/collector_study_daemon.sh`
- `scripts/collector_screen_ctl.sh`

当前 repo 文档默认认为主要 study 是：
- `micro10_full_live_20260319`
- fixed 1500-feed panel
- Top50
- 10-minute cadence
- viewer modes: `unauth` + `auth`

B. public-only omnivore 模式：

当前 canonical wrapper：
- `scripts/collector_public_omnivore_daemon.sh`

这个模式适合：
- public discovery
- hydration
- public backfill
- optional public-only micro windows

但这个模式不是当前 paper-grade 主研究路径的完整替代。

====================
五、我对 RQ1 / RQ2 的判断，你必须保持诚实
====================

请你把下面的判断写进文档和最终 handoff，不要夸大。

1. RQ1：现在已经“接近可答”，但不是完美。

为什么接近可答：
- 有 top-K appearance + rank
- 有 window / timing / feed / bucket / viewer_mode / vantage
- 有 author hydration
- 有 interaction backfill
- 有 richer RQ1 factor backfill（post / thread / quote / labeler / relationship 等）
- 有 duplicate cluster / DCED 分析脚本

为什么还不是完美：
- `same text, different image/context` 这种多模态 near-duplicate 还没有完全硬化
- public-only 模式回答不了 viewer-private / home timeline / private preference
- 历史 backfill 拿到的是当前 public state，不是历史时点真快照
- graph 仍是 graph-lite，而不是完整的 timestamped graph history

结论：
你可以把 collector 描述成“已经足以支持一版严肃的 monitored observational RQ1”，但不能描述成“已经观察到所有导致 exposure 差异的因素”。

2. RQ2：collection primitives 已经有了，但 end-to-end pipeline 还没有收口。

已有的 building blocks：
- `bsky_collector_v2/topic_probe.py`
- `bsky_collector_v2/topic_batch.py`
- `bsky_collector_v2/content_bias.py`
- `bsky_collector_v2/annotation_sampling.py`
- `bsky_collector_v2/cluster_label_apply.py`
- `bsky_collector_v2/annotation_merge.py`

问题在于：
- 还没有一个 canonical 一键流水线从 raw collection -> topic/event cluster -> frame labels -> frame exposure vs supply tables
- frame labeling 仍然部分依赖人工 annotation
- declared-objective adjustment 还没有被统一成标准化最终输出表
- viewer_mode / moderation comparisons 仍取决于采集模式是否包含 auth / accept-labelers 等条件

结论：
当前 repo 还不能诚实地说“完美回答 RQ2”。更准确的说法是：
“RQ2 的 collection base 已经有了，topic/frame pipeline 需要整合成一个 first-class workflow。”

====================
六、你现在应该完成的剩余重构
====================

你这一轮不要再大拆大建。主要做下面四件事。

【任务 1】把 fixed-panel daemon 和 realtime backfill 统一起来。

当前现实情况是：
- `collector_daemon.sh` 负责当前 10-minute fixed feed panel study
- `collector_public_omnivore_daemon.sh` 可以负责 public-only realtime backfill / omnibus collection
- 但两者还是分开的

我希望你把它收口成下面二选一的结果，任选一种，但要做干净：

方案 A：扩展 `scripts/collector_daemon.sh`
- 给它增加开关，让它在 fixed-panel study 模式下也能周期性跑：
  - `seed-post-registry`
  - `backfill-interactions`
  - `backfill-rq1-factors`
- 默认通过 `BSKY_STATE_WRITER_SOCKET` / state-writer 管理共享 state
- 把“10-minute fixed panel + realtime backfill”真正变成一个 daemon profile

方案 B：新增一个更清楚的 wrapper，比如：
- `scripts/collector_rq_daemon.sh`
- 它内部调用现有 `collector_daemon.sh` / `public_omnivore` / standalone backfill commands
- 但对操作者暴露为一个清晰入口

无论你选 A 还是 B，都必须满足：
- 可以一条命令启动当前 10-minute fixed feed panel collection
- 可以一条命令启动 realtime backfill
- 可以一条命令启动 history backfill
- 最好再给一个“全套研究栈”入口

【任务 2】把 RQ2 topic/frame 工具升格成 first-class workflow。

至少完成下面这些之一：
- 把 topic/frame 相关工具接进 `bsky_collector_v2/cli.py`
- 或新增 `scripts/run_rq2_pipeline.sh` / `scripts/run_rq2_pipeline.py`

我希望最终存在一个清楚的 RQ2 pipeline，大致包括：
- topic probing / topic batch
- event clustering
- annotation candidate sampling
- cluster label application
- annotation merge
- frame exposure vs frame supply final table generation

如果最后一环 “final table generation” 现有 repo 里没有现成实现，请你补一个最基础、可跑、可测试的版本，把 topic / frame / exposure / supply 汇总成一张干净表。

【任务 3】重写 repo root `agent.md`

我已经让 ChatGPT 写了一版新的 agent.md，但你要再次检查并根据你最终的代码状态更新它。

新的 root `agent.md` 必须告诉之后所有 agent / Codex：
- 这个 repo 的研究目标是什么
- canonical collector path 是什么
- canonical data root 是什么
- 当前 paper-grade 主线采集方式是什么
- public-only 模式是什么，不是什么
- 如何启动 10-minute fixed panel
- 如何启动 realtime backfill
- 如何启动 history backfill
- RQ1 可以回答到什么程度
- RQ2 还差什么
- 不要做什么（不要删 data，不要 rename data_v2_full，不要夸大 RQ2 完成度）

【任务 4】文档收尾

请至少补一份清楚的 handoff 文档，例如：
- `docs/handoffs/rq_collection_handoff_2026-04-02.md`

里面要写：
- 当前 collector modes
- 现在推荐怎么启动
- current known limitations
- RQ1 / RQ2 readiness assessment
- 你这轮改了什么
- 你没改什么

====================
七、当前命令：请保留兼容，并在必要时新增更好的 wrapper
====================

在你进一步整理前，当前可工作的命令应该被保留兼容。

1. 当前 10-minute fixed feed panel collection

直接启动：

```bash
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
ENV_PATH=/Volumes/T9/BlueSky/auth.env \
DEFAULT_STUDY_ID=micro10_full_live_20260319 \
STUDY_ID=micro10_full_live_20260319 \
/Volumes/T9/BlueSky/scripts/collector_daemon.sh
```

screen 管理：

```bash
cd /Volumes/T9/BlueSky
DEFAULT_STUDY_ID=micro10_full_live_20260319 \
STUDY_ID=micro10_full_live_20260319 \
./scripts/collector_screen_ctl.sh start
```

2. 当前 realtime backfill

```bash
cd /Volumes/T9/BlueSky
export BSKY_STATE_WRITER_SOCKET=/tmp/bsky_state_writer_prod.sock
ROOT=/Volumes/T9/BlueSky \
OUT_BASE=/Volumes/T9/BlueSky/data_v2_full \
SEED_REGISTRY=1 \
RUN_INDEX_FEED_GENERATORS=0 \
RUN_REFRESH_DISCOVERY=0 \
RUN_BUILD_PANEL=0 \
RUN_SNAPSHOT_PANEL=0 \
RUN_WIDE_SWEEP=0 \
RUN_HYDRATE_AUTHORS=0 \
RUN_HYDRATE_FEED_GENERATORS=0 \
RUN_MICRO_STUDIES=0 \
RUN_BACKFILL_INTERACTIONS=1 \
RUN_BACKFILL_RQ1_FACTORS=1 \
INTERVAL_PUBLIC_OMNIBUS_S=300 \
./scripts/collector_public_omnivore_daemon.sh
```

3. 当前 history backfill

```bash
cd /Volumes/T9/BlueSky
export BSKY_STATE_WRITER_SOCKET=/tmp/bsky_state_writer_prod.sock
.venv/bin/python -m bsky_collector_v2 seed-post-registry \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --include-hourly --include-wide --include-micro5 --include-posts-first-seen

.venv/bin/python -m bsky_collector_v2 backfill-interactions \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --seen-before-utc 2026-03-19T05:30:00Z \
  --max-posts 200000 \
  --batch-size 25 \
  --max-items-per-endpoint 0

.venv/bin/python -m bsky_collector_v2 backfill-rq1-factors \
  --out-base /Volumes/T9/BlueSky/data_v2_full \
  --seen-before-utc 2026-03-19T05:30:00Z \
  --max-posts 200000 \
  --batch-size 25 \
  --max-items-per-endpoint 0 \
  --max-thread-depth 1000 \
  --max-thread-parent-height 1000
```

如果你做了新的 wrapper，请让它们更简单，但别把上面的老入口弄坏。

====================
八、测试要求
====================

无论你怎么改，最后至少要跑：

```bash
pytest -q
```

如果你动了 collector / daemon / backfill 相关代码，重点跑：

```bash
pytest -q \
  tests/test_seed_post_registry.py \
  tests/test_public_omnibus.py \
  tests/test_cli_public_collectors.py \
  tests/test_rq1_factor_backfill.py \
  tests/test_micro_snapshot_study.py \
  tests/test_state.py
```

如果你新增 RQ2 final table generator，请补一组新测试。

====================
九、你的最终交付
====================

你最终必须交付：

1. 改完的 repo
2. 更新后的 root `agent.md`
3. 一份 handoff 文档，说明你这轮改了什么、为什么改、当前怎么启动
4. 如果你做了新的 unified wrapper，请给清楚命令
5. 一段诚实的 RQ1 / RQ2 readiness 说明，不要夸大

====================
十、你不该做的事
====================

- 不要删历史 data
- 不要 rename `data_v2_full`
- 不要把 public-only collector 说成已经等价于 auth+unauth 研究设计
- 不要声称 RQ2 已经 fully productionized，除非你真的把 frame pipeline 全接上了
- 不要为了“整洁”破坏兼容性

如果你读到这里，只需要开始干活，不要回复一大段计划。直接实现，跑测试，最后给我清楚的 diff、命令、以及 RQ1/RQ2 评估。
