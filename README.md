# 跨区 DNS 变更发布顺序规划器

给内网 DNS 值班员用的 FastAPI 服务，提供两个接口：

* **`/plan`**：提交最多 8 个**互不嵌套**的权威区的新旧 A / CNAME 记录，
  以及必须持续可解析的业务域名，服务按**无缓存权威记录**推演每一次
  **整区切换**后的解析状态，回答三件事：

  1. **有没有安全顺序**：有则返回逐步发布顺序，并给出每一步前后的解析结果。
  2. **现状是否已经坏了**：若按现状（所有区都还是旧记录）就查不到 IPv4 地址，
     直接返回故障链路，不再谈发布顺序。
  3. **没有安全顺序时卡在哪**：返回能安全走到的**最大前缀**，并逐个说明剩余
     每个区若紧接着发布会断在哪条解析链上，区分**缺记录**与**成环**。

* **`/ixfr/reconstruct`**：值班员手里的 IXFR 日志来自多个副本，片段乱序、
  可能互相矛盾。提交区名、A/CNAME 备份及其 SOA 序列号、主库目标序列号和
  最多 20 条乱序差量，服务判断这些片段能否恢复出主库指定序列号的
  **唯一单区快照**；能则返回可直接填入 `/plan` 的 `records`。

## 推演模型

* 每个区只有两个状态：整区旧记录、整区新记录。一次发布 = 一个区整区切换，
  区内不存在新旧共存的中间态。
* 解析不经过缓存，不使用 TTL；记录里的 `ttl` 只做原样保留，不影响结论。
* 支持 A 与 CNAME。CNAME 链跨已提交的区追踪，最多 32 跳。
* CNAME 链**离开已提交的区**即视为无法确认（`external_target`）。需要把外部区
  纳入推演时，把它按 `records == new_records` 一并提交即可。
* 所有记录必须属于本区；同一名称不能同时有 A 与 CNAME；区顶点不允许 CNAME；
  同名 CNAME 目标必须一致；A 值必须是 IPv4。违反则返回 422。

### 为什么不是贪心

安全谓词不单调：发布一个区可能同时修好一个域名、又弄坏另一个；一个“当前会断”
的状态也可能被后发布的区修好。因此“每次挑一个当前安全的区”并不保证找到存在的
安全顺序。服务对 **2^n ≤ 256 个状态**做完整深度优先搜索（带记忆化），
一定给出正确结论；无解时在所有等长的安全前缀中挑“卡点失败域名最少”的那个。

## IXFR 片段重建模型

`/ixfr/reconstruct` 把来自多个副本的差量片段按序列号衔接成链：

* 序列号**不回绕**，按普通整数比较；差量只能向前（`to_serial > from_serial`），
  目标序列号不得早于备份序列号。
* 差量按序列号衔接：片段的 `from_serial` 必须等于当前状态的序列号才能应用；
  与提交顺序无关。
* 删除按**完整记录**匹配（名称、类型、值、TTL 全部一致）；添加不得重复删除后
  仍然保留的记录。记录按集合处理（忽略顺序）。
* 每应用完一整条差量，区记录必须满足 `/plan` 的校验规则（记录归属本区、
  同名不共存 A 与 CNAME、区顶点无 CNAME、CNAME 目标唯一、CNAME 之下无名称、
  每区最多 256 条），否则该片段不可用。
* 序列号沿链严格递增，片段构成有向无环图；服务对 `(序列号, 记录集)` 状态做
  带记忆化的完整遍历，考察**所有**有效链。

三种结果：

| status | 含义 |
| --- | --- |
| `unique_snapshot` | 所有到达目标序列号的有效链得到同一组记录（忽略顺序），返回可填入 `/plan` 的 `records` |
| `ambiguous` | 至少两条有效链到达目标但结果不同，返回两条链的片段编号和一处记录差异，**不返回快照** |
| `unreachable` | 没有有效链到达目标，返回目标前最高可达序列号，**不返回快照** |

## 启动

```bash
docker compose up --build -d
docker compose ps          # 等待 healthy
curl -s http://localhost:8000/healthz
```

OpenAPI 文档：<http://localhost:8000/docs>

镜像以非 root 用户运行，容器只读挂载根文件系统（`read_only: true`、
`no-new-privileges`），并带 `HEALTHCHECK`。

### 不用 Docker 时

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

### `GET /healthz`

```json
{"status": "ok", "version": "1.0.0"}
```

