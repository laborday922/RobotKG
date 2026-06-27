# RobotKG Python Backend (FastAPI)

这是一个用于构造知识库（Neo4j）的 Python 后端服务，提供“文件新增/修改/删除 → 抽取 → 写入图谱”的 HTTP API，供 Spring Boot 等系统同步调用。

## 功能概览

- 文件新增/修改：接收 `file_id + file_name + content + metadata`，两轮抽取后写入 Neo4j
- 文件删除：删除对应 `Document` 及该文件产生的关系增量
- 抽取策略：
  - 第一轮：粗粒度抽取章节结构（正则优先；LLM 兜底接口预留）
  - 第二轮：按章节类型提取结构化字段（正则/列表解析；按 key 选择策略）
- 字段名映射：支持“申请材料/所需材料”等同义标题自动归一；也支持每个文件传入自定义映射覆盖

## 目录结构

- `main.py`：FastAPI 入口与路由
- `config.py`：环境变量配置（`KG_` 前缀）
- `extractor.py`：两轮抽取与字段映射
- `neo4j_client.py`：Neo4j 写入/删除/查询封装
- `schemas.py`：API 请求/响应模型

## 启动方式

### 1) 安装依赖

在项目根目录（`g:\Robot\RobotKG`）执行：

```powershell
.\.venv\Scripts\pip install -r requirements.txt
```

### 2) 配置环境变量（可选但推荐）

Neo4j 连接：

- `KG_NEO4J_URI`（默认 `bolt://8.163.1.154:7687`）
- `KG_NEO4J_USER`（默认 `neo4j`）
- `KG_NEO4J_PASSWORD`（默认 `neo4j`）
- `KG_NEO4J_DATABASE`（可选）
- `KG_NEO4J_CREATE_SCHEMA`（默认 `true`，启动时尝试创建唯一约束）

鉴权（可选）：

- `KG_API_TOKEN`：如果设置，则需要在请求头携带 `Authorization: Bearer <token>`

### 3) 启动服务

```powershell
.\.venv\Scripts\uvicorn kg_api.main:app --host 0.0.0.0 --port 8000
```

启动后可访问：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI JSON：`http://127.0.0.1:8000/openapi.json`

## API 列表

- `GET /health`：存活探针
- `GET /ready`：就绪探针（检查 Neo4j 连通性）
- `POST /files/upsert`：新增/修改文件并同步图谱
- `DELETE /files/{file_id}`：删除文件并同步删除图谱
- `GET /files/{file_id}`：查询文件摘要（实体列表、关系数量等）

### /files/upsert 请求示例

```json
{
  "file_id": "doc-001",
  "file_name": "指南.txt",
  "content": "服务事项：个人社会保险缴纳\n所需材料：身份证\n办理流程：提交材料",
  "metadata": {
    "sourcePath": "D:/docs/指南.txt"
  }
}
```

返回格式（成功）：

```json
{
  "ok": true,
  "message": "upserted",
  "data": {
    "file_id": "doc-001",
    "entities_count": 12,
    "relations_count": 3
  }
}
```

返回格式（失败）：

```json
{
  "ok": false,
  "message": "..."
}
```

## 字段名映射（同义标题/自定义映射）

系统内置同义标题归一，例如：

- `申请材料` / `所需材料` / `材料清单` → `materials`
- `办理流程` / `办理程序` / `办理步骤` → `process`

如遇到文档标题非常规，可在 `metadata` 里给出覆盖映射：

```json
{
  "metadata": {
    "field_mapping": {
      "所需资料": "materials",
      "办理程序": "process",
      "办理地点（地址）": "address"
    }
  }
}
```

`field_mapping` 或 `heading_aliases` 两个键任选其一。

## 抽取两轮说明

### 第一轮：粗粒度抽取（找结构）

- 按“标题：”切分章节，输出 `sections=[{title,key,text}]`
- key 会被映射为规范字段（如 `materials/process/policy_basis` 等）
- 若正则切分效果很差（章节过少）且接入了 LLM，则可用 LLM 兜底（当前默认未启用）

### 第二轮：细粒度抽取（提取值）

- 针对不同 `section.key` 使用不同解析策略：
  - `materials`：提取条目列表
  - `process`：提取步骤列表
  - `policy_basis`：提取法规/文件名称列表
  - `channels/contact/complaint`：提取电话/网址等

输出会写入 `Document.structured_json`（JSON 字符串）。

## Neo4j 数据模型

- `(:Document {id, name, content, updated_at, metadata_json, structured_json})`
- `(:Entity {name})`
- `(d:Document)-[:MENTIONS]->(e:Entity)`
- `(a:Entity)-[:RELATED {doc_id, type}]->(b:Entity)`（doc_id 用于按文件增量清理）

## LLM 预留接口

`extractor.py` 中定义了 `LLMExtractor` 协议以及预留 Prompt 常量：

- `COARSE_STRUCTURE_PROMPT`
- `FINE_VALUE_PROMPTS`

当前 `main.py` 调用时传入 `llm=None`，后续你可以实现 LLM 客户端并替换为实例以启用“匹配不上才调用 LLM”的兜底逻辑。

