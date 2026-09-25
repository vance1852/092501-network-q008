# 城市地下管网安全监测与应急调度服务

本项目为城市供水、排水和燃气管网提供离线后台服务，保存管段、传感器读数、泄漏告警、巡检工单、维修审批和应急资源分配。系统使用确定性的风险评分帮助值班人员优先处理高风险管段，账号按角色授予读取、处置和审批权限，状态变化写入 SQLite 审计表。

## 目录

- `src/urban_network/`：管网领域服务、风险计算、权限、SQLite 存储和 JSON API；
- `src/power_dispatch/`：应急泵站资源分配使用的计划与容量计算组件；
- `src/plant_science/`：传感器校准与统计分析组件；
- `tests/`：领域规则、存储事务和 API 测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m urban_network.acceptance --workspace .
```

验收命令会创建演示管段、导入传感器读数、计算泄漏风险、生成巡检工单并输出 JSON。它不访问外部网络，也不要求常驻的数据库、队列或其他服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m urban_network.api --database network.sqlite3 --host 127.0.0.1 --port 8080
```

`GET /health` 返回服务状态，其余接口使用 JSON 和 `Authorization: Bearer <token>` 会话，支持管段登记、读数上报、风险查询、工单创建和应急资源分配。

## 管段健康指数与年度报告

健康指数不只参考最近一次泄漏告警，而是按**可配置、带生效时间的评分规则**综合四个因子：腐蚀年限（安装年份与材质速率）、历史维修（时间窗内已完成工单）、压力波动（读数总体标准差）和关键设施等级。

### 评分规则

- 规则按 `rule_id` + 递增 `version` 保存，`effective_at` 决定何时生效；已发布版本不可修改，调整权重只能发布新版本（需要 `approve` 权限）。
- 可配置项包括各因子权重（缺失因子的权重在可用因子间重新归一化）、腐蚀/压力/维修满量程、维修回看天数、风险区间边界和最少读数阈值。系统在 `bootstrap` 时内置 `baseline-health` v1。
- `POST /health-rules`：发布新版本；`GET /health-rules`：列出规则与版本；`GET /health-rules/{id}?version=n`：查看指定版本。

### 报告冻结

`POST /health-reports`（可带 `district`、`rule_id`、`as_of`）生成报告时冻结：

- 规则版本与规则指纹（`rule_fingerprint`，规则 id/版本/生效时间/配置的 SHA-256）；
- 输入数据指纹（`input_fingerprint`，规范 JSON 序列化后所有管段、读数、工单快照的 SHA-256）及完整输入快照；
- 每个管段各因子的归一化值、有效权重与得分贡献。

之后补录读数或发布新规则**不会改变任何旧报告**；新规则只影响之后生成的报告。`GET /health-reports` 列报告，`GET /health-reports/{id}` 支持 `district`、`risk_band`、`min_score`、`max_score` 筛选。

### 追溯与不确定性

- `GET /health-reports/{id}/segments/{segment_id}/trace` 从冻结快照返回该管段当时使用的全部读数与工单，并校验指纹一致。
- 读数少于阈值（默认 5 条）或缺少安装年份时，管段标记 `uncertain: true`、`confidence: "low"` 并给出原因，`rank_position` 为空——样本不足的管段不参与看似精确的排名，只在确定排名之后单独分组展示。

