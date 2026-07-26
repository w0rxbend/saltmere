---
title: "Perses: Dashboards as Code for the Post-Grafana-Relicense World"
date: 2026-07-26
track: observability
summary: "Perses is a CNCF Sandbox dashboard tool (accepted August 29, 2024) built around typed dashboards-as-code — Go and CUE SDKs, Kubernetes CRDs, embeddable panels — instead of Grafana's UI-first JSON model. Current release: v0.53.x, still pre-1.0."
reading_time: 5
tags: [perses, dashboards-as-code, gitops, cncf, prometheus, kubernetes, grafana]
sources:
  - title: "Perses | CNCF Projects"
    url: "https://www.cncf.io/projects/perses/"
  - title: "perses/perses (GitHub) — The CNCF sandbox for observability visualisation"
    url: "https://github.com/perses/perses"
  - title: "Getting started with Dashboard-as-Code (Perses docs)"
    url: "https://perses.dev/perses/docs/dac/getting-started/"
  - title: "Perses support for Prometheus (prometheus.io)"
    url: "https://prometheus.io/docs/visualization/perses/"
  - title: "PromCon Recap: Unveiling Perses, the GitOps-Friendly Metrics Visualization Tool (Logz.io)"
    url: "https://logz.io/blog/promcon-recap-perses-project/"
---

Every other post in this track has been about collecting or shipping telemetry. This one is about the last mile: putting it on a screen without your dashboards turning into an unversioned pile of clicked-together JSON. That's the problem Perses was built to solve, and it comes from inside the Prometheus project itself.

## Where it comes from

Perses was created by Augustin Husson, a Prometheus maintainer and principal engineer at Amadeus, after his team hit a wall managing over 5,000 Grafana dashboards. The failure mode was structural: Grafana's dashboard model is a UI-first JSON blob, so "dashboard as code" in practice meant clicking through the editor, exporting JSON, and hoping the diff made sense. Julius Volz — Prometheus co-creator and founder of PromLabs — publicly welcomed Perses as a GitOps-first alternative when it launched at PromCon. The project was accepted into the **CNCF Sandbox on August 29, 2024**, and is governed under the Linux Foundation rather than any single vendor, with active contributors from Amadeus, Red Hat, and Chronosphere. As of mid-2026 it's shipping the **v0.53.x** release line (v0.53.1, March 2026) — actively developed, but there is no 1.0 yet, so treat schemas as still capable of shifting between minor versions.

Red Hat has since folded Perses into OpenShift and Advanced Cluster Management as the default visualization layer for cluster observability, which is a decent signal that a CNCF Sandbox project is already trusted for production dashboards, not just demos.

## The core idea: a typed, declarative dashboard

A Perses dashboard is a structured resource — `kind: Dashboard`, with `metadata` and a `spec` made of `panels`, `layouts`, `variables`, and `datasources` — rather than an opaque JSON export. The schema is stable enough to validate statically, diff meaningfully in a PR, and generate from code instead of hand-editing.

That last part is the headline feature: **Dashboards-as-Code (DAC)**. Instead of writing raw YAML/JSON, you write a program — in Go or in CUE — that builds the dashboard object, and the `percli` CLI compiles it down to the resource Perses actually stores. This gets you loops, functions, and shared building blocks for panel groups and variables, without reaching for Jsonnet macros the way Grafana's unofficial `grafonnet` ecosystem does.

A trimmed CUE example, adapted from the official getting-started guide:

```cue
package mydac

import (
	dashboardBuilder "github.com/perses/perses/cue/dac-utils/dashboard"
	panelGroupsBuilder "github.com/perses/perses/cue/dac-utils/panelgroups"
	panelBuilder "github.com/perses/plugins/prometheus/sdk/cue/panel"
	timeseriesChart "github.com/perses/plugins/timeserieschart/schemas:model"
	promQuery "github.com/perses/plugins/prometheus/schemas/prometheus-time-series-query:model"
	promDs "github.com/perses/plugins/prometheus/schemas/datasource:model"
)

dashboardBuilder & {
	#name:    "ContainersMonitoring"
	#project: "MyProject"

	#panelGroups: panelGroupsBuilder & {
		#input: [
			{
				#title: "Resource usage"
				#cols:  3
				#panels: [
					panelBuilder & {
						spec: {
							display: name: "Container memory"
							plugin: timeseriesChart
							queries: [{
								kind: "TimeSeriesQuery"
								spec: plugin: promQuery & {
									spec: query: "max by (container) (container_memory_rss{namespace=\"$namespace\"})"
								}
							}]
						}
					},
				]
			},
		]
	}.panelGroups

	#datasources: promDemo: {
		default: true
		plugin: promDs & {spec: close({directUrl: "https://demo.prometheus.com"})}
	}
}
```

