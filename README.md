# 城市地下管网安全监测与应急调度服务

本项目为城市供水、排水和燃气管网提供离线后台服务，保存管段、传感器读数、泄漏告警、巡检工单、维修审批和应急资源分配。系统使用确定性的风险评分帮助值班人员优先处理高风险管段，账号按角色授予读取、处置和审批权限，状态变化写入 SQLite 审计表。

资产管理部门可配置带生效时间的健康指数评分规则（腐蚀年限、历史维修、压力波动、关键设施等级四类加权因子），生成年度更新计划报告。报告生成时冻结规则版本、输入数据指纹与各因子贡献，之后补录读数或发布新规则均不会改写旧报告；样本不足的管段会明确标记不确定性且不参与排名。

## 目录

- `src/urban_network/`：管网领域服务、风险计算、健康指数评分、权限、SQLite 存储和 JSON API；
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

## 管段健康指数

- `POST /health-rules` 创建评分规则草稿（权重、归一化参数、置信阈值和 `effective_from` 生效时间），`POST /health-rules/{version}/publish` 发布后规则不可变，同一生效时间后发布者优先；
- `POST /health-reports` 按报告时刻已生效的最新规则生成健康指数报告，冻结规则版本、规则指纹、输入数据指纹和每个管段的四项因子贡献；
- `GET /health-reports/{id}` 查询报告条目，支持 `district`、`risk_min`、`risk_max`、`uncertain` 过滤，样本充足的条目按风险降序排名，不确定条目 `rank` 为空；
- `GET /health-reports/{id}/entries/{segment_id}` 沿报告追溯该条目实际使用的传感读数与已完成维修工单。

健康指数取值 0–100，越高越健康；风险分 = 100 - 健康指数，分为 low/medium/high/critical 四档。管段登记时可携带 `installed_year`（投运年份）与 `material`（材质）用于腐蚀年限因子。
