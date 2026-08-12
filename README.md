# 可信岗位图谱 Agent

> 本仓库只包含数据处理逻辑、算法规则和前端展示源码，不包含任何招聘原始数据、处理结果、模型权重或本机凭据。

## 获取源码后先配置

1. 创建 Python 3.10 或更高版本的虚拟环境，并运行 `pip install -r requirements.txt`。
2. 将 `config/neo4j_connection.example.json` 复制为 `config/neo4j_connection.json`，填写本机 Neo4j 连接信息。
3. 如需导入原始 JD，将 `raw_jd_layer/config.example.json` 复制为 `raw_jd_layer/config.json`，填写仅存在于本机的数据目录。
4. 如需双平台增量导入，将 `two_platform_import.example.json` 复制为 `two_platform_import.json`，填写本机数据路径。

这些本地配置和运行产物已由 `.gitignore` 排除，不会进入 Git 提交。

这个文件夹只保留正式 V5、重建基础数据、程序代码和必要运行环境。

## 两个平台新数据一键入图

复制 `two_platform_import.example.json` 为 `two_platform_import.json`，填写两个
平台名称和数据文件/目录路径，然后双击 `一键导入两个平台.cmd`。程序会自动完成
字段预检、增量去重导入、能力处理、IT 准入、岗位映射、技能归一化、验证和最终
发布；失败可直接重跑。完整说明见 `INCREMENTAL_RUN.md`。

只有原始 JD、尚无五维能力列时，先运行 `extract_five_dimension_abilities.py`。
它会生成现有流水线直接识别的“能力提取结果”字段，并严格按 JD 原文回标。

## 新岗位发现

新岗位发现已经从 Agent 主代码中拆分到独立目录：

`new_role_discovery/`

双击 `new_role_discovery/start_workbench.cmd` 即可启动。算法、文件职责、
Qwen 配置和测试方法见 `new_role_discovery/README.md` 与
`new_role_discovery/FILES.md`。

## 信息技术领域准入

原始招聘文件中的搜索结果不能直接视为信息技术岗位。运行
`filter_it_domain.cmd` 后，每个当前 JD 版本会得到 `IT`、`NON_IT` 或
`UNCERTAIN` 判定。判定同时使用标准岗位族、实际岗位名称、职责中的多组
技术证据、行业反证和本地 BGE 语义相似度，不以单个关键词作为准入依据。

归一化与图谱证据层只读取 `domain_label = 'IT'` 的记录；`NON_IT` 和
`UNCERTAIN` 仍保留在 Neo4j 中，并记录分数、命中的证据组和排除原因，便于
抽样审计或调整策略后重新判定。

领域准入后运行 `backfill_it_roles.cmd`。它根据**实际职位名**映射
`it_role_taxonomy.json` 中的受控 IT 岗位族；来源文件名仅在它本身属于该
分类表时才可作为回退。无法可靠映射的 IT 记录保留在原始审计层，但不会冒充
“审计经理、药品生产”等岗位进入前端图谱。

## 平时打开图谱

1. 在 Neo4j Desktop 中启动岗位图谱数据库，确认状态为 `RUNNING`。
2. 双击 `start_v5_neo4j.cmd`。
3. 浏览器访问 `http://127.0.0.1:8060/`。

## 清理错误能力点

Neo4j 启动后，双击 `cleanup_v5_neo4j_noise.cmd`。看到 `CLEANUP_COMPLETE` 后刷新网页，不需要重建数据。

## 重新构建正式图谱

双击 `build_v5_neo4j.cmd`。该操作会重新计算向量，耗时较长；只有源数据或处理规则变化时才运行。

## 目录说明

- `output/all_it_roles_knowledge_graph_v5_neo4j`：当前正式 V5 结果。
- `output/all_it_roles_sample`：重新构建 V5 使用的基础数据。
- `config`：Neo4j 本机连接配置。
- `trusted_graph_agent`：核心程序代码。
- `models`：本地向量模型。
- `.venv`：Python 运行环境。
- `tests`：自动测试。
- `docs`：保留的项目汇报材料。
- `raw_jd_layer`：把全部CSV/JSON增量导入Neo4j原始JD层。

不要删除 `all_it_roles_sample`、`models` 或 `.venv`，否则无法重新构建。

全量原始JD首次导入请阅读 `raw_jd_layer/README.md`。该流程只导入原始数据，不运行大模型和向量计算。