### `POST /plan`

请求体：

```json
{
  "zones": [
    {
      "name": "a.example.com",
      "records":     [{"name": "www.a.example.com", "type": "CNAME", "value": "svc.b.example.com", "ttl": 60}],
      "new_records": [{"name": "www.a.example.com", "type": "CNAME", "value": "svc2.b.example.com", "ttl": 60}]
    },
    {
      "name": "b.example.com",
      "records":     [{"name": "svc.b.example.com",  "type": "A", "value": "10.20.1.10"}],
      "new_records": [{"name": "svc.b.example.com",  "type": "A", "value": "10.20.1.10"},
                      {"name": "svc2.b.example.com", "type": "A", "value": "10.20.2.10"}]
    }
  ],
  "required_domains": ["www.a.example.com"]
}
```

`records` 是现状，`new_records` 是该区切换后的状态；两者相同表示“这个区不变，
但仍参与解析”。

响应 `status` 有三种：

| status | 含义 |
| --- | --- |
| `safe_order_found` | 存在安全顺序，`order` / `steps` 给出逐步操作与前后解析结果 |
| `already_broken` | 现状就解析不到 IPv4，`fault_chains` 给出故障链 |
| `no_safe_order` | 无完整安全顺序，`safe_prefix` + `blocked_zones` 给出卡点 |

每条失败链都带：

* `category`：`missing_record` / `cname_loop` / `external_target` / `chain_too_long`
* `category_label`：`缺记录` / `成环` / `链出已提交的权威区` / `CNAME 链过长`
* `detail`：缺记录时进一步区分 `nodata`（名称存在但无 A/CNAME）与
  `nxdomain`（名称不存在）
* `chain`：可读链路，例如
  `www.a.example.com --CNAME--> u.b.example.com => 无记录（NXDOMAIN）`
* `trace`：结构化的逐跳记录，便于值班员复制进工单

### `POST /ixfr/reconstruct`

请求体：

```json
{
  "zone": "ops.example.com",
  "backup_serial": 100,
  "backup_records": [
    {"name": "www.ops.example.com", "type": "A", "value": "10.0.0.1", "ttl": 300}
  ],
  "target_serial": 103,
  "deltas": [
    {
      "id": "frag-a",
      "from_serial": 100,
      "to_serial": 101,
      "deletions": [],
      "additions": [
        {"name": "db.ops.example.com", "type": "A", "value": "10.0.1.1", "ttl": 120}
      ]
    }
  ]
}
```

* `zone`：区名；备份与差量里的记录都必须属于本区（按 `/plan` 规则校验）。
* `backup_serial` / `backup_records`：手头的 A/CNAME 备份及其 SOA 序列号。
* `target_serial`：主库目标序列号，不得早于备份序列号。
* `deltas`：最多 20 条乱序差量。每条有唯一 `id`（字符串或整数）、
  `from_serial` / `to_serial`（只能向前）以及 `deletions` / `additions`
  （完整记录，含 TTL）。

响应 `status` 有三种（规则见上文「IXFR 片段重建模型」）：

| status | 关键字段 |
| --- | --- |
| `unique_snapshot` | `records`（可直接填入 `/plan`）、`record_count`、`example_chain`（一条有效链的片段编号） |
| `ambiguous` | `chain_a` / `chain_b`（两条有效链的片段编号）、`difference`（一处记录差异及它属于哪条链）、`distinct_snapshots_at_target`；不返回 `records` |
| `unreachable` | `highest_reachable_serial`（目标前最高可达序列号）、`reachable_serials`；不返回 `records` |

请求不合法（序列号越界或倒退、目标早于备份、差量编号重复、超过 20 条、
备份本身不满足区规则等）返回 422。

## 调用示例

仓库自带 4 个场景，可直接用 curl 调用：

