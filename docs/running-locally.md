# Running locally

Beyond the demo and the seeded console in the README: a live Kind validation, the live scenario suite and the instrumented lab.

For a local live Kubernetes validation with Kind, Docker, and kubectl:

```bash
make cluster-up
make deploy
make load                  # use another terminal to generate steady traffic
make inject-bad-rollout
make ui                    # http://localhost:8080
make recover
```

Use `make rbac-check` to verify that the control-plane service account can
observe the cluster without reading Secrets or writing workloads. The complete
real-cluster lifecycle gate is `make e2e-kind`.

The same fault-injection path drives the internal live scenario suite:

```bash
make live-scenarios        # list the suite
make live-bench            # stage every scenario, grade the stored diagnosis
make live-demo SCENARIO=payment_config_change   # stage one fault and leave it in place
make ui                    # in another terminal, then open http://localhost:8080
make live-restore          # undo the staged fault when finished
```

For the instrumented lab used by the testbed (Kind, Chaos Mesh, the Connector and
a control plane on the host):

```bash
make lab-up            # cluster, images, Chaos Mesh, workload; ends with lab-check
make lab-pki           # certificates for the connector and the gateway
make connector-deploy  # the Connector and the Alertmanager route into the lab
make cp-up             # control plane and its own PostgreSQL on the host
make connector-check   # the Connector can read, and cannot write or read Secrets
```
