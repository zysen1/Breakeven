# Go-live dossier / 上线资格备忘

Synthetic Acme claims. Every figure below is from the command in the footer.
合成的 Acme 理赔。下面每个数字都来自文末那条命令。

## English

Target reliability **95.0%**, one-sided Clopper–Pearson confidence **95.0%**, assumed human-review accuracy **100.0%**. Calibration 1200 / holdout 800, stratified by category, seed 7.

| Agent | Accuracy | Verdict | Frozen policy | Holdout lower bound | Human touch | Hours vs all-human | Breakeven volume |
|---|---:|---|---|---:|---:|---:|---:|
| Careful | 82.0% | Go | Confidence threshold τ≤0.1818 | 99.2% | 17.9% | -63.1% | 119 |
| Health-blind | 82.0% | Go | Two-key health τ≤0.4936 | 99.4% | 26.6% | -33.6% | 213 |
| Brash | 88.0% | Reliability only | Sample audit 100% | 99.6% | 100.0% | 4.0% | n/a |
| Scattered | 82.0% | Reliability only | Sample audit 100% | 99.6% | 100.0% | 4.0% | n/a |

### The 90% plan

On Health-blind, the lowest-confidence plan automates **90.5%** of holdout cases and projects **492.4** human hours against an all-human **400.0** (23.1% ). Holdout lower bound **85.0%**. The policy actually frozen for Health-blind is **Two-key health τ≤0.4936** (go), at **26.6%** human touch and **265.6** hours (ratio 0.664).

### Same accuracy, opposite recommendation

Careful and Scattered are both at accuracy **82.0%** (equal: True). Careful is **go** (hours ratio 0.369). Scattered is **reliability_only** (hours ratio 1.040). Brash is more accurate (**88.0%**) and is **reliability_only** (hours ratio 1.040).

### Assumptions that the verdict depends on

- All-human time 0.20 h/case, review 0.25 h, exception 0.50 h, dual review 0.50 h, audit 0.20 h, correction of a shipped error 1.60 h, maintenance 16.0 h per period, loaded rate USD 55/h, model cost 0.00 per case.
- Reviewers are assumed perfect unless the human-accuracy sensitivity says otherwise. `a_h min` on each agent is the weakest constant review accuracy that still clears the target under the conditional bound.
- High-risk categories in the two-key menu (health, liability, and the amount cutoffs) are customer inputs. They were not chosen by looking at the holdout set.
- Cases are treated as iid. A book clustered by claimant or clinic needs a cluster-level bound this build does not compute.
- This is not an industry-average ROI. Change the cost sheet and the verdict can move. No live language model was called.

## 中文

可靠性目标 **95.0%**，单侧 Clopper–Pearson 置信水平 **95.0%**，人工复核准确率假定 **100.0%**。调参集 1200 条，留出集 800 条，按类别分层，种子 7。

| 代理 | 准确率 | 结论 | 冻结的策略 | 留出集下界 | 人工介入 | 相对全人工工时 | 盈亏平衡件数 |
|---|---:|---|---|---:|---:|---:|---:|
| 稳妥 | 82.0% | 可上线 | Confidence threshold τ≤0.1818 | 99.2% | 17.9% | -63.1% | 119 |
| 健康险盲区 | 82.0% | 可上线 | Two-key health τ≤0.4936 | 99.4% | 26.6% | -33.6% | 213 |
| 冒进 | 88.0% | 只够可靠 | Sample audit 100% | 99.6% | 100.0% | 4.0% | n/a |
| 散点 | 82.0% | 只够可靠 | Sample audit 100% | 99.6% | 100.0% | 4.0% | n/a |

### 自动化约九成的方案

健康险盲区这个代理，按置信度最低的一档转人工，留出集上的自动化率是 **90.5%**，按全月件数投影的人工工时是 **492.4** 小时，全人工基线是 **400.0** 小时（相差 23.1%）。留出集可靠性下界 **85.0%**。真正被冻结的策略是 **Two-key health τ≤0.4936**（go），人工介入 **26.6%**，工时 **265.6** 小时（比值 0.664）。

### 准确率相同，结论相反

稳妥和散点的准确率都是 **82.0%**（是否相同：True）。稳妥的结论是 **go**（工时比值 0.369）。散点的结论是 **reliability_only**（工时比值 1.040）。冒进的准确率更高（**88.0%**），结论是 **reliability_only**（工时比值 1.040）。

### 结论依赖的假设

- 全人工 0.20 小时/件，复核 0.25，异常 0.50，双人复核 0.50，抽检 0.20，漏检纠错 1.60，每期维护 16.0 小时，全成本时薪 55 USD，模型 0.00/件。
- 默认把复核人员看成不会出错。每位代理上的 a_h min 是条件界仍能达到目标时，复核准确率可以低到什么程度。
- 双人复核菜单里的高风险类别（健康险、责任险，以及金额阈值）由客户事先给定，不是在留出集上搜出来的。
- 案例按独立同分布处理。如果案件按被保险人或机构成簇，需要簇水平的区间，这一版没有做。
- 这不是行业平均 ROI。成本表一改，结论就可能改。没有调用在线大模型。

---

Reproduced with `breakeven demo --cases 2000 --seed 7 --target 0.95 --reps 1000 --boot 400`.