```bash
# 1) 存在安全顺序：必须先发 b，再发 a
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/01-safe-order.json | python -m json.tool

# 2) 现状即故障：CNAME 指向一个没有记录的区
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/02-already-broken.json | python -m json.tool

# 3) 无安全顺序：a/b 互为前置，c 可安全先发
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/03-no-safe-order.json | python -m json.tool

# 4) 无安全顺序且卡点同时含“缺记录 + 成环”
curl -s -X POST http://localhost:8000/plan \
  -H 'Content-Type: application/json' \
  --data-binary @examples/04-blocked-mixed.json | python -m json.tool

# 5) IXFR：乱序片段拼出唯一快照（frag-x 的删除 TTL 不匹配，被跳过）
curl -s -X POST http://localhost:8000/ixfr/reconstruct \
  -H 'Content-Type: application/json' \
  --data-binary @examples/ixfr-01-unique.json | python -m json.tool

# 6) IXFR：两个副本对同一序列号给出不同记录，结果矛盾
curl -s -X POST http://localhost:8000/ixfr/reconstruct \
  -H 'Content-Type: application/json' \
  --data-binary @examples/ixfr-02-ambiguous.json | python -m json.tool

# 7) IXFR：缺少 101 -> 102 的片段，到不了目标序列号
curl -s -X POST http://localhost:8000/ixfr/reconstruct \
  -H 'Content-Type: application/json' \
  --data-binary @examples/ixfr-03-unreachable.json | python -m json.tool
```

场景 1 的响应节选：

```json
{
  "status": "safe_order_found",
  "order": ["b.example.com", "a.example.com"],
  "steps": [
    {"step": 1, "zone": "b.example.com",
     "addresses_before": {"www.a.example.com": ["10.20.1.10"]},
     "addresses_after":  {"www.a.example.com": ["10.20.1.10"]},
     "all_required_resolvable": true},
    {"step": 2, "zone": "a.example.com",
     "addresses_before": {"www.a.example.com": ["10.20.1.10"]},
     "addresses_after":  {"www.a.example.com": ["10.20.2.10"]},
     "all_required_resolvable": true}
  ]
}
```

场景 4 的响应节选：

```json
{
  "status": "no_safe_order",
  "safe_prefix": ["d.example.com"],
  "blocked_zones": [
    {"zone": "a.example.com",
     "categories": [{"category": "missing_record", "label": "缺记录"}],
     "failures": [{"chain": "one.a.example.com --CNAME--> gone.b.example.com => 无记录（NXDOMAIN）"}]},
    {"zone": "c.example.com",
     "categories": [{"category": "cname_loop", "label": "成环"}],
     "failures": [{"chain": "two.a.example.com --CNAME--> keep.c.example.com --CNAME--> keep.c.example.com => 回到 keep.c.example.com，成环"}]}
  ]
}
```

场景 5（IXFR 唯一快照）的响应节选：

```json
{
  "status": "unique_snapshot",
  "example_chain": ["frag-a", "frag-c", "frag-b"],
  "records": [
    {"name": "api.ops.example.com", "type": "CNAME", "value": "db.ops.example.com", "ttl": 60},
    {"name": "db.ops.example.com", "type": "A", "value": "10.0.1.1", "ttl": 120},
    {"name": "www.ops.example.com", "type": "A", "value": "10.0.0.2", "ttl": 300}
  ]
}
```

场景 6（矛盾）与场景 7（断链）的响应节选：

```json
{
  "status": "ambiguous",
  "chain_a": ["replica-a"],
  "chain_b": ["replica-b"],
  "difference": {
    "record": {"name": "api.ops.example.com", "type": "A", "value": "10.0.2.1", "ttl": 300},
    "present_in_chain_a": true,
    "present_in_chain_b": false
  }
}
```

```json
{"status": "unreachable", "highest_reachable_serial": 101,
 "reachable_serials": [100, 101]}
```

也可以不启动服务、直接跑同一套逻辑：

```bash
python examples/demo.py
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

覆盖：安全顺序、必须后发/先发的依赖、现状故障（缺记录 / 成环 / 链出区 /
链过长 / NODATA vs NXDOMAIN）、无解时的最大前缀与逐区卡点、随机小规模实例与
穷举结果的等价性校验；IXFR 重建的唯一快照 / 矛盾 / 断链三种结果、乱序与
多副本片段、删除 TTL 精确匹配、添加不重复、差量后区状态按 /plan 规则校验、
请求校验与 HTTP 状态码。

## 限制

* 只支持 A / CNAME；AAAA、SRV、通配符等不参与推演。
* 最多 8 个区、每区 256 条记录、最多 64 个必保域名。
* 同一名称的多条 A 记录视为一个 RRset（多个地址任选其一即视为可解析）。
* 不校验外部区真实状态；链出已提交区一律按“无法确认”处理。
* IXFR 重建每次请求最多 20 条差量；序列号按不回绕的 uint32 处理；
  快照是否唯一只比较记录集合（忽略顺序），不比较到达路径。
