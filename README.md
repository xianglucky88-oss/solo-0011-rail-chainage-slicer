# 铁路巡测线性参考切片

**数据工程 · 数据分析与数据应用。** 将各区段的局部巡测读数投影到统一路线里程，产出可复核的覆盖区间数据集。它处理离线巡测资料，不调度列车。

在仓库根目录以 Python 3.9+ 运行：

```sh
python3 -B -m railchain --db demo.sqlite publish examples/alignment.json
python3 -B -m railchain --db demo.sqlite read quarry-branch alignment-blue
python3 -B examples/smoke.py
```

样例产出 6 段，包含反向投影、未观测段和遮挡重叠段，并保留组成每段的来源序号。详见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
