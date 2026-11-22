# 巡测成果数据契约

## 路线与覆盖

- `sections` 是有序路线区段表，`section` 唯一，`length_mm` 是正整数，`direction` 为 `forward` 或 `reverse`。全局坐标从 0 开始，偏移取前序区段长度和。
- `passes` 的每行用唯一 `pass` 标识，指定区段、局部半开范围 `[from_mm,to_mm)` 和 `usable` 或 `occluded`。反向段长度为 L 时 `[a,b)` 投影为 `[offset+L-b,offset+L-a)`。
- 以区段边界和观测端点切分全路线。在相同边界先移除结束的观测，再加入起始观测。任一可用来源得到 `covered`，只有遮挡来源得到 `occluded`，无来源得到 `unobserved`。每个原子区间保留激活的可用及遮挡来源和 1 起算的数组序号。不同证据的相邻区间不折叠。
- 按 `(route,edition)` 在 SQLite 一起保存来源、摘要和事实；同版本同摘要幂等，同版本不同摘要拒绝。发布下一版不修改旧版。当前范围限单路线和连续区段，不做地理坐标推算。

## 开班计划

`plan-shift` 在开班时冻结一份计划，`replan-shift` 只在此基础上追加版本。

- 通知可以带 `at_min` 到达时间，也可以 `requires` 另一条通知。未到达的通知不能用于决策，依赖它未到达的前序通知也不能被当作已知。
- `policies` 是固定规则：`early`（开班即签、不依赖后续通知的作业）、`reserve_minutes`（在 `release_at_min` 前保留的班次分钟）、`order`/`priority`（固定排序）、`drop`（不参与）。同一开盘信息只对应同一组开盘作业，不会按事后结果倒选。`scenarios` 只用于评估每种选择在各情况下的补里程与留下的缺口，不参与组合作业。
- 若某选择在所有可能情况下都不比另一选择好，且至少一种情况更差，则不作为可选方案返回（`dominated`）。
- 评估指标是：消除了多少整段缺口（`added_mm`）、新增多少可观测里程（`usable_added_mm`）、班终还剩多少缺口（`gap_mm`）。

重新计划时：

- 只能改未开始且未锁定的作业；`spent_minutes` 只增不减，已耗分钟不返还。
- 可以锁定（`locked`）或放弃（`drop`）未开始的作业；对已开始/已锁定作业放弃会拒绝。
- 已签但未开始的作业保留原签订时间并占用容量；对从未签订的作业报已耗分钟或锁定会被拒绝。

## 日常发布

`publish-daily` 发布班终成果，供下一班开班使用。

- 每条回执必须对应一项已签作业，且落在该作业范围内；范围外部分不参与本次补足。交回其他人的已签作业、补交同一回执、或超出已签范围，都会列入 `receipts.excluded`。
- 独立车组按 `(区段, 车组)` 统计：同一车组反向重跑、只完成半段、同一回执补交都不能增加独立车组数。
- 每段分别给出 `observed_spans`（已有观测）和 `independent_ok`/`required_groups`（独立证据是否达到要求）。`retest_spans` 是仍需整段复测的范围：全段有观测但独立车组不足时仍然存在。
- `prior` 可以是上一班已发布的日常成果，也可以是导入的历史 `passes`。若为本库已发布成果，事实摘要必须一致，被手工改过的副本会被拒绝。

## 移交下一班

下一班用 `baseline` 取缺口（`--daily` 发布结果或历史）。`independent_ok` 由发布事实中的 `crew_groups` 重新推算，不直接沿用旧标记，因此提高要求后，原有单一车组的可用观测只会算"已观测"，不会算"已复测满足"。

原版与本班实测共用同一套局部毫米口径，可按区段核回计划版本（`read-shift --version`）与原始回执。

`railchain/reference.py` 管理线性坐标，`railchain/slicing.py` 归约覆盖事实，`railchain/evidence.py` 落实独立证据，`railchain/plan.py` 做开班与非预测评估，`railchain/daily.py` 发布日常成果，`railchain/store.py` 统一 SQLite 存储。