To build and ship it:

```bash
# one-time setup
cue mod init mydac
percli dac setup            # or: percli dac setup --language go
cue mod tidy

# compile the DAC program to a Perses resource
percli dac build -f dashboard.cue -o dashboard.yaml

# apply it to a running Perses instance
percli apply -f dashboard.yaml
```

The Go SDK does the same thing with fluent builders (`dashboard.New(...)`, `dashboard.AddPanelGroup(...)`, `panel.AddQuery(query.PromQL(...))`) if you'd rather stay in a language your team already tests and lints.

## Perses vs. Grafana

| | Perses | Grafana |
|---|---|---|
| Governance | CNCF Sandbox, Linux Foundation, multi-vendor | Grafana Labs, single-vendor |
| License | Apache 2.0 | AGPLv3 (since the 2021/2022 relicense) |
| Dashboard model | Typed, versioned schema; API resource | UI-exported JSON blob |
| Dashboards-as-code | Native Go/CUE SDKs + `percli` | Unofficial community tooling (Grafonnet, Jsonnet) |
| Kubernetes story | Native CRDs (`PersesDashboard`, `PersesDatasource`) via the Perses Operator | Community Grafana Operator, less native |
| Embedding | Individual panels ship as npm packages, embeddable standalone | Panels embed mainly via iframe/snapshot |
| Alerting | None — visualization only | Full built-in alerting engine |
| Data sources | Prometheus, Tempo, Loki, Pyroscope (plugin-based, growing) | Very broad, huge plugin catalog |

The practical takeaway: Grafana still wins on breadth of data sources, alerting, and ecosystem maturity. Perses wins when the thing you actually want is a dashboard *format* you can generate, validate in CI, and reconcile with Argo CD or Flux the same way you reconcile everything else in the cluster.

## Kubernetes-native deployment

Beyond the CLI, the [Perses Operator](https://github.com/perses/perses-operator) lets you manage dashboards as first-class Kubernetes objects:

```yaml
apiVersion: perses.dev/v1alpha1
kind: PersesDashboard
metadata:
  name: containers-monitoring
  namespace: monitoring
spec:
  config:
    kind: Dashboard
    metadata:
      name: containers-monitoring
      project: MyProject
    spec:
      display: {name: "Containers Monitoring"}
```

The operator reconciles the CR against a running Perses server, which is exactly the shape you want for GitOps: the dashboard lives next to the application manifests it monitors, in the same namespace, reviewed in the same pull request.

## Running it locally

```bash
docker run -d -p 8080:8080 persesdev/perses:v0.53.1
# or, from source:
git clone https://github.com/perses/perses && cd perses
make build
./bin/perses --config ./config.yaml
```

Point a Prometheus datasource at it through the UI or via `percli apply -f datasource.yaml`, then apply any dashboard resource you built with the DAC SDK above.

## Where it fits with Prometheus

Perses isn't a Prometheus fork or a TSDB — it has no storage engine or alerting of its own. It's a visualization layer with first-class, native Prometheus support, listed directly in Prometheus's own documentation as a visualization option alongside Grafana. Its `PromQL`-typed query builders in both SDKs mean a bad metric name or malformed query fails at `percli dac build` time, in CI, instead of rendering an empty panel in front of an on-call engineer at 3 a.m.

**Try next:** clone `perses/perses`, run it locally with Docker, and port one existing Grafana dashboard's panel definitions into a CUE DAC program — then diff two versions of that `.cue` file in git and see how much more legible the change is than a Grafana JSON export diff.
