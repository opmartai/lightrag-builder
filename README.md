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
`--compose-file` 可重复追加 Compose 配置，按顺序合并在本仓库 `compose.yaml` 之后；
相对路径从本仓库解析，绝对路径直接使用。不指定时只使用基础 Compose。
`runtime.py` 的 Compose 生命周期命令与 `smoke.py` 使用同一组参数。
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

## dev 部署

标准部署位置为 `/usr/local/amazon/lightrag-builder`。`deploy/dev.compose.yaml` 保留 dev 的
Docling 离线模型配置、处理限制及缓存/输入/输出挂载；主机路径由私有配置提供。
`deploy/amazon-experts-knowledge.service` 统一管理 Controller、Docling 和本 namespace 的动态实例，
直接调用本仓库脚本，不依赖 `/usr/local/amazon/scripts/`。

在 `/etc/amazon-experts/lightrag-runtime.env` 准备权限为 `0600` 的完整配置：
从 `.env.example` 补齐 dev 当前数据库、服务凭据与模型配置，并使用以下 dev 接入值。
不复制本地数据库、文档或运行数据，不修改环境提供的 PostgreSQL 与网络。

```dotenv
STACK_NAME=lightrag-dev
LIGHTRAG_NAMESPACE=dev-systemd
NETWORK_NAME=v3_default
POSTGRES_HOST=pgvector-18
POSTGRES_PORT=5432
POSTGRES_DATABASE=lightrag
CONTROLLER_PORT=9630
DOCLING_PORT=5001
ADVERTISE_CONTAINER_IP=true
DOCLING_MODELS_DIR=/data/amazon-docling-serve/models
DOCLING_INPUT_DIR=/data/amazon-docling-serve/input
DOCLING_OUTPUT_DIR=/data/amazon-docling-serve/output
DOCLING_IMAGE=quay.io/docling-project/docling-serve-cpu@sha256:061d35c03611bc15b73d024c8e8387bcf0624279f8b57c16c1567326f214ba56
LIGHTRAG_IMAGE=ghcr.io/hkuds/lightrag@sha256:ab23a9c83a735901b18c8960b6b482b602d5b6291abb7e07c5776f7bb2da504e
CONTROLLER_IMAGE=lightrag-controller:dev
```

以上 Docling digest 是 dev 已运行的 v1.30.0；本次接管复用现有镜像和模型缓存。
该镜像已默认设置 `OMP_NUM_THREADS=4`，与原运行容器一致，无须在 dev override 重复配置。
`images.env` 保留通用默认版本，私有配置中的镜像值优先；若需升级镜像，应单独构建或拉取并验证。
`DOCLING_PORT` 和 `CONTROLLER_PORT` 均绑定宿主机 `127.0.0.1`，Backend 继续使用
`http://127.0.0.1:5001` 和 `http://127.0.0.1:9630`。

先验证配置并构建独立 Controller 镜像：

```bash
cd /usr/local/amazon/lightrag-builder
sudo python3 scripts/runtime.py --env-file /etc/amazon-experts/lightrag-runtime.env --compose-file deploy/dev.compose.yaml config
sudo python3 scripts/runtime.py --env-file /etc/amazon-experts/lightrag-runtime.env --compose-file deploy/dev.compose.yaml build
```

首次切换前备份原服务声明和私有配置，暂停 Backend 知识库写入，
执行 `sudo systemctl stop amazon-experts-backend.service` 停止 Backend。
在现有 `/etc/systemd/system/amazon-experts-backend.service.d/knowledge-runtime.conf` 中，
将 `Wants=` 和 `After=`
依赖列表里的 `amazon-experts-lightrag-controller.service` 替换为
`amazon-experts-knowledge.service`，保留原有环境配置和其他依赖。
仅禁用旧 Controller unit 不能阻止 Backend 的 `Wants=` 再次启动它。

停止并禁用旧 Controller unit，同时停止旧 Docling 并关闭其容器重启策略，释放宿主端口；
保留旧镜像、声明备份、数据目录和实例卷供回退，不再使用旧声明启动服务：

```bash
sudo systemctl disable --now amazon-experts-lightrag-controller.service
sudo docker compose -p amazon-docling-serve -f /data/amazon-docling-serve/docker-compose.yml stop docling-serve
sudo docker update --restart=no docling-serve
sudo install -m 0644 deploy/amazon-experts-knowledge.service /etc/systemd/system/amazon-experts-knowledge.service
sudo systemctl daemon-reload
sudo systemctl enable --now amazon-experts-knowledge.service
sudo python3 scripts/runtime.py --env-file /etc/amazon-experts/lightrag-runtime.env --compose-file deploy/dev.compose.yaml status
sudo python3 scripts/smoke.py --env-file /etc/amazon-experts/lightrag-runtime.env --compose-file deploy/dev.compose.yaml
```

旧 Controller unit 使用 `app.lightrag_controller:app`，不能直接沿用该命令启动本仓库镜像。
新服务使用镜像自带的 `controller:app` 入口；不要同时启动两个管理 `dev-systemd` 的 Controller。
上述 `daemon-reload` 同时加载 Backend 依赖变更。知识服务和基础 smoke 通过后，执行
`sudo systemctl start amazon-experts-backend.service` 恢复 Backend，
再从 Backend 验证文档上传、转换、索引和检索。
需要真实模型验证时对上述 smoke 增加 `--index`；smoke 本身不覆盖 Backend 业务入口。

日常用 `systemctl start|stop|restart amazon-experts-knowledge.service` 管理服务。
停止前暂停知识库写入，恢复后重启 Backend 刷新实例地址。
`up` 会接管既有 namespace 实例，但不会重建已有实例或更新其镜像和环境变量；
更改模型、embedding 或实例镜像时需单独安排实例更新。
原固定池 `lightrag-1/2` 不属于 Controller 管理范围，应先确认消费者，再单独处理其退役。

## 本机接入

日常入口为 `/usr/local/amazon/scripts/knowledge-runtime.sh`。项目名 `lightrag-main`，
namespace 为 `local-systemd`；Controller 端口 19631，Docling 端口 15001。
Backend 在 WSL 宿主机运行，因此设置 `ADVERTISE_CONTAINER_IP=true`；
Backend 与实例同处 Docker 网络时可使用容器名称。

私有配置为 `/home/za/.config/lightrag-builder/runtime.env`。
现有网络为 `amazon-v3-local-acceptance-net`，数据库为 `acceptance-postgres:5432/lightrag`。
环境维护命令见 `/usr/local/amazon/README-LOCAL.md`。
沿用原 namespace、workspace 和实例卷，保留已有知识库与索引的对应关系。
