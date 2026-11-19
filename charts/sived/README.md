# SIVED

One pod. A volume at `/data` is the data directory. The web process and the worker share it. SQLite is `/data/editting.sqlite`.

The desk does not read `DATABASE_URL`. It does not open Postgres. A Postgres cluster added under `extraManifests` is applied as written. The desk does not connect to it.

Ingress, the Gateway API, cert-manager, a network policy, an external secret, a pod disruption budget, and a secret reloader are off until you set them. This chart does not install those controllers.

## Install

Build the image from the repo root. The default values use that local tag.

```bash
docker build -t sived:0.1.0 .
helm install sived charts/sived
```

`openssl` and `pdftotext` are in the image. LibreOffice is included only with `--build-arg INSTALL_SOFFICE=1`. The image does not install Python packages. The Python application is MIT. The Debian packages keep their own licenses. `NOTICE` in the image names them.

A version tag `v0.1.0` publishes `ghcr.io/klemsidd/sived:0.1.0`. Set `image.repository` to `ghcr.io/klemsidd/sived` and `image.tag` to that version when the cluster pulls it. The package has to be public before another account can pull it. `image.pullPolicy` is `IfNotPresent`.

Helm 3 and Helm 4 both install this chart. Kubernetes 1.25 or newer.

## The volume

`persistence.enabled` creates a `ReadWriteOnce` claim of `10Gi`. `persistence.existingClaim` uses a claim you already have. `persistence.storageClass` set to `-` asks for no class. `persistence.retain` leaves the claim in the cluster on uninstall. The next install then sets `persistence.existingClaim` to that claim.

`persistence.enabled: false` uses an emptyDir. The manuscripts go away with the pod.

`helm uninstall` deletes the claim when `retain` is false. That deletes the manuscripts, the letters, and the secrets stored in the database. Back the volume up first. Encrypt that backup. The application does not encrypt the directory.

`replicaCount` stays `1`. `strategy.type` stays `Recreate`. A second pod on this file locks it or splits it.

## The worker

`worker.mode` is `sidecar`, `cronjob`, or `off`.

`sidecar` is the default. The container waits until the database file exists, runs `python -m app work`, and sleeps `worker.interval` seconds. One manuscript per pass. The command prints the job id, or `idle`.

`cronjob` runs that command on `worker.schedule` (default every minute). `concurrencyPolicy` is `Forbid`. The job requires the web pod's node, because the claim is `ReadWriteOnce`. It needs `persistence.enabled`. `worker.affinity` replaces that node rule when you set it, and the job then has to stay on the volume's node. The job pods do not carry the Service's labels.

`off` leaves the queue to you.

## Ingress

```yaml
ingress:
  enabled: true
  className: nginx
  hosts:
    - host: desk.example.com
      paths:
        - path: /
          pathType: Prefix
```

`ingress.enabled` needs `ingress.hosts`. With `env.trustProxy` empty, the chart sets `TRUST_PROXY=1` so the public scheme and host come from the proxy. Set `env.trustProxy` to `0` or `1` when you want a fixed value.

Your annotations replace the chart's on the same key. An empty `ingress.tls` list is left empty until cert-manager is also on.

## Gateway API

```yaml
gateway:
  enabled: true
  parentRefs:
    - name: public
      namespace: gateway
  hostnames:
    - desk.example.com
```

The chart writes an `HTTPRoute`. It does not write a `Gateway`. The same `TRUST_PROXY` rule as Ingress applies. Put the certificate on the Gateway listener, or enable `certManager.certificate`.

## cert-manager

```yaml
certManager:
  enabled: true
  issuer: letsencrypt
  issuerKind: ClusterIssuer
```

The issuer must already exist. `issuerKind` is `ClusterIssuer` or `Issuer`.

With Ingress on and `ingress.tls` empty, the chart adds a TLS block. The secret name is the release fullname plus `-tls`. The Ingress is annotated with `cert-manager.io/cluster-issuer` or `cert-manager.io/issuer`. An annotation you set on that key wins.

