# 铁路巡测线性参考切片

**数据工程 · 数据分析与数据应用。** 将各区段的局部巡测读数投影到统一路线里程，产出可复核的覆盖区间数据集；在此基础上支持开班计划、报告到达后的安全改派，以及班终日常巡测成果发布。它处理离线巡测资料，不调度列车。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m railchain --db demo.sqlite publish examples/alignment.json
python3 -B -m railchain --db demo.sqlite read quarry-branch alignment-blue
python3 -B -m railchain --db demo.sqlite plan-shift examples/shift.json
python3 -B -m railchain --db demo.sqlite read-shift quarry-branch 2026-11-23-day
python3 -B -m railchain --db demo.sqlite publish-daily examples/daily.json
python3 -B -m railchain --db demo.sqlite read-daily quarry-branch 2026-11-23-day
python3 -B examples/smoke.py
python3 -B examples/shift_smoke.py
```

`examples/smoke.py` 覆盖正反投影、空白与遮挡分段。`examples/shift_smoke.py` 覆盖开班计划下的非预测性、可比方案筛选、报告到达后的安全改派，以及独立车组与整段复测的日常发布。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
