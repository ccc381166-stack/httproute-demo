# Istio HTTPRoute 实战

这个实验用两条 HTTPRoute 控制两跳请求。第一跳演示外部请求怎么进集群，第二跳演示一个服务怎么把请求送到另一个服务的不同版本。应用会把每次实际收到的灰度头、命中的规则和后端版本写进响应，便于确认每一跳。

```text
Mac curl → Istio ingress → storefront
                              │ 新建 HTTP 请求，调用 inventory:8080
                              ▼
                    storefront 的 Envoy sidecar
                              ├─ inventory-stable → inventory v1
                              └─ inventory-canary → inventory v2
```

## 0. 准备环境

需要一个已安装 Istio 的 Kubernetes 集群，以及支持 `gateway.networking.k8s.io/v1` 的 Gateway API CRD。这个实验按 Istio **sidecar 模式**配置一个新命名空间，使用默认注入 webhook。若你的 Istio 只支持按 revision 注入，在创建 Pod 前把 `base/namespace.yaml` 的 `istio-injection: enabled` 换成实际的 `istio.io/rev: <revision>` 标签。

在 Mac 终端进入这个目录，核对上下文和 API：

```bash
cd /Users/lawrence/Documents/Codex/2026-11-19/zhe-g/outputs/httproute-demo

kubectl config current-context
kubectl get crd gateways.gateway.networking.k8s.io httproutes.gateway.networking.k8s.io
kubectl get gatewayclass istio -o wide
```

如果缺少 CRD，先确认谁负责安装及升级，按你所在集群的 Istio 安装规范补齐。AWS Auto Mode 的 CRD 所有权与 Istio 的 GatewayClass 是两层独立设置。这个实验要求 `istio` 类由 Istio 控制，入口通过 Istio 的 `infrastructure.parametersRef` 配置为 **ClusterIP**，使用本地端口转发验证。

## 1. 从南北向入口开始

```bash
kubectl kustomize base
kubectl apply -k base
for workload in storefront inventory-stable inventory-canary; do
  kubectl -n httproute-lab rollout status deployment/"$workload" --timeout=180s
done

kubectl -n httproute-lab get pods
kubectl -n httproute-lab get pods -l app=storefront \
  -o jsonpath='{.items[0].spec.containers[*].name}{"\n"}'

kubectl apply -f 01-ingress.yaml
kubectl -n httproute-lab wait --for=condition=Programmed gateway/lab-ingress --timeout=120s
kubectl -n httproute-lab get svc lab-ingress-istio
kubectl -n httproute-lab port-forward svc/lab-ingress-istio 18087:80
```

检查 storefront 的容器列表里应同时有 `app` 和 `istio-proxy`。确认 `lab-ingress-istio` 的类型是 `ClusterIP`。把端口转发保持运行，然后在另一终端发请求。

```bash
curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
  http://127.0.0.1:18087/shop | jq .

# 故意让域名和路径分别不匹配，查看 HTTP 状态码
curl --noproxy '*' -si -H 'Host: other.httproute.test' \
  http://127.0.0.1:18087/shop
curl --noproxy '*' -si -H 'Host: shop.httproute.test' \
  http://127.0.0.1:18087/shopping
```

成功请求应返回 `ingress_route: "storefront-ingress"`，后端 `inventory.version` 是 `v1`，`inventory.mesh_route` 是 `null`。这个实验里，不匹配的两个请求应返回 404。`PathPrefix: /shop` 按路径段匹配，会匹配 `/shop/no-context`，不会匹配 `/shopping`。

这里发生了三次选择：

1. `Gateway` 的 HTTP listener 接收 80 端口上发给 `shop.httproute.test` 的请求。
2. `HTTPRoute` 用 `parentRefs.sectionName` 挂到这个 listener，再用 `hostnames` 和路径条件匹配请求。它还通过 `RequestHeaderModifier` 设置一个观测用的标记。
3. 路由把请求转给 `Service/storefront:8080`。应用响应里的 `ingress_route` 来自代理注入的头。初始阶段 storefront 调用 `inventory` 时，Service 的 selector 只选中 v1。

## 2. 加入东西向 HTTPRoute 和灰度头

回到演练目录所在的终端，或在另一终端进入同一目录。

```bash
kubectl apply -f 02-mesh-header.yaml
kubectl -n httproute-lab get httproute inventory-mesh -o yaml
```

这次 `parentRefs` 有 `group: ""`、`kind: Service` 和 `name: inventory`。调用方仍访问 `http://inventory:8080/`，DNS 仍然按 namespace 解析。Istio 用这个 Service 关联出站路由，选择 `backendRefs` 中指定的稳定版或候选版 Service。

```bash
# 无头请求 → v1
curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
  http://127.0.0.1:18087/shop | jq .

# 灰度头经过 storefront 继续传递 → v2
curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
  -H 'x-release-cohort: canary' http://127.0.0.1:18087/shop | jq .

# 对照实验：storefront 收到灰度头，但不传给 inventory → v1
curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
  -H 'x-release-cohort: canary' http://127.0.0.1:18087/shop/no-context | jq .
```

| 请求 | storefront 收到的灰度头 | storefront 转发的灰度头 | inventory 版本 | 东西向命中 |
|---|---|---|---|---|
| `/shop` 无灰度头 | `null` | `null` | `v1` | `mesh-stable` |
| `/shop` 带灰度头 | `canary` | `canary` | `v2` | `mesh-canary` |
| `/shop/no-context` 带灰度头 | `canary` | `null` | `v1` | `mesh-stable` |

还可以绕过南北向入口，在 storefront Pod 里直接发东西向请求：