`certManager.certificate.enabled` writes a `Certificate`. `dnsNames` defaults to the Ingress hosts, then to the Gateway hostnames. The private key is ECDSA P-256. `duration` and `renewBefore` are omitted until you set them.

`certManager.enabled` needs Ingress or `certManager.certificate.enabled`.

## TLS in the process

```yaml
tls:
  existingSecret: desk-tls
probes:
  scheme: TCP
```

The secret's `tls.crt` and `tls.key` are mounted at `/tls`, and the process serves TLS. A public certificate does not match the pod IP, so an HTTPS probe from the kubelet fails. `probes.scheme: TCP` checks that the port is open. Prefer Ingress or Gateway TLS and leave this mount empty. The probe then stays on HTTP `/healthz`.

`ACME_DOMAIN` is `env.acmeDomain` and needs `env.acmeEmail`. The chart sets `ACME_HTTP_PORT=0` unless `env.acmeHttpPort` is set, because this user cannot bind port 80. The Service already forwards its port to 8080. Port 80 of the public name has to reach that Service for the HTTP-01 challenge. cert-manager is the usual way to do this in the cluster.

## Secrets

The chart creates a Secret only when you set an inline key. `secrets.xaiApiKey` becomes `XAI_API_KEY`. `secrets.llmApiKey` becomes `LLM_API_KEY`. `secrets.extra` is any other key. Those two names belong in the fields above, not in `extra`.

`secrets.existingSecret` mounts `XAI_API_KEY` and `LLM_API_KEY` as optional keys, plus each name in `secrets.extraKeys`. Inline keys and `existingSecret` together are rejected.

`externalSecret.enabled` writes an `ExternalSecret` of `external-secrets.io/v1`. Set `secretStoreRef.name` and `data` or `dataFrom`. The chart does not install External Secrets. Inline keys and `existingSecret` are rejected while this is on. `reloader.enabled` restarts the pod when that Secret changes. The chart does not install Stakater Reloader.

A checksum of the env and the secret values rolls the pod on `helm upgrade`. A key that arrives later from External Secrets needs the reloader.

`XAI_API_KEY` and `LLM_API_KEY` stay in the environment. The desk does not copy them into the database when no provider row is saved.

## Network policy

`networkPolicy.enabled` selects the web pod and the worker pod. With `ingressFrom` empty, pods in this namespace may open the web port. An ingress controller in another namespace needs an entry, and that list replaces the same-namespace rule:

```yaml
networkPolicy:
  enabled: true
  ingressFrom:
    - podSelector: {}
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ingress-nginx
```

`allowAllEgress` defaults to true. Set it false to allow DNS on port 53 and the rules in `networkPolicy.egress`. Patreon, the model provider, and mail each need a rule when egress is closed.

## Other options

`bootstrap.enabled` runs `python -m app bootstrap` as an init container. That creates `owner@example.com` when the account is missing. It is safe to run again. Leave it off when `DEV_LOGIN` or `DEMO_MODE` is on. Those flags already seed accounts, and neither flag belongs on a listener other people can open.

`podDisruptionBudget.enabled` uses `minAvailable` or `maxUnavailable`, not both. `minAvailable: 1` on the only replica blocks draining that pod.

`service.type` may be `ClusterIP`, `NodePort`, or `LoadBalancer`. `service.nodePort` is applied for `NodePort`. `service.loadBalancerSourceRanges` limits a load balancer.

`serviceAccount.automount` is false. The desk does not call the Kubernetes API.

`extraEnv`, `extraEnvFrom`, `extraVolumes`, and `extraVolumeMounts` are copied onto the web container, the worker, and the bootstrap container. A repeated env name is the last one in the container spec.

`extraManifests` is a list of objects, templated with the release as the root. Set `metadata.namespace` to `"{{ .Release.Namespace }}"` on a namespaced object. The desk does not connect to what you add.

`resources`, `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`, and `priorityClassName` are the usual pod fields.

There is no metrics endpoint, no ServiceMonitor, and no horizontal pod autoscaler.
