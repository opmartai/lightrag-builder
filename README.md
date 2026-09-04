# LightRAG Controller 与知识服务部署

本仓库维护 LightRAG Controller 的源码、测试、镜像构建，以及 Docling/LightRAG 的部署。
Controller 按知识库创建、检查和删除独立 LightRAG 实例；Docling 将文档转成 Markdown，
LightRAG 负责索引和检索。Backend 保留业务 API、权限、文档记录和索引任务，通过认证的
HTTP 接口调用 Controller。两个仓库不互相导入源码，也不依赖对方构建镜像。

PostgreSQL（含 pgvector）与 Docker 网络由环境提供，本仓库不管理其启停、初始化或数据卷。

## 准备与构建

需要 Linux、Docker Compose v2、Python 3.11+，以及可访问的 PostgreSQL 和 Docker 网络。
部署脚本只使用 Python 标准库，无须安装 Controller 的 Python 依赖。

参考 `.env.example`，在仓库外准备权限为 `0600` 的配置文件，填写：

- `STACK_NAME`、`NETWORK_NAME`、`POSTGRES_*`：项目名、已有网络及数据库连接。
- `LIGHTRAG_NAMESPACE`：实例管理范围；接管已有实例时沿用原值。
- `LIGHTRAG_CONTROLLER_TOKEN`、`LIGHTRAG_API_KEY`：服务间调用凭据。
- 模型和 embedding 的地址、凭据、模型名称及向量维度。

镜像默认值见 `images.env`，可在私有配置中覆盖。
下面使用本机配置路径，新环境请替换：

```bash
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env config
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env build
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env pull
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env up
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env status
```

`config` 只检查配置，不输出凭据。`build` 从本仓库构建 Controller；
`pull` 拉取 Docling 和 LightRAG。`up` 恢复本 namespace 的实例并等待服务健康。
`--env-file` 可重复指定，后面的配置覆盖前面的同名项。
更新 Controller 源码后执行 `build` 和 `up`。

也可独立构建镜像，无须配置数据库或检出 Backend：

```bash
docker build -t lightrag-controller:local .
```

使用预构建镜像时，先用 `docker pull` 拉取并设置 `CONTROLLER_IMAGE`，再执行 `up`。

## 停止

停止前暂停 Backend 的知识库写入：

```bash
python3 scripts/runtime.py --env-file /home/za/.config/lightrag-builder/runtime.env stop
```

`stop` 保留实例数据卷；若其他服务共享 Docling，加 `--keep-docling`。
知识服务恢复后重启 Backend，由其启动恢复流程刷新实例地址。

## Controller 开发与接口

`controller.py` 是独立 FastAPI 服务，`requirements.txt` 是镜像依赖；
`requirements-dev.txt` 增加测试和 lint 工具。Python 代码与提交信息使用英文。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q
.venv/bin/ruff check controller.py tests/ scripts/
```

所有业务接口和健康检查都要求 `Authorization: Bearer <LIGHTRAG_CONTROLLER_TOKEN>`：

| 接口 | 用途 |
| --- | --- |
| `GET /health` | Controller 进程健康 |
| `PUT /v1/instances/{knowledgeBaseId}` | 幂等创建并等待实例就绪 |
| `GET /v1/instances/{knowledgeBaseId}` | 检查实例和工作空间 |
| `DELETE /v1/instances/{knowledgeBaseId}` | 清理索引后删除所属容器和数据卷 |
| `POST /v1/instances/reconcile` | 对比请求的知识库与已管理实例，不自动删除 |

创建和查询返回 `provider`、`providerRef`、`baseUrl`、`workspace`、`status`。
身份由 namespace 与知识库 ID 确定；修改 namespace 会切换资源范围。
Controller 只操作标签匹配的资源，不提供通用 Docker 操作接口。

## 基础验证

```bash
python3 scripts/smoke.py --env-file /home/za/.config/lightrag-builder/runtime.env
```

检查 Docling 与 Controller 健康、真实 DOCX 转 Markdown，以及临时 LightRAG 实例的创建、
数据库连接、工作空间身份和删除清理。默认不调用模型或 embedding，不证明入库检索质量。

需要验证完整入库检索时加 `--index`：

```bash
python3 scripts/smoke.py --env-file /home/za/.config/lightrag-builder/runtime.env --index
```

脚本只使用 `kb_deploy_smoke_` 临时实例，并在退出前清理。
如果进程被强制终止，应根据输出的测试标识通过 Controller 删除对应实例。

## 本机接入

日常入口为 `/usr/local/amazon/scripts/knowledge-runtime.sh`。项目名 `lightrag-main`，
namespace 为 `local-systemd`；Controller 端口 19631，Docling 端口 15001。
Backend 在 WSL 宿主机运行，因此设置 `ADVERTISE_CONTAINER_IP=true`；
Backend 与实例同处 Docker 网络时可使用容器名称。

私有配置为 `/home/za/.config/lightrag-builder/runtime.env`。
现有网络为 `amazon-v3-local-acceptance-net`，数据库为 `acceptance-postgres:5432/lightrag`。
环境维护命令见 `/usr/local/amazon/README-LOCAL.md`。
沿用原 namespace、workspace 和实例卷，保留已有知识库与索引的对应关系。
