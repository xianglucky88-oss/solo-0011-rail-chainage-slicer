# 巡测成果数据契约

- `sections` 是有序路线区段表，`section` 唯一，`length_mm` 是正整数，`direction` 为 `forward` 或 `reverse`。全局坐标从 0 开始，偏移取前序区段长度和。
- `passes` 的每行用唯一 `pass` 标识，指定区段、局部半开范围 `[from_mm,to_mm)` 和 `usable` 或 `occluded`。反向段长度为 L 时 `[a,b)` 投影为 `[offset+L-b,offset+L-a)`。
- 以区段边界和观测端点切分全路线。在相同边界先移除结束的观测，再加入起始观测。任一可用来源得到 `covered`，只有遮挡来源得到 `occluded`，无来源得到 `unobserved`。每个原子区间保留激活的可用及遮挡来源和 1 起算的数组序号。不同证据的相邻区间不折叠。
- 按 `(route,edition)` 在 SQLite 一起保存来源、摘要和事实；同版本同摘要幂等，同版本不同摘要拒绝。发布下一版不修改旧版。当前范围限单路线和连续区段，不做地理坐标推算。

`railchain/reference.py` 管理线性坐标，`railchain/slicing.py` 归约覆盖事实，`railchain/publication.py` 冻结并复读成果。
