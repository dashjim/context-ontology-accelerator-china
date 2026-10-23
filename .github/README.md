# ✦ Context Ontology Accelerator — AWS 中国区适配版

本仓库的 `china-region` 分支，基于上游 [aws/context-ontology-accelerator](https://github.com/aws/context-ontology-accelerator) **v0.3.5**，改造后可以部署到 **AWS 中国区**（`aws-cn` partition），已在宁夏（cn-northwest-1）实际部署和验证。

- **商业区行为不变**：所有改造都由 CDK context 开关控制，在商业区默认取原来的值；
- **中国区自动生效**：部署到 `cn-` region 时，开关自动取中国区的默认值，不需要额外传参；
- **不依赖 Global 资源**：中国区方案不引用任何商业区（Global）账号的资源，有测试锁定这一点。

> 本仓库是社区维护的改造版本，**不是 AWS 官方发布**，不提供官方支持。上游项目及其许可证见 [aws/context-ontology-accelerator](https://github.com/aws/context-ontology-accelerator)（Apache-2.0）。

> COA 本身是什么、架构和开发方式，见上游英文说明 [README.md](/README.md)。本文只说明中国区改造的内容和部署方法。

---

## 为什么要改造

COA 依赖的以下能力在 AWS 中国区不存在，或者行为不同：

| 依赖 | 中国区情况 |
|---|---|
| Amazon Bedrock（LLM、embedding、Guardrails） | 不存在 |
| Amazon DataZone / SageMaker Unified Studio（元数据目录） | 不存在 |
| OpenSearch Serverless（向量库） | 不存在 |
| Cognito User Pools（登录） | 不存在 |
| AgentCore Memory | 不存在；AgentCore Runtime 存在，但要求 `PlatformVersion=V2` |
| `us-east-1` 的 edge-waf 栈 | 跨 partition，无法创建 |
| CloudFront OAC、API Gateway EDGE endpoint、Lambda 事件源映射的标签 | 不支持 |
| PyPI、Debian、PyTorch 源、Docker Hub、raw.githubusercontent.com | 从中国区构建机访问极慢或不通 |

另外，原代码里有 589 处硬编码的 `arn:aws:` ARN 和 `amazonaws.com` 主机名，在 `aws-cn` 下全部会出错。

## 做了哪些改造

### 1. Partition 适配

- **CDK**：ARN 和 endpoint 一律改用 `AWS::Partition` / `AWS::URLSuffix`；ECR 地址按所在 region 推导 partition；`cn-` region 下 CSP 使用 `.amazonaws.com.cn`。
- **运行时**：`libs/common` 新增 `aws_partition()` 和 `aws_dns_suffix()`，以下各项都改为根据 partition 生成：
  - S3 角色 ARN 的校验正则；
  - Cognito issuer；
  - Lake Formation 和 Databricks 连接器的角色 ARN；
  - AgentCore 调用地址（data-layer、mcp-server、mcp-proxy、web-app、scripts）。
- **校验**：各服务和 Smithy 模型里的 ARN 校验，统一放开为 `aws|aws-cn|aws-us-gov`。

### 2. 替代中国区缺失的服务

| 原依赖 | 中国区替代方案 | 开关（中国区默认值） |
|---|---|---|
| Bedrock LLM / embedding | **OpenAI 兼容接口**（例如硅基流动），API key 存放在部署账号的 Secrets Manager 中。覆盖全部调用方：serve 的 LLM（含流式）、共享 Bedrock 客户端、embedding、ontology-engine、Strands SQL agent（支持工具调用）、graphrag | `llm_provider=openai_compatible` |
| Bedrock Guardrails | 没有对应服务。跳过调用，并记一条结构化日志 `guardrail_unsupported_skipped`；不创建 GuardrailStack | 随 `llm_provider` 切换 |
| DataZone / SMUS | **DynamoDB 单表**实现的元数据存储（项目、成员关系、资产，GSI `ProjectAssetsIndex`） | `metadata_store_backend=dynamodb` |
| OpenSearch Serverless | **托管 OpenSearch 域**：只能从 VPC 内访问，默认 1 × t3.small.search，静态加密 + 节点间加密，只接受 TLS 1.2 HTTPS，访问策略只允许 IAM；SigV4 签名自动在 `es` 和 `aoss` 之间切换 | `vector_store=opensearch` |
| AgentCore Memory | 不创建 Memory。会话历史读取返回空，写入跳过；session 管理和流式查询照常可用 | `agentcore_memory_enabled=false` |
| AgentCore Runtime | 写入 `PlatformVersion=V2`（aws-cdk-lib 2.270 没有这个属性，通过 L1 属性覆盖实现） | `agentcore_platform_version=V2` |
| Cognito User Pools | 外部 OIDC IdP（`/coa/config` 中设 `idpType=OIDC`），并修复了 `jwksUri` 没有生效的问题 | — |
| edge-waf（`us-east-1`） | 不创建该栈，CloudFront 不挂 WebACL；传入 `cloudfront_web_acl_id` 时挂指定的 WebACL | `edge_waf=false` |

授权范围都限定到具体资源：

- API key secret 的 `GetSecretValue` 只授权给它的使用方；
- OpenSearch 只授权到这个域，其中 serve 只读；
- 元数据表按使用方分别授予读写或只读权限。

### 3. 中国区部署链路

- **镜像源**：`build_pip_index_url`、`build_apt_mirror`、`build_torch_index_url`、`build_torch_find_links` 会作为 build args 传给全部 7 个镜像。这几个开关默认不设置，因为能访问哪个镜像源取决于构建机，和部署到哪个 region 无关。
- **spaCy 模型**：可以离线安装官方 wheel，构建时校验 sha256。
- **preflight**：
  - `cn-` region 下跳过 `us-east-1` 的 bootstrap 检查和 ECR Public 登录；
  - 在 arm64 主机上构建 amd64 镜像时，检查 docker buildx 和 QEMU；
  - 修复 VPC 配额检查把已有 VPC 也算进去的问题。
- **`deploy.sh`**：新增 `SCL_EXTRA_CONTEXT`，内容原样追加到 CDK 的 context 参数后面。

### 4. 只有部署到中国区才会暴露的问题

| 现象 | 处理 |
|---|---|
| VPC endpoint 服务不存在 | 中国区有 12 个服务的 endpoint 服务名是 `cn.com.amazonaws.<region>.<svc>`，按实测清单生成；不创建 Neptune Analytics endpoint |
| IAM 拒绝 `bedrock.amazonaws.com` 服务主体 | 只有本 region 有 Bedrock 时，才创建批量推理角色 |
| Lambda 事件源映射报 "Tags not supported" | 中国区的栈级标签排除这类资源 |
| 没有 CloudFront OAC | 改用 OAI |
| API Gateway 不支持 EDGE | 改为 REGIONAL |

### 5. 顺带修复的上游缺陷（对商业区同样有效）

- **SQL 防火墙对带库名的授权表名匹配失效。** 列黑名单的键写成 `db.table` 时，限制会静默失效；表白名单写成 `db.table` 时，会把所有查询都拒绝。现在两者都统一按裸表名匹配。
- **`oidcSettings.jwksUri` 没有传给 API 授权器。**
- **Cognito 授权器用部署所在的 region 拼 JWKS 地址。** user pool 和部署不在同一 region 时会出错。
- **RIGOR 读取模型回答时只取第一个内容块**，模型先返回推理块时，输出会丢失。

---

## 开关一览

| CDK context | 商业区默认值 | `cn-` region 默认值 |
|---|---|---|
| `llm_provider` | `bedrock` | `openai_compatible`（必须同时设置 `openai_compat_base_url` 和 `openai_compat_api_key_secret_arn`） |
| `openai_compat_chat_model` / `openai_compat_embed_model` / `openai_compat_model_map` | — | — |
| `metadata_store_backend` | `smus` | `dynamodb` |
| `vector_store` | `aoss` | `opensearch`（实例类型可用 `opensearch_instance_type` 覆盖） |
| `agentcore_memory_enabled` | `true` | `false` |
| `agentcore_platform_version` | 不设置 | `V2` |
| `edge_waf` | `true` | `false` |
| `build_pip_index_url` / `build_apt_mirror` / `build_torch_index_url` / `build_torch_find_links` | 不设置 | 不设置 |

开关的取值逻辑统一在 [`infra/lib/utils/deployment-switches.ts`](/infra/lib/utils/deployment-switches.ts)：显式传入的 `-c` 优先，否则按部署 region 取默认值。

---

## 部署到中国区

### 前提

1. **合规**：模型和身份认证只能使用中国区账号内的服务或国内第三方，**不得使用 Global 账号的 Bedrock、API key 或 Cognito**。跨 partition 能调通，不等于允许使用。
2. **部署机**：建议用中国区内的 EC2，磁盘至少 100 GB，安装 node 22、pnpm、uv、JDK 21、make、Docker 和 **docker-buildx**。在 arm64 机器上构建 4 个 amd64 镜像需要 QEMU ≥ 9（Ubuntu 自带的 8.x 会崩溃）；更省事的做法是直接用 x86 机器构建。
3. **身份认证**：准备一个 OIDC IdP，在 SSM 参数 `/coa/config` 中设置 `idpType=OIDC` 和 `oidcSettings`（issuerUrl、clientId、jwksUri、groupClaim）。
   - **AgentCore 的 JWT 校验要求 IdP 的 discovery 地址是公网可访问的 HTTPS 地址**（由 AWS 服务端校验，VPC 模式也改变不了这一点）；
   - 如果 IdP 只在内网，API 和 Web UI 可以用，但 Playground 和 MCP 不能用。
4. **模型**：在部署账号的 Secrets Manager 中存放模型供应商的 API key（可以是纯 key，也可以是 JSON `{"api_key": "..."}`）。
5. **CloudFront**：账号需要开通 CloudFront 分发的创建；没开通时，`web` 栈会部署失败。

### 部署命令

```bash
export CDK_DEFAULT_REGION=cn-northwest-1 AWS_REGION=cn-northwest-1
export UV_DEFAULT_INDEX=https://repo.huaweicloud.com/repository/pypi/simple PIP_INDEX_URL=$UV_DEFAULT_INDEX
export SCL_NEPTUNE_INSTANCE_CLASS=db.t4g.medium        # 宁夏买不到 db.r8g；生产环境可以用 db.r7g.large
export SCL_EXTRA_CONTEXT="\
 -c openai_compat_base_url=https://api.siliconflow.cn/v1 \
 -c openai_compat_api_key_secret_arn=arn:aws-cn:secretsmanager:<REGION>:<ACCOUNT_ID>:secret:<NAME> \
 -c openai_compat_chat_model=deepseek-ai/DeepSeek-V3.2 \
 -c openai_compat_embed_model=Qwen/Qwen3-Embedding-8B \
 -c build_pip_index_url=https://repo.huaweicloud.com/repository/pypi/simple \
 -c build_apt_mirror=repo.huaweicloud.com \
 -c build_torch_find_links=https://mirrors.aliyun.com/pytorch-wheels/cpu/"
make deploy-dev
```

- embedding 模型需要支持 `dimensions` 参数，并能输出 1024 维。例如 `BAAI/bge-m3` 不接受这个参数，不能用。
- 完整说明见 [deploying.md](/external-docs/content/deploying.md) 的 "Other CDK context through `deploy.sh`" 和 "Package mirrors for image builds" 两节。

### 部署前自检

```bash
cdk synth --all ... && uvx cfn-lint --regions cn-northwest-1 -- infra/cdk.out/*.template.json
```

cfn-lint 能一次查出所有"资源类型在中国区不存在"的问题。但它查不出部署时才由服务端校验的问题，比如 IAM 服务主体、标签支持、API Gateway 的 endpoint 类型。这几类问题本分支都已修复，并有回归测试。

---

## 对商业区的影响

- **行为**：所有开关在商业区都保持原默认值，功能和权限不变。
- **模板不是逐字节一致**，差别有两处：
  - ARN 改成了 `Fn::Join` + `AWS::Partition`，ServeStack 因此多出一个 CDK 自动生成的溢出托管策略，权限不变；
  - Dockerfile 新增了默认值为空的 `ARG`，镜像会重新构建，内容和原来等价。
- **新增依赖**：`strands-agents[openai]`、`llama-index-llms-openai-like`；`boto3` 要求不低于 1.39。

## 验证情况

- **自动化测试**：infra（jest）、web-app（vitest）和各 Python 包的单测全部通过。新增的 [`infra/test/partition-regression.test.ts`](/infra/test/partition-regression.test.ts) 会在 cn region 下 synth 全部栈，并逐项断言上面提到的中国区约束。
- **宁夏实际部署**：14 个栈中 13 个成功。web 栈失败，是因为账号层面没有开通 CloudFront 分发创建。
- **全链路**：在 MOCK 数据集上跑通了以下全部环节：
  - API 和 Web UI；
  - Glue 扫描和 AI 富化；
  - 本体归纳和接受；
  - VKG；
  - 文档链路（preprocessing → 实体抽取 → kg-build）；
  - Tier 1/2/3 查询；
  - AgentCore 流式查询和 MCP。

## 已知限制

1. **没有内容安全层**：中国区没有 Bedrock Guardrails。有合规要求时，需要在 `llm_provider.skip_unsupported_guardrail()` 处接入国内的内容安全服务。
2. **AgentCore 需要公网 HTTPS 的 IdP**：见"部署到中国区 › 前提"第 3 条。
3. **答题质量取决于模型**：
   - 用 DeepSeek-V3.2 时，NL→SQL 倾向于只选最少的列；
   - 对于数据里没有答案的问题，它也总会生成一条 SQL，于是查询停在 Tier 2，用不上 Tier 3 的文档。
   - 建议换用更强的模型后再评估。
4. **AI 富化可能超时**：`ENRICHMENT_TABLE_TIMEOUT_SEC` 默认 90 秒。国内供应商在高并发下单次请求可能超过这个时间，建议多个数据源串行注册。
5. **DynamoDB 元数据存储的限制**：搜索只支持名称前缀，不保留 revision 历史。
6. **日志标签**：日志里打印的模型名仍是原来的 Bedrock 模型 ID，实际请求发往的是映射后的供应商模型。

## 与上游同步

- **上游**：[aws/context-ontology-accelerator](https://github.com/aws/context-ontology-accelerator)，只读镜像，不接受 PR。
- **冲突风险**：partition 改动遍布全仓，合并上游新版本时冲突会比较多。合并后先运行 `partition-regression` 测试，再在中国区跑一次 `cfn-lint`。
- **本文放在 `.github/README.md`**：GitHub 会优先展示这个文件。根目录的 `README.md` 保留上游原文，以减少同步时的冲突。
