# CPB

CPB 是一个最小可行的分布式计算任务市场。发布者为小型、确定性的
`ContainerJob` 工作负载预留内部积分；Linux 工作节点租用任务，在受限的 Docker
容器中运行，上传证据，并仅在平台验证结果后获得积分。

该 MVP 有意采用中心化的 PostgreSQL 调度和仅追加的积分账本。它不实现现金、
加密货币、GPU、动态竞价、私有源代码凭据、Kubernetes 或消息代理。

## 架构

```mermaid
flowchart LR
  Publisher["发布者 / API 密钥"] --> API["FastAPI /api/v1"]
  Worker["工作节点 / worker 令牌"] --> API
  API --> DB[("PostgreSQL 队列 + 账本")]
  API --> S3[("MinIO / S3 对象")]
  Worker --> S3
  Worker --> Docker["受限 Docker 容器"]
  API --> Verifier["ContainerJob 验证器"]
  Verifier --> S3
  Verifier --> DB
```

API 负责状态转换、租约、验证和结算。工作节点不受信任：它们只能取得短时有效的、
单次运行的租约密钥，而数据库只保存其 HMAC 哈希。详见 [MVP 架构](docs/mvp-architecture.md)、
[任务生命周期](docs/task-lifecycle.md)、[积分账本](docs/credit-ledger.md)和
[安全性](docs/security.md)。

## 环境要求

- 已安装 Compose 插件的 Docker Engine
- 工作节点主机上安装 Python 3.11 或更高版本
- GNU Make 为可选项；下文所有命令都提供了不依赖 Make 的写法

将 `.env.example` 复制为 `.env`，并替换 `CPB_API_SECRET` 与
`CPB_STORAGE_SECRET_KEY`。仓库中提交的默认值仅供开发使用。

## 本地部署

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose exec api cpb seed
docker compose cp api:/app/seed-credentials.json ./seed-credentials.json
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

在 PowerShell 中，请使用 `.\.venv\Scripts\Activate.ps1` 激活虚拟环境。
API 容器启动时会自动执行 PostgreSQL 迁移；如需手动执行：

```bash
docker compose run --rm api alembic upgrade head
```

OpenAPI 文档位于 [http://localhost:8000/docs](http://localhost:8000/docs)，MinIO
控制台位于 [http://localhost:9001](http://localhost:9001)，健康状态接口为
`GET /api/v1/health`。

## 运行工作节点

工作节点需要访问主机 Docker CLI，因此必须运行在 Linux 主机上。从
`seed-credentials.json` 读取生成的 `worker_token`，然后执行：

```bash
cpb-worker run \
  --server http://localhost:8000 \
  --token '<worker_token>'
```

`cpb seed` 创建的节点默认使用 `x86_64` 架构。如使用 ARM，请通过
`POST /api/v1/nodes/register` 注册匹配架构的节点。任务容器绝不会获得 Docker socket。

## 端到端示例

在一个终端中保持工作节点运行，并在另一个终端执行：

```bash
python scripts/demo.py \
  --server http://localhost:8000 \
  --credentials seed-credentials.json
```

该脚本会压缩 `examples/python-pytest-job`，上传并提交它，等待任务完成，随后打印
发布者扣减额、工作节点所有者余额以及预签名制品 URL。该示例只使用 Python 标准库，
因此即使在禁用网络的 `python:3.12-slim` 容器中也能运行。

等效的 Make 目标为 `make dev`、`make migrate`、`make seed`、`make worker`、
`make demo`、`make recover`、`make test` 和 `make lint`。

## 测试

```bash
pytest
ruff check .
```

对并发敏感的测试需要一个可随时丢弃的 PostgreSQL 数据库：

```bash
docker compose exec postgres createdb -U cpb cpb_test
export TEST_DATABASE_URL='postgresql+psycopg://cpb:cpb-dev-only@localhost:5432/cpb_test'
pytest -m postgres
```

单元测试仅对不依赖行锁语义的逻辑使用隔离的 SQLite 数据库；双重租约测试仅适用于
PostgreSQL。工作节点执行测试会模拟 Docker 进程。请通过 demo 手动运行附带任务，
以实际 Docker 守护进程验证完整流程。

## 运维与限制

API 后台循环会恢复过期租约，也可运行一次性命令：

```bash
docker compose exec api cpb recover-leases
```

限制和定价均可通过环境变量配置。默认价格为每预留一个 CPU 核心分钟收取一个积分：
`cpu × ceil(timeout_seconds / 60)`。新的开发用户会获得 100 积分。成功完成的工作中，
80% 支付给节点所有者，20% 支付给平台账户。

## 当前安全边界与路线图

Docker 限制包括：禁用网络、只读容器根文件系统、CPU/内存/PID 限制、移除所有能力、
`no-new-privileges`、非 root UID，以及隔离的一次性工作区。Docker 并不足以作为
针对恶意多租户的安全边界。生产环境应增加 gVisor、Kata、Firecracker 或专用 VM；
恶意软件扫描；配额；审计导出；更强的身份验证；以及独立可信验证。完整威胁模型和
限制请参阅 [security.md](docs/security.md)。

下一个里程碑应强化执行隔离与可观测性，增加验证器镜像和对象保留策略，并在竞争
条件下对 PostgreSQL 租约和结算进行负载测试。