```bash
kubectl -n httproute-lab exec deployment/storefront -c app -- python -c '
from urllib.request import Request, urlopen
request = Request("http://inventory:8080/", headers={"x-release-cohort": "canary"})
print(urlopen(request, timeout=5).read().decode())
'
```

应直接看到 inventory 的 `version: v2` 和 `mesh_route: mesh-canary`。此处的关键是 storefront 发起了一次新的 HTTP 请求，必须显式传播灰度信息。实际对外服务应在可信入口认证并设置分组，不能让外部客户端自行决定自己的灰度权限。若使用 Istio Ambient，东西向的 HTTP 规则需要目标流量经过适用的 waypoint。

## 3. 把普通请求改成 90/10 放量

```bash
kubectl apply -f 03-mesh-weighted.yaml
```

它更新同一条 `HTTPRoute/inventory-mesh`。精确命中灰度头的请求继续全部走 v2。默认规则现在有两个 `backendRefs`，权重是 90 和 10。权重按相对比例分流，样本结果会波动。等规则传播后，测一批普通请求，再测单个灰度请求。

```bash
set -o pipefail
for i in {1..100}; do
  curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
    http://127.0.0.1:18087/shop | jq -er '.inventory.version' || break
done | sort | uniq -c

curl --noproxy '*' -fsS -H 'Host: shop.httproute.test' \
  -H 'x-release-cohort: canary' http://127.0.0.1:18087/shop | jq .inventory
```

应看到普通请求多数落在 v1、少数在 v2。灰度头仍固定得到 v2。将 `02-mesh-header.yaml` 再应用一次，即可恢复只有指定请求头进入候选版的模式。

## 4. 从配置状态追到 Envoy 和 Pod

```bash
kubectl -n httproute-lab get gateway lab-ingress -o yaml

kubectl -n httproute-lab get httproute -o json | jq '
  .items[] | {name: .metadata.name, generation: .metadata.generation,
    parents: [.status.parents[]? | {parent: .parentRef.name, controller: .controllerName,
      conditions: [.conditions[] | {type, status, reason, observedGeneration}]}]}'

kubectl -n httproute-lab get endpointslice \
  -l kubernetes.io/service-name=inventory-canary

istioctl proxy-config routes deployment/lab-ingress-istio -n httproute-lab
istioctl proxy-config routes deployment/storefront -n httproute-lab
```

| 现象 | 先查哪里 |
|---|---|
| 找不到 Gateway 或 HTTPRoute 资源类型 | Gateway API CRD 和 served 版本 |
| Gateway 没有 `Programmed=True` | GatewayClass、listener 和生成的 Gateway Deployment |
| `Accepted=False` 或没有父资源状态 | `parentRefs` 的名称、listener 名和挂载权限 |
| `ResolvedRefs=False` | 后端名称、Service 端口、跨命名空间引用的授权 |
| 路由已接受，灰度依旧只返回 v1 | storefront 的 sidecar、`forwarded_cohort`、Envoy 路由、当前的资源 generation |
| 返回 503 或 storefront 报 502 | EndpointSlice、Pod readiness 和应用日志。502 也可能来自 storefront 的上游请求超时 |

API server 保存 Gateway 和 HTTPRoute 对象。Istio 控制平面 `istiod` 读取对象，把监听器、匹配规则、过滤器和上游地址转换成 Envoy 可执行的配置。南北向规则在入口 Envoy 上执行，东西向规则在本实验的调用方 sidecar 上执行。HTTPRoute 的状态能帮你查配置是否被接收，实际请求能验证数据路径是否按预期工作。

## 5. 从实验映射到实际场景

| 需求 | 适合的配置 |
|---|---|
| 一个公共入口服务多个应用 | Gateway 统一管理端口和 TLS，各应用的 HTTPRoute 按域名或路径挂到监听器 |
| 让指定租户验证新版 | 可信入口打标，应用逐跳传递，HTTPRoute 按头匹配 |
| 少量真实流量试探新版 | 默认规则配置权重，再结合外部指标观察成功率和延迟 |
| 蓝绿切换 | HTTPRoute 固定指向 active 和 preview 两个 Service，由 Rollouts 更新 Service selector |
| 不同团队分 namespace | Gateway 通过 `allowedRoutes` 控制哪些 namespace 能挂路由。跨 namespace 的入口路由后端引用按 Gateway API 配置 `ReferenceGrant` |
| 同一个服务跨 AZ 就近访问 | 单独设计 Pod 分布与流量本地化。HTTPRoute 选后端 Service，代理再选具体后端实例 |

本实验的权重是手工应用的。要接 Argo Rollouts 自动修改权重，需要验证流量插件、Istio 版本以及 Gateway 或 Service 绑定方式的兼容性。要覆盖多服务全链路，给下一跳服务也加版本路由，并继续传播同一个分组标记。

## 清理

停止端口转发后，确认 `httproute-lab` 只用于本次演练。删除 namespace 会同时删除其中的实验应用和路由。

```bash
kubectl delete namespace httproute-lab
```

## 参考

- [Istio 的 Kubernetes Gateway API 配置与部署方式](https://istio.io/latest/docs/tasks/traffic-management/ingress/gateway-api/)
- [Istio 服务间请求头路由](https://istio.io/latest/docs/tasks/traffic-management/request-routing/)
- [Gateway API 的 HTTPRoute](https://gateway-api.sigs.k8s.io/reference/api-types/httproute/)
- [Gateway API Mesh 模式](https://gateway-api.sigs.k8s.io/docs/mesh/mesh-overview/)
- [Gateway API 权重分流](https://gateway-api.sigs.k8s.io/guides/user-guides/traffic-splitting/)
